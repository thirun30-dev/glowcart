"""Import / sync REAL products from Amazon.in into GlowCart.

Two data providers are supported. Set the keys for one of them (environment variables or a .env file
next to this script – see .env.example):

  1. RapidAPI "Real-Time Amazon Data"   (easiest: free tier, sign up at rapidapi.com)
        RAPIDAPI_KEY=...
  2. Amazon Product Advertising API 5.0 (official: needs an approved Amazon Associates account on amazon.in)
        AMAZON_PAAPI_ACCESS_KEY=...   AMAZON_PAAPI_SECRET_KEY=...   AMAZON_PARTNER_TAG=yourtag-21

If both are set, PA-API is used. Force one with AMAZON_PROVIDER=rapidapi or AMAZON_PROVIDER=paapi.

Amazon products are sold through the normal GlowCart cart and checkout. Their price and MRP follow
Amazon.in, and every price change reaches open browsers instantly (via the SQL triggers in schema.sql).
Amazon doesn't publish stock counts, so stock is demo inventory: it drops to 0 when Amazon says
"Currently unavailable", follows "Only 3 left in stock", and is refilled when the item is back.

Usage:
    python amazon_products.py                          # import the default beauty searches
    python amazon_products.py "kajal" "lip balm"       # import your own searches
    python amazon_products.py --refresh                # refresh prices of Amazon products already in the store
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
DB_PATH = BASE / "glowcart.db"

# search text -> GlowCart category slug
DEFAULT_SEARCHES = [
    ("lipstick", "makeup"), ("foundation", "makeup"), ("kajal eyeliner", "makeup"),
    ("face serum", "skincare"), ("sunscreen spf 50", "skincare"), ("face wash", "skincare"),
    ("shampoo", "haircare"), ("hair oil", "haircare"), ("perfume", "fragrance"),
    ("body lotion", "bath-body"), ("nail polish", "nails"),
]
CATEGORY_WORDS = {
    "nails": ["nail"],
    "fragrance": ["perfume", "fragrance", "deo", "deodorant", "eau de", "attar", "body mist"],
    "haircare": ["hair", "shampoo", "conditioner", "scalp"],
    "bath-body": ["body", "soap", "shower", "bath", "hand cream", "scrub"],
    "skincare": ["serum", "sunscreen", "spf", "moisturi", "face wash", "cleanser", "toner", "cream", "skin",
                 "face mask", "sheet mask", "night", "retinol", "niacinamide", "vitamin c"],
    "makeup": ["lip", "kajal", "eyeliner", "mascara", "foundation", "concealer", "blush", "compact",
               "primer", "highlighter", "eyeshadow", "makeup", "bronzer", "setting spray"],
}
PRODUCT_TYPES = [  # first match in the title wins
    ("lip balm", "Lip Balm"), ("lip liner", "Lip Liner"), ("lip gloss", "Lip Gloss"), ("lipstick", "Lipstick"),
    ("kajal", "Kajal"), ("eyeliner", "Eyeliner"), ("mascara", "Mascara"), ("eyeshadow", "Eyeshadow"),
    ("foundation", "Foundation"), ("concealer", "Concealer"), ("primer", "Primer"), ("compact", "Compact"),
    ("blush", "Blush"), ("highlighter", "Highlighter"), ("serum", "Serum"), ("sunscreen", "Sunscreen"),
    ("face wash", "Face Wash"), ("cleanser", "Cleanser"), ("toner", "Toner"), ("moisturi", "Moisturiser"),
    ("shampoo", "Shampoo"), ("conditioner", "Conditioner"), ("hair oil", "Hair Oil"), ("hair mask", "Hair Mask"),
    ("perfume", "Perfume"), ("eau de", "Perfume"), ("deodorant", "Deodorant"), ("body mist", "Body Mist"),
    ("body lotion", "Body Lotion"), ("body wash", "Body Wash"), ("shower gel", "Body Wash"),
    ("nail polish", "Nail Polish"), ("nail paint", "Nail Polish"),
]
UNAVAILABLE = re.compile(r"unavailable|out of stock|not available", re.I)
ONLY_LEFT = re.compile(r"only\s+(\d+)\s+left", re.I)


class AmazonError(Exception):
    """Raised with a message that is safe to show to the admin."""


# ---------------------------------------------------------------- settings
def load_env_file():
    """Read KEY=VALUE lines from .env (never overrides variables that are already set)."""
    env = BASE / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def provider_name():
    """'paapi', 'rapidapi' or None when no keys are configured."""
    load_env_file()
    has_pa = all(os.environ.get(k) for k in ("AMAZON_PAAPI_ACCESS_KEY", "AMAZON_PAAPI_SECRET_KEY",
                                             "AMAZON_PARTNER_TAG"))
    has_rapid = bool(os.environ.get("RAPIDAPI_KEY"))
    forced = os.environ.get("AMAZON_PROVIDER", "").strip().lower()
    if forced == "paapi":
        return "paapi" if has_pa else None
    if forced == "rapidapi":
        return "rapidapi" if has_rapid else None
    return "paapi" if has_pa else ("rapidapi" if has_rapid else None)


PROVIDER_LABELS = {"paapi": "Amazon PA-API 5.0", "rapidapi": "RapidAPI Real-Time Amazon Data"}


# ---------------------------------------------------------------- helpers
def money(value):
    """'₹1,299.00' / 1299 / {'Amount': 1299.0} -> 1299 (rupees) or None."""
    if isinstance(value, dict):
        value = value.get("Amount", value.get("amount"))
    if isinstance(value, (int, float)):
        return int(round(value)) if value > 0 else None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(value or ""))
    if not m:
        return None
    amount = float(m.group(0).replace(",", ""))
    return int(round(amount)) if amount > 0 else None


def http_json(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "GlowCart/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        try:
            j = json.loads(body)
            errs = j.get("Errors") or []
            body = (errs[0].get("Message") if errs else None) or j.get("message") or body
        except ValueError:
            pass
        raise AmazonError(f"Amazon data provider returned HTTP {exc.code}: {body}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AmazonError(f"Couldn't reach the Amazon data provider ({exc.__class__.__name__}).") from None


def clean(s, limit=None):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s


# An item is a plain dict:
#   asin, title, brand, price, mrp, rating, rating_count, image, url, availability, features, sales


# ---------------------------------------------------------------- provider: RapidAPI
RAPID_HOST = "real-time-amazon-data.p.rapidapi.com"


def _rapid_get(path, params):
    url = f"https://{RAPID_HOST}{path}?" + urllib.parse.urlencode(params)
    j = http_json(url, headers={"x-rapidapi-key": os.environ["RAPIDAPI_KEY"], "x-rapidapi-host": RAPID_HOST})
    if j.get("status") not in (None, "OK"):
        raise AmazonError(f"RapidAPI: {j.get('error', {}).get('message') or j.get('status')}")
    return j.get("data") or {}


def _rapid_item(p):
    sales = clean(p.get("sales_volume"))
    return {
        "asin": p.get("asin"),
        "title": clean(p.get("product_title")),
        "brand": clean(p.get("product_brand") or ""),
        "price": money(p.get("product_price")),
        "mrp": money(p.get("product_original_price")),
        "rating": float(p["product_star_rating"]) if p.get("product_star_rating") else None,
        "rating_count": p.get("product_num_ratings") or 0,
        "image": p.get("product_photo"),
        "url": p.get("product_url") or f"https://www.amazon.in/dp/{p.get('asin')}",
        "availability": clean(p.get("product_availability") or ""),
        "features": [clean(x) for x in (p.get("about_product") or [])],
        "sales": sales,
    }


def rapid_search(query, limit):
    data = _rapid_get("/search", {"query": query, "country": "IN", "page": 1, "sort_by": "RELEVANCE",
                                  "product_condition": "NEW"})
    return [_rapid_item(p) for p in (data.get("products") or [])[:limit]]


# ---------------------------------------------------------------- provider: PA-API 5.0
PA_HOST = "webservices.amazon.in"
PA_REGION = "eu-west-1"
PA_SERVICE = "ProductAdvertisingAPI"
PA_RESOURCES = ["ItemInfo.Title", "ItemInfo.ByLineInfo", "ItemInfo.Features", "Images.Primary.Large",
                "OffersV2.Listings.Price", "OffersV2.Listings.Availability"]


def _sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _pa_call(operation, payload):
    """POST to PA-API 5.0 with an AWS Signature Version 4 header."""
    access, secret = os.environ["AMAZON_PAAPI_ACCESS_KEY"], os.environ["AMAZON_PAAPI_SECRET_KEY"]
    payload = {**payload, "PartnerTag": os.environ["AMAZON_PARTNER_TAG"], "PartnerType": "Associates",
               "Marketplace": "www.amazon.in", "Resources": PA_RESOURCES}
    body = json.dumps(payload).encode("utf-8")
    path = f"/paapi5/{operation.lower()}"
    target = f"com.amazon.paapi5.v1.ProductAdvertisingAPIv1.{operation}"
    now = dt.datetime.now(dt.timezone.utc)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")

    headers = {"content-encoding": "amz-1.0", "host": PA_HOST, "x-amz-date": amz_date, "x-amz-target": target}
    signed = ";".join(sorted(headers))
    canonical = "\n".join(["POST", path, "", "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)),
                           signed, hashlib.sha256(body).hexdigest()])
    scope = f"{day}/{PA_REGION}/{PA_SERVICE}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _sign(_sign(_sign(_sign(("AWS4" + secret).encode(), day), PA_REGION), PA_SERVICE), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()

    req_headers = {**headers, "content-type": "application/json; charset=utf-8",
                   "Authorization": f"AWS4-HMAC-SHA256 Credential={access}/{scope}, "
                                    f"SignedHeaders={signed}, Signature={signature}"}
    return http_json(f"https://{PA_HOST}{path}", data=body, headers=req_headers)


def _pa_item(it):
    info = it.get("ItemInfo") or {}
    byline = info.get("ByLineInfo") or {}
    brand = ((byline.get("Brand") or byline.get("Manufacturer") or {}).get("DisplayValue")) or ""
    offers = (it.get("OffersV2") or it.get("Offers") or {}).get("Listings") or [{}]
    offer = offers[0]
    price_obj = offer.get("Price") or {}
    if "Money" in price_obj:                                       # OffersV2
        price = money(price_obj.get("Money"))
        mrp = money((price_obj.get("SavingBasis") or {}).get("Money"))
    else:                                                          # Offers (v1)
        price = money(price_obj)
        mrp = money(offer.get("SavingBasis"))
    avail = offer.get("Availability") or {}
    avail_text = avail.get("Message") or ("Currently unavailable" if avail.get("Type") == "OUT_OF_STOCK" else "")
    return {
        "asin": it.get("ASIN"),
        "title": clean((info.get("Title") or {}).get("DisplayValue")),
        "brand": clean(brand),
        "price": price,
        "mrp": mrp,
        "rating": None, "rating_count": 0,
        "image": (((it.get("Images") or {}).get("Primary") or {}).get("Large") or {}).get("URL"),
        "url": it.get("DetailPageURL") or f"https://www.amazon.in/dp/{it.get('ASIN')}",
        "availability": clean(avail_text) if offers != [{}] else "Currently unavailable",
        "features": [clean(x) for x in ((info.get("Features") or {}).get("DisplayValues") or [])],
        "sales": "",
    }


def pa_search(query, limit):
    j = _pa_call("SearchItems", {"Keywords": query, "SearchIndex": "Beauty", "ItemCount": min(10, limit)})
    return [_pa_item(i) for i in ((j.get("SearchResult") or {}).get("Items") or [])]


def pa_get_items(asins):
    out = []
    for i in range(0, len(asins), 10):                             # PA-API: max 10 ASINs per call, 1 call/sec
        if i:
            time.sleep(1.1)
        j = _pa_call("GetItems", {"ItemIds": asins[i:i + 10]})
        out += [_pa_item(it) for it in ((j.get("ItemsResult") or {}).get("Items") or [])]
    return out


def search(query, limit=10):
    prov = provider_name()
    if prov is None:
        raise AmazonError("No Amazon API key is set. Add RAPIDAPI_KEY (or the three AMAZON_PAAPI_* / "
                          "AMAZON_PARTNER_TAG keys) to a .env file – see .env.example.")
    return pa_search(query, limit) if prov == "paapi" else rapid_search(query, limit)


# ---------------------------------------------------------------- database
def ensure_schema(conn):
    """Add the Amazon columns to databases created before this feature existed."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
    if "asin" not in cols:
        conn.execute("ALTER TABLE products ADD COLUMN asin TEXT")
    if "external_rating_count" not in cols:
        conn.execute("ALTER TABLE products ADD COLUMN external_rating_count INTEGER")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_products_asin ON products(asin)")
    conn.commit()


def guess_category(text, fallback):
    t = text.lower()
    for slug, words in CATEGORY_WORDS.items():
        if any(w in t for w in words):
            return slug
    return fallback


def guess_type(title):
    t = title.lower()
    return next((name for word, name in PRODUCT_TYPES if word in t), "")


def guess_brand(title, known):
    """Amazon titles start with the brand: 'Maybelline New York Fit Me…'. Prefer brands we already have."""
    t = title.lower()
    for b in sorted(known, key=len, reverse=True):
        if len(b) > 2 and t.startswith(b.lower()):
            return b
    first = title.split()[0] if title.split() else "Amazon"
    first = first.strip(",-|")
    return first if first.isupper() and len(first) <= 4 else first.title()       # NYX, MAC stay as they are


def demo_stock(asin, availability):
    m = ONLY_LEFT.search(availability or "")
    if m:
        return int(m.group(1))
    if UNAVAILABLE.search(availability or ""):
        return 0
    return random.Random(f"glowcart-amz-{asin}").randint(8, 40)


def save_items(conn, items, category_slug=None, query_text=""):
    """Insert or update Amazon items. Returns stats."""
    conn.row_factory = sqlite3.Row
    cats = {r["slug"]: r["id"] for r in conn.execute("SELECT id, slug FROM categories")}
    known = [r["name"] for r in conn.execute("SELECT name FROM brands")]
    stats = dict(products_added=0, products_updated=0, prices_changed=0, now_unavailable=0, skipped=0)

    for it in items:
        if not it.get("asin") or not it.get("title"):
            stats["skipped"] += 1
            continue
        row = conn.execute("SELECT id FROM products WHERE asin = ?", (it["asin"],)).fetchone()
        unavailable = bool(UNAVAILABLE.search(it.get("availability") or "")) or it.get("price") is None
        if row is None and unavailable:                 # don't add things nobody can buy
            stats["skipped"] += 1
            continue

        title = clean(it["title"], 150)
        brand = it.get("brand") or guess_brand(title, known)
        brand = re.sub(r"^(Visit the |Brand: )|( Store)$", "", brand).strip() or "Amazon"
        conn.execute("INSERT OR IGNORE INTO brands (name) VALUES (?)", (brand,))
        brand_id = conn.execute("SELECT id FROM brands WHERE name = ?", (brand,)).fetchone()["id"]
        if brand not in known:
            known.append(brand)

        ptype = guess_type(title)
        slug = category_slug or guess_category(f"{title} {query_text}", "makeup")
        rating_text = (f" Rated {it['rating']}★ by {it['rating_count']:,} Amazon customers."
                       if it.get("rating") and it.get("rating_count") else "")
        desc = clean(" ".join(it.get("features") or []), 700) or f"{title}.{rating_text}"
        if it.get("sales"):
            desc += f" {it['sales']} on Amazon."
        tags = ",".join(dict.fromkeys(x for x in [ptype.lower(), query_text.lower(), brand.lower(), "amazon"] if x))

        if row:
            pid = row["id"]
            conn.execute("""UPDATE products SET name=?, image_url=COALESCE(?, image_url), product_url=?,
                            external_rating=COALESCE(?, external_rating),
                            external_rating_count=COALESCE(?, external_rating_count),
                            updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                         (title, it.get("image"), it.get("url"), it.get("rating"),
                          it.get("rating_count") or None, pid))
            stats["products_updated"] += 1
        else:
            pid = conn.execute(
                """INSERT INTO products (name, description, tags, product_type, category_id, brand_id, image_url,
                                         product_url, external_rating, external_rating_count, source, asin)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'amazon', ?)""",
                (title, desc, tags, ptype, cats.get(slug, cats.get("makeup")), brand_id, it.get("image"),
                 it.get("url"), it.get("rating"), it.get("rating_count") or None, it["asin"])).lastrowid
            stats["products_added"] += 1

        v = conn.execute("SELECT id, price, mrp, stock FROM product_variants WHERE product_id = ? ORDER BY id LIMIT 1",
                         (pid,)).fetchone()
        if v is None:
            price = it["price"]
            conn.execute("""INSERT INTO product_variants (product_id, kind, name, price, mrp, stock)
                            VALUES (?, 'Size', 'Standard', ?, ?, ?)""",
                         (pid, price, max(it.get("mrp") or price, price), demo_stock(it["asin"], it.get("availability"))))
            continue

        # Existing product: follow Amazon's price and availability (your own sales still lower the stock)
        stock = v["stock"]
        only = ONLY_LEFT.search(it.get("availability") or "")
        if unavailable:
            if stock:
                stats["now_unavailable"] += 1
            stock = 0
        elif only:
            stock = min(stock, int(only.group(1))) if stock else int(only.group(1))
        elif stock == 0:                                 # back in stock on Amazon
            stock = demo_stock(it["asin"], "")
        price = it["price"] if it.get("price") is not None else v["price"]
        mrp = max(it.get("mrp") or price, price)
        if price != v["price"]:
            stats["prices_changed"] += 1
        conn.execute("UPDATE product_variants SET price = ?, mrp = ?, stock = ? WHERE id = ?",
                     (price, mrp, stock, v["id"]))
    conn.commit()
    return stats


def _merge(total, part):
    for k, v in part.items():
        total[k] = total.get(k, 0) + v
    return total


def _record(conn, stats, action):
    meta = {"amazon_last_sync": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "amazon_provider": PROVIDER_LABELS.get(provider_name(), "–"),
            "amazon_last_action": action, "amazon_stats": json.dumps(stats)}
    conn.executemany("INSERT INTO app_meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", meta.items())
    conn.commit()


def saved_searches(conn):
    row = conn.execute("SELECT value FROM app_meta WHERE key = 'amazon_searches'").fetchone()
    return [tuple(x) for x in json.loads(row[0])] if row else []


def import_searches(conn, searches, limit=None, log=print):
    """searches: [(query, category_slug or None)]. Remembers them so --refresh can repeat them."""
    ensure_schema(conn)
    limit = limit or int(os.environ.get("AMAZON_MAX_PER_SEARCH", "10"))
    stats = {}
    for i, (q, slug) in enumerate(searches):
        if i and provider_name() == "paapi":
            time.sleep(1.1)
        items = search(q, limit)
        part = save_items(conn, items, slug, q)
        log(f"  '{q}': {len(items)} results – {part['products_added']} new, {part['prices_changed']} price changes")
        _merge(stats, part)
    remembered = saved_searches(conn)
    for s in searches:
        if list(s) not in [list(r) for r in remembered]:
            remembered.append(tuple(s))
    conn.execute("INSERT INTO app_meta (key, value) VALUES ('amazon_searches', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(remembered),))
    _record(conn, stats, "import")
    return stats


def refresh(conn, log=print):
    """Update prices/availability of the Amazon products already in the store."""
    ensure_schema(conn)
    if provider_name() is None:
        raise AmazonError("No Amazon API key is set – see .env.example.")
    if provider_name() == "paapi":                       # exact: look up every ASIN we sell
        asins = [r[0] for r in conn.execute("SELECT asin FROM products WHERE asin IS NOT NULL")]
        stats = save_items(conn, pa_get_items(asins)) if asins else {}
    else:                                                # RapidAPI: repeat the searches (1 request each)
        stats = {}
        for q, slug in saved_searches(conn):
            _merge(stats, save_items(conn, search(q, int(os.environ.get("AMAZON_MAX_PER_SEARCH", "10"))), slug, q))
    log("  " + ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in stats.items()))
    _record(conn, stats, "refresh")
    return stats


def refresh_loop(db_path, minutes):
    """Background thread body: keep Amazon prices fresh while the app runs."""
    while True:
        time.sleep(minutes * 60)
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            refresh(conn, log=lambda *_: None)
        except Exception as exc:                         # keep the thread alive; show the problem on the dashboard
            conn.execute("INSERT INTO app_meta (key, value) VALUES ('amazon_error', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(exc)[:300],))
            conn.commit()
        finally:
            conn.close()


if __name__ == "__main__":
    if not DB_PATH.exists():
        sys.exit("No database yet – run  python seed.py  first.")
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA foreign_keys = ON")
    try:
        if provider_name() is None:
            sys.exit("No Amazon API key set. Copy .env.example to .env and fill in RAPIDAPI_KEY "
                     "(or the PA-API keys).")
        print(f"Using {PROVIDER_LABELS[provider_name()]}")
        if "--refresh" in sys.argv:
            refresh(c)
        else:
            queries = [a for a in sys.argv[1:] if not a.startswith("--")]
            searches = [(q, None) for q in queries] or DEFAULT_SEARCHES
            import_searches(c, searches)
    except AmazonError as e:
        sys.exit(f"Error: {e}")
    finally:
        c.close()
