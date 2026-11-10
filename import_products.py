"""Import / sync REAL cosmetics (brands, products, shades) into GlowCart.

Source: the free Makeup API – https://makeup-api.herokuapp.com
  • real brands (NYX, Maybelline, L'Oréal, Dior, Clinique, e.l.f., Revlon…)
  • real product names, descriptions, photos and links
  • real shades with their colour hex codes  →  product varieties
Prices are converted from USD/GBP/CAD to ₹ using today's exchange rate.

Usage:
    python import_products.py            # live API (falls back to the bundled snapshot)
    python import_products.py --offline  # bundled snapshot only (data/makeup_api_snapshot.json)

Running it again is a *sync*: new products/shades are added and prices refreshed,
while your stock levels are kept. Price changes reach open browsers instantly.
"""
import html
import json
import random
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
DB_PATH = BASE / "glowcart.db"
SNAPSHOT = BASE / "data" / "makeup_api_snapshot.json"
API_URL = "https://makeup-api.herokuapp.com/api/v1/products.json"
FX_URL = "https://open.er-api.com/v6/latest/USD"
# Used only when the exchange-rate service can't be reached (rates per 1 USD, Nov 2026)
FALLBACK_RATES = {"INR": 94.0, "USD": 1.0, "GBP": 0.76, "CAD": 1.39}

BRAND_NAMES = {
    "nyx": "NYX", "e.l.f.": "e.l.f.", "l'oreal": "L'Oréal", "covergirl": "CoverGirl", "maybelline": "Maybelline",
    "wet n wild": "wet n wild", "dior": "Dior", "physicians formula": "Physicians Formula", "colourpop": "ColourPop",
    "milani": "Milani", "revlon": "Revlon", "smashbox": "Smashbox", "clinique": "Clinique", "pacifica": "Pacifica",
    "glossier": "Glossier", "almay": "Almay", "fenty": "Fenty", "benefit": "Benefit", "nyx cosmetics": "NYX",
    "deciem": "Deciem", "sally b's skin yummies": "Sally B's Skin Yummies", "c'est moi": "C'est Moi",
    "w3llpeople": "W3LL PEOPLE", "zorah biocosmetiques": "Zorah Biocosmétiques", "burt's bees": "Burt's Bees",
}
TYPE_NAMES = {"lip_liner": "Lip Liner", "nail_polish": "Nail Polish"}
TYPE_ICONS = {"nail_polish": "nails"}          # product_type -> category slug (everything else: makeup)
HEX = re.compile(r"#[0-9a-fA-F]{6}")
CSS_NAME = re.compile(r"[a-zA-Z]+\d{0,3}")


# ---------------------------------------------------------------- fetching
def _get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "GlowCart/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def load_products(offline=False):
    """Return (records, source_label)."""
    if not offline:
        try:
            data = _get_json(API_URL, timeout=60)
            if isinstance(data, list) and data:
                return data, "Makeup API (live)"
        except Exception as exc:
            print(f"  ! Live API unavailable ({exc.__class__.__name__}) – using bundled snapshot")
    return json.loads(SNAPSHOT.read_text(encoding="utf-8")), "Makeup API (offline snapshot)"


def load_rates(offline=False):
    if not offline:
        try:
            data = _get_json(FX_URL, timeout=15)
            rates = data.get("rates", {})
            if rates.get("INR"):
                return {k: float(rates[k]) for k in ("INR", "USD", "GBP", "CAD") if k in rates}, "live"
        except Exception as exc:
            print(f"  ! Exchange-rate service unavailable ({exc.__class__.__name__}) – using ₹{FALLBACK_RATES['INR']}/USD")
    return dict(FALLBACK_RATES), "fallback"


# ---------------------------------------------------------------- cleaning
def to_inr(price, currency, rates):
    """Convert to rupees and round the Indian-retail way (₹949, ₹1,299)."""
    try:
        amount = float(price)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    usd = amount / rates.get((currency or "USD").upper(), 1.0)
    inr = usd * rates["INR"]
    step = 50 if inr < 2000 else 100
    return max(99, int(round(inr / step) * step) - 1)


def clean_text(s, limit=None):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s


def brand_name(raw):
    raw = (raw or "").strip()
    return BRAND_NAMES.get(raw.lower(), raw.title())


def colour(hex_value):
    v = (hex_value or "").strip()
    m = HEX.search(v)
    if m:
        return m.group(0).upper()
    return v.lower() if CSS_NAME.fullmatch(v) else None


def variants_for(rec, price):
    """Shades from product_colors, or one 'Standard' variety."""
    out, seen = [], set()
    for c in rec.get("product_colors") or []:
        hexv = colour(c.get("hex_value"))
        name = clean_text(c.get("colour_name"))[:60] or (hexv or "").upper()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(("Shade", name, hexv))
    if not out:
        out = [("Size", "Standard", None)]
    rng = random.Random(f"glowcart-{rec['id']}")    # same demo stock every time you import
    return [(kind, name, hexv, price, price,
             0 if rng.random() < 0.08 else rng.randint(3, 40)) for kind, name, hexv in out]


def image(rec):
    url = rec.get("api_featured_image") or rec.get("image_link") or ""
    return ("https:" + url) if url.startswith("//") else (url or None)


# ---------------------------------------------------------------- sync
def sync(conn, offline=False, verbose=True):
    """Insert or update real products. Returns a summary dict."""
    conn.row_factory = sqlite3.Row
    log = print if verbose else (lambda *a, **k: None)
    records, source = load_products(offline)
    rates, rate_src = load_rates(offline)
    log(f"  Source: {source} – {len(records)} records · 1 USD = ₹{rates['INR']:.2f} ({rate_src})")

    cats = {r["slug"]: r["id"] for r in conn.execute("SELECT id, slug FROM categories")}
    stats = dict(products_added=0, products_updated=0, shades_added=0, prices_changed=0, skipped=0)

    for rec in records:
        if not rec.get("brand") or not rec.get("name") or rec.get("id") is None:
            stats["skipped"] += 1
            continue
        price = to_inr(rec.get("price"), rec.get("currency"), rates)
        if price is None:                       # no price in the API → can't be sold
            stats["skipped"] += 1
            continue
        b = brand_name(rec["brand"])
        conn.execute("INSERT OR IGNORE INTO brands (name) VALUES (?)", (b,))
        brand_id = conn.execute("SELECT id FROM brands WHERE name = ?", (b,)).fetchone()["id"]
        ptype_raw = (rec.get("product_type") or "").strip()
        ptype = TYPE_NAMES.get(ptype_raw, ptype_raw.replace("_", " ").title())
        cat_id = cats.get(TYPE_ICONS.get(ptype_raw, "makeup"), cats.get("makeup"))
        tags = ",".join(dict.fromkeys(t.strip().lower() for t in
                        [ptype.lower(), ptype_raw.replace("_", " "), rec.get("category") or "", b.lower(),
                         *(rec.get("tag_list") or [])] if t and t.strip()))
        name = clean_text(rec["name"])[:150]
        if b.lower() not in name.lower() and rec["brand"].lower() not in name.lower():
            name = f"{b} {name}"
        desc = clean_text(rec.get("description"), 700) or f"{ptype} by {b}."
        rating = rec.get("rating") if isinstance(rec.get("rating"), (int, float)) else None
        values = (name, desc, tags, ptype, cat_id, brand_id, image(rec), rec.get("product_link"), rating)

        row = conn.execute("SELECT id FROM products WHERE external_id = ?", (rec["id"],)).fetchone()
        if row:
            pid = row["id"]
            conn.execute("""UPDATE products SET name=?, description=?, tags=?, product_type=?, category_id=?,
                            brand_id=?, image_url=?, product_url=?, external_rating=?, updated_at=CURRENT_TIMESTAMP
                            WHERE id=?""", (*values, pid))
            stats["products_updated"] += 1
        else:
            pid = conn.execute("""INSERT INTO products (name, description, tags, product_type, category_id, brand_id,
                                  image_url, product_url, external_rating, source, external_id)
                                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'makeup-api', ?)""",
                               (*values, rec["id"])).lastrowid
            stats["products_added"] += 1

        for kind, vname, hexv, vprice, mrp, stock in variants_for(rec, price):
            v = conn.execute("SELECT id, price FROM product_variants WHERE product_id = ? AND name = ?",
                             (pid, vname)).fetchone()
            if v is None:
                conn.execute("""INSERT INTO product_variants (product_id, kind, name, color_hex, price, mrp, stock)
                                VALUES (?, ?, ?, ?, ?, ?, ?)""", (pid, kind, vname, hexv, vprice, mrp, stock))
                stats["shades_added"] += 1
            else:
                if v["price"] != vprice:
                    stats["prices_changed"] += 1
                # MAX keeps any manual MRP the admin set; stock is never overwritten
                conn.execute("UPDATE product_variants SET price = ?, mrp = MAX(mrp, ?), color_hex = ? WHERE id = ?",
                             (vprice, vprice, hexv, v["id"]))

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    meta = {"last_sync": now, "sync_source": source, "usd_inr": f"{rates['INR']:.2f}", "rate_source": rate_src,
            "sync_stats": json.dumps(stats)}
    conn.executemany("INSERT INTO app_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     meta.items())
    conn.commit()
    log("  " + ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in stats.items()))
    return {**stats, **meta}


if __name__ == "__main__":
    if not DB_PATH.exists():
        sys.exit("No database yet – run  python seed.py  first.")
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA foreign_keys = ON")
    print("Syncing real products…")
    sync(c, offline="--offline" in sys.argv)
    c.close()
