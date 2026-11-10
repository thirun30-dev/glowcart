"""GlowBot – the shopping assistant behind the chat bubble.

Two engines:
  1. Claude (Anthropic API) – used automatically when ANTHROPIC_API_KEY is set.
     The whole catalogue and the shopper's recent orders are given to the model,
     so it answers from real store data.
  2. Built-in assistant – works offline with no key. It understands skin concerns,
     skin types, budgets ("under 500"), categories, shades, order tracking and FAQs,
     and answers straight from the SQL database.
"""
import json
import os
import re
import urllib.request

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("GLOWCART_AI_MODEL", "claude-haiku-5-5")

# concern word -> tags to look for in products.tags
CONCERNS = {
    "acne": ["acne", "pimples", "pores", "oil control"], "pimple": ["acne", "pimples"],
    "pore": ["pores"], "oily": ["oily", "oil control"], "dry": ["dry", "moisturiser", "barrier"],
    "glow": ["glow", "brightening"], "dull": ["dull", "glow", "brightening"],
    "dark spot": ["dark spots", "pigmentation"], "pigment": ["pigmentation", "dark spots"],
    "tan": ["tan", "sunscreen"], "sun": ["sunscreen", "spf", "sun protection"],
    "sensitive": ["sensitive", "gentle"], "hydrat": ["hydration"], "moistur": ["moisturiser"],
    "hair fall": ["hair fall"], "hairfall": ["hair fall"], "frizz": ["frizz", "smooth"],
    "damaged": ["damaged hair"], "lip": ["lips", "lipstick", "lip balm"], "eye": ["eyes"],
    "perfume": ["perfume", "fragrance"], "fragrance": ["fragrance"], "gift": ["gift"],
    "nail": ["nails"], "foundation": ["foundation"], "mascara": ["mascara"], "kajal": ["kajal"],
    "lipstick": ["lipstick"], "sunscreen": ["sunscreen"], "serum": ["serum"], "shampoo": ["shampoo"],
    "scrub": ["scrub"], "face wash": ["face wash", "cleanser"], "cleanser": ["cleanser"],
}
SKIN_TYPES = {"oily": "Oily", "dry": "Dry", "sensitive": "Sensitive", "combination": "Combination"}

FAQ = {
    ("deliver", "shipping", "ship"): "🚚 Delivery takes 3–5 working days. It's FREE on orders over ₹499, otherwise ₹49.",
    ("return", "refund", "exchange"): "↩️ Unopened products can be returned within 7 days. Cancel an order before it ships "
                                       "from *My orders* — online payments are refunded automatically.",
    ("payment", "pay", "upi", "cod", "card", "cash"): "💳 We accept Cash on Delivery, UPI, Debit/Credit cards and Net Banking.",
    ("contact", "support", "help desk", "customer care"): "📞 Reach us at support@glowcart.com, 9 am – 7 pm, Monday to Saturday.",
}


# ---------------------------------------------------------------- public entry point
def answer(db, message, user=None, history=None):
    """Return {"reply": str, "products": [card dicts], "engine": "claude"|"local"}."""
    message = (message or "").strip()[:500]
    if not message:
        return {"reply": "Ask me anything about our products! 💬", "products": [], "engine": "local"}
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _claude_answer(db, message, user, history or [])
        except Exception as exc:                         # network/API problem -> stay useful offline
            print("Claude API unavailable, using built-in assistant:", exc)
    return _local_answer(db, message, user)


def product_cards(db, ids):
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = db.execute(f"SELECT * FROM product_listing WHERE id IN ({marks})", ids).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [{"id": r["id"], "name": r["name"], "brand": r["brand"], "price": r["price"], "mrp": r["mrp"],
             "icon": r["icon"], "rating": r["avg_rating"], "url": f"/product/{r['id']}"}
            for i in ids if (r := by_id.get(i))]


# ---------------------------------------------------------------- built-in engine
def _local_answer(db, message, user):
    text = message.lower()

    if re.fullmatch(r"\W*(hi|hello|hey|hii+|namaste|vanakkam|good (morning|evening|afternoon))\W*", text):
        name = f", {user['name'].split()[0]}" if user else ""
        return _reply(f"Hi{name}! 👋 I'm GlowBot. Tell me your skin type, a concern (like acne or hair fall) "
                      "or a budget, and I'll find products for you. I can also track your orders.")

    if re.search(r"\b(order|track|where is my|status)\b", text):
        return _order_status(db, user)

    for words, reply in FAQ.items():
        if any(w in text for w in words) and not re.search(r"\b(suggest|recommend|best|show|need|want)\b", text):
            return _reply(reply)

    # --- product finding
    budget = None
    m = re.search(r"(under|below|less than|within|upto|up to|max|<)\s*(rs\.?|₹|inr)?\s*(\d{2,6})", text)
    if m:
        budget = int(m.group(3))
    skin = next((v for k, v in SKIN_TYPES.items() if k in text), None)
    tags = {t for k, ts in CONCERNS.items() if k in text for t in ts}
    words = [w for w in re.findall(r"[a-z]{3,}", text) if w not in _STOP]

    brands, types = _detect(db, text)
    picks = _rank(db, text, budget, skin, tags, words, brands, types)[:4]
    note = ""
    if not picks and brands and types:     # e.g. "dior mascara" but no Dior mascara in stock
        note = f"We don't have {' / '.join(sorted(brands))} {' / '.join(sorted(types)).lower()} in stock right now. "
        picks = _rank(db, text, budget, skin, tags, words, (), types)[:4]
        brands = set()

    if not picks:
        return _reply("I couldn't find a match for that. Try something like “serum for oily skin under 600”, "
                      "“lipstick shades”, “hair fall” or “track my order”.")

    # Shade / size question about one clear product
    if re.search(r"\b(shade|shades|colou?rs?|sizes?|variant|varieties|options)\b", text):
        top = picks[0]
        vs = db.execute("SELECT * FROM product_variants WHERE product_id = ? ORDER BY price, id",
                        (top["id"],)).fetchall()
        shown = vs[:12]
        same_price = len({v["price"] for v in vs}) == 1
        listing = ", ".join(v["name"] + ("" if v["stock"] else " (sold out)") if same_price
                            else f"{v['name']} (₹{v['price']}{'' if v['stock'] else ', sold out'})" for v in shown)
        more = f" and {len(vs) - 12} more" if len(vs) > 12 else ""
        price = f" at ₹{vs[0]['price']}" if same_price else ""
        return _reply(f"**{top['name']}** comes in {len(vs)} option{'s' if len(vs) != 1 else ''}{price}: "
                      f"{listing}{more}. Tap it to see every swatch.",
                      [top["id"]], db)

    bits = sorted(brands) + sorted({k for k in CONCERNS if k in text and k not in SKIN_TYPES})
    if skin:
        bits.append(f"{skin.lower()} skin")
    if budget:
        bits.append(f"under ₹{budget}")
    intro = note + ("Here are some alternatives" if note else "Here are my top picks") + \
        (f" ({' · '.join(bits)})" if bits else "") + " ✨"
    lines = [f"• **{r['name']}** by {r['brand']} – ₹{r['price']}"
             + (f" ({(r['mrp'] - r['price']) * 100 // r['mrp']}% off)" if r["mrp"] > r["price"] else "")
             for r in picks]
    return _reply(intro + "\n" + "\n".join(lines), [r["id"] for r in picks], db)


def _detect(db, text):
    """Brands and product types named in the message ('nyx', 'lipsticks', 'nail polish')."""
    cleaned = re.sub(r"[^a-z0-9.&' ]", " ", text)
    padded = f" {cleaned} "
    brands = {r["name"] for r in db.execute("SELECT name FROM brands")
              if f" {r['name'].lower()} " in padded or f" {r['name'].lower().replace('é', 'e')} " in padded}
    types = set()
    for r in db.execute("SELECT DISTINCT product_type AS t FROM products WHERE product_type != ''"):
        t = r["t"].lower()
        if re.search(rf"\b{re.escape(t)}(e?s)?\b", text):
            types.add(r["t"])
    return brands, types


def _rank(db, text, budget=None, skin=None, tags=(), words=(), brands=(), types=()):
    """Score in-stock products against the shopper's words (tags count most).
    A brand or product type named in the question is a hard filter."""
    rows = db.execute("SELECT * FROM product_listing WHERE stock > 0").fetchall()
    scored = []
    for r in rows:
        if brands and r["brand"] not in brands:
            continue
        if types and r["product_type"] not in types:
            continue
        ptags = {t.strip() for t in r["tags"].split(",")}
        hay = f"{r['name']} {r['brand']} {r['product_type']} {r['category']}".lower()
        score = 3 * len(set(tags) & ptags) + sum(2 if w in hay else (1 if w in r["description"].lower() else 0)
                                                  for w in words)
        if skin and r["skin_type"] == skin:
            score += 2
        if budget is not None and r["price"] > budget:
            continue
        if brands or types:
            score += 1      # already matched the brand/type filter
        if score > 0 or (budget is not None and not tags and not words):
            scored.append((score, r["avg_rating"] or 0, r["external_rating"] or 0, r))
    scored.sort(key=lambda x: (-x[0], -x[1], -x[2]))
    if scored:                          # drop weak matches (e.g. a word found only in a description)
        best = scored[0][0]
        scored = [x for x in scored if x[0] >= max(1, best / 2)]
    return [r for *_, r in scored]


def _order_status(db, user):
    if not user:
        return _reply("Please sign in and I'll show you where your orders are. 📦")
    orders = db.execute("SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 3",
                        (user["id"],)).fetchall()
    if not orders:
        return _reply("You haven't placed any orders yet. Want some recommendations? 😊")
    lines = [f"• Order #{o['id']} – **{o['status']}**, ₹{o['total']} ({o['payment_method']}, {o['payment_status']})"
             for o in orders]
    return _reply("Your latest orders:\n" + "\n".join(lines))


def _reply(text, ids=None, db=None):
    return {"reply": text, "products": product_cards(db, ids) if ids and db else [], "engine": "local"}


_STOP = set("""the and for with you your can are what which best good some any show need want looking
suggest recommend please products product skin type me get buy give under below less than within
upto rupees has have this that from like price cost much how tell about""".split())


# ---------------------------------------------------------------- Claude engine
def _catalogue_text(db, message):
    """The 40 products most relevant to the question (the full catalogue is too big for one prompt)."""
    text = message.lower()
    words = [w for w in re.findall(r"[a-z]{3,}", text) if w not in _STOP]
    tags = {t for k, ts in CONCERNS.items() if k in text for t in ts}
    brands, types = _detect(db, text)
    rows = _rank(db, text, tags=tags, words=words, brands=brands, types=types)[:40]
    if len(rows) < 15:      # vague question → add best-rated products from every category
        rows += db.execute("""SELECT * FROM product_listing WHERE stock > 0
                              ORDER BY avg_rating DESC, external_rating DESC LIMIT 25""").fetchall()
    out, seen = [], set()
    for p in rows:
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        vs = db.execute("SELECT name, price, stock FROM product_variants WHERE product_id = ? ORDER BY price LIMIT 15",
                        (p["id"],)).fetchall()
        opts = "; ".join(f"{v['name']} ₹{v['price']}{'' if v['stock'] else ' (sold out)'}" for v in vs)
        if p["variant_count"] > 15:
            opts += f"; +{p['variant_count'] - 15} more"
        out.append(f"[#{p['id']}] {p['name']} | {p['brand']} | {p['product_type'] or p['category']} | "
                   f"skin: {p['skin_type']} | {p['description'][:160]} | options: {opts}")
    return "\n".join(out)


def _claude_answer(db, message, user, history):
    orders = ""
    if user:
        rows = db.execute("SELECT id, status, total, payment_status, created_at FROM orders "
                          "WHERE user_id = ? ORDER BY id DESC LIMIT 5", (user["id"],)).fetchall()
        orders = "\n".join(f"Order #{o['id']}: {o['status']}, ₹{o['total']}, payment {o['payment_status']}, "
                           f"placed {o['created_at']}" for o in rows) or "No orders yet."
    system = (
        "You are GlowBot, the friendly beauty advisor for GlowCart, an Indian online cosmetics store. "
        "Answer briefly (under 120 words), warmly, in the shopper's language. Recommend ONLY products from "
        "the list below and always cite them as [#id]. Prices are in rupees. Never invent products, "
        "prices, discounts or order details. For medical skin problems suggest seeing a dermatologist.\n"
        "Store policy: delivery 3–5 days, free over ₹499 else ₹49; 7-day returns on unopened items; "
        "payment by COD, UPI, card or net banking.\n\n"
        f"RELEVANT PRODUCTS (live prices and stock):\n{_catalogue_text(db, message)}\n\n"
        f"SHOPPER: {user['name'] if user else 'guest (not signed in)'}\nRECENT ORDERS:\n{orders or 'Not signed in.'}"
    )
    msgs = [{"role": h["role"], "content": h["content"][:1000]}
            for h in history[-8:] if h.get("role") in ("user", "assistant") and h.get("content")]
    msgs.append({"role": "user", "content": message})
    body = json.dumps({"model": MODEL, "max_tokens": 400, "system": system, "messages": msgs}).encode()
    req = urllib.request.Request(ANTHROPIC_URL, data=body, method="POST", headers={
        "content-type": "application/json",
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    ids = list(dict.fromkeys(int(i) for i in re.findall(r"\[#(\d+)\]", text)))[:4]
    text = re.sub(r"\s*\[#\d+\]", "", text)
    return {"reply": text, "products": product_cards(db, ids), "engine": "claude"}
