"""Create the database and fill it with sample cosmetics data.

Run:  python seed.py          (re-run any time to reset the store; uses the bundled real-product snapshot)
      python seed.py --live   (same, but downloads the newest products and exchange rate first)
"""
import sqlite3
from pathlib import Path

from werkzeug.security import generate_password_hash

BASE = Path(__file__).parent
DB_PATH = BASE / "glowcart.db"

CATEGORIES = [
    ("Skincare", "skincare", "🧴"),
    ("Makeup", "makeup", "💄"),
    ("Haircare", "haircare", "💇"),
    ("Fragrance", "fragrance", "🌸"),
    ("Bath & Body", "bath-body", "🛁"),
    ("Nails", "nails", "💅"),
]

BRANDS = ["Lumière", "PureLeaf", "Velvet Rose", "Aqua Botanica", "Kesar Naturals", "Noir Studio"]


def sizes(*rows):
    """rows of (name, price, mrp, stock) -> Size variants"""
    return [("Size", n, None, p, m, s) for n, p, m, s in rows]


def shades(price, mrp, *rows):
    """rows of (shade name, hex, stock) -> Shade variants with the same price"""
    return [("Shade", n, h, price, mrp, s) for n, h, s in rows]


# name, description, skin_type, tags, category, brand, variants
PRODUCTS = [
    # ---- Skincare
    ("Vitamin C Brightening Serum", "10% Vitamin C with ferulic acid to fade dark spots and even out skin tone.",
     "All", "glow,dark spots,pigmentation,dull,brightening,serum", "skincare", "Lumière",
     sizes(("15 ml", 449, 599, 30), ("30 ml", 699, 999, 40))),
    ("Hyaluronic Acid Hydrating Gel", "Oil-free gel moisturiser that locks in hydration for 72 hours.",
     "Oily", "hydration,moisturiser,oily,dehydrated,gel", "skincare", "Aqua Botanica",
     sizes(("50 ml", 449, 599, 60), ("100 ml", 749, 999, 25))),
    ("Niacinamide 10% + Zinc Serum", "Controls excess oil, calms acne and visibly reduces pores.",
     "Oily", "acne,pimples,pores,oil control,oily,serum", "skincare", "PureLeaf",
     sizes(("30 ml", 549, 699, 35))),
    ("Ceramide Repair Night Cream", "Rich barrier-repair cream for dry and sensitive skin.",
     "Dry", "dry,barrier,night cream,moisturiser,sensitive,flaky", "skincare", "Lumière",
     sizes(("50 g", 799, 1099, 25))),
    ("SPF 50 PA++++ Sunscreen Gel", "Lightweight, no white cast, water resistant for 80 minutes.",
     "All", "sunscreen,spf,sun,tan,uv,sun protection", "skincare", "Aqua Botanica",
     sizes(("50 g", 399, 499, 80), ("100 g", 649, 849, 40))),
    ("Kumkumadi Face Oil", "Ayurvedic saffron blend for a natural glow.",
     "Dry", "ayurvedic,glow,saffron,face oil,dull,dry", "skincare", "Kesar Naturals",
     sizes(("15 ml", 899, 1299, 20))),
    ("Gentle Foaming Face Wash", "Soap-free, pH-balanced daily cleanser.",
     "Sensitive", "face wash,cleanser,sensitive,gentle", "skincare", "PureLeaf",
     sizes(("100 ml", 249, 299, 100), ("200 ml", 399, 499, 50))),

    # ---- Makeup
    ("Matte Liquid Lipstick", "Transfer-proof, 16-hour wear with a velvet finish.",
     "All", "lipstick,lips,matte,long lasting", "makeup", "Velvet Rose",
     shades(499, 699, ("Ruby Red", "#9b111e", 20), ("Nude Rose", "#c48b7f", 25),
               ("Berry Bliss", "#7b2d4f", 15), ("Coral Crush", "#e8665a", 18))),
    ("Longwear Foundation SPF 20", "Medium-to-full coverage, 24-hour wear.",
     "Combination", "foundation,base,coverage,face makeup", "makeup", "Noir Studio",
     shades(849, 1199, ("Ivory 110", "#f1d3bb", 10), ("Beige 220", "#dcb08a", 12),
               ("Honey 330", "#c18f5f", 12), ("Caramel 440", "#9c6a43", 8), ("Mocha 550", "#6e4a33", 6))),
    ("Volume Boost Mascara", "Smudge-proof, buildable volume.",
     "All", "mascara,eyes,lashes,volume", "makeup", "Noir Studio",
     shades(399, 549, ("Black", "#111111", 30), ("Brown-Black", "#3b2a20", 15))),
    ("Kajal Kohl Pencil", "Intense colour, waterproof, 12-hour stay.",
     "All", "kajal,kohl,eyeliner,eyes", "makeup", "Kesar Naturals",
     shades(199, 249, ("Jet Black", "#0b0b0b", 80), ("Deep Brown", "#4a2f22", 30), ("Navy", "#1c2a5a", 20))),
    ("12-Shade Eyeshadow Palette", "Mattes and shimmers in curated tones.",
     "All", "eyeshadow,palette,eyes,party", "makeup", "Velvet Rose",
     shades(999, 1499, ("Sunset", "#d9774b", 10), ("Smoky Nights", "#4d4a57", 8))),
    ("Tinted Lip Balm SPF 15", "Moisturising lip balm with a sheer tint.",
     "All", "lip balm,lips,dry lips,tint", "makeup", "PureLeaf",
     shades(179, 199, ("Peach", "#f5a77f", 40), ("Cherry", "#c8213a", 35), ("Clear", "#f3e9e3", 30))),

    # ---- Haircare
    ("Onion & Biotin Hair Oil", "Reduces hair fall and strengthens roots.",
     "All", "hair fall,hair growth,oil,biotin,onion", "haircare", "Kesar Naturals",
     sizes(("100 ml", 249, 299, 40), ("200 ml", 349, 449, 70))),
    ("Keratin Smooth Shampoo", "Sulphate-free shampoo for frizz control.",
     "All", "shampoo,frizz,smooth,keratin", "haircare", "Aqua Botanica",
     sizes(("300 ml", 399, 499, 55), ("650 ml", 699, 899, 20))),
    ("Argan Oil Hair Mask", "Deep conditioning mask for dry, damaged hair.",
     "All", "hair mask,damaged hair,dry hair,conditioning,argan", "haircare", "Lumière",
     sizes(("200 g", 549, 749, 30))),

    # ---- Fragrance
    ("Oud Noir Eau de Parfum", "Warm oud, amber and smoky vanilla.",
     "All", "perfume,oud,fragrance,gift,men,women,long lasting", "fragrance", "Noir Studio",
     sizes(("50 ml", 1899, 2499, 12), ("100 ml", 2999, 3999, 6))),
    ("Rose & Jasmine Body Mist", "Fresh floral mist for everyday wear.",
     "All", "body mist,perfume,floral,fragrance,gift", "fragrance", "Velvet Rose",
     sizes(("150 ml", 349, 449, 65))),

    # ---- Bath & Body
    ("Shea Butter Body Lotion", "48-hour moisture for very dry skin.",
     "Dry", "body lotion,dry skin,moisturiser,winter", "bath-body", "PureLeaf",
     sizes(("200 ml", 299, 349, 40), ("400 ml", 449, 549, 50))),
    ("Coffee Body Scrub", "Exfoliating arabica scrub to smooth rough skin.",
     "All", "scrub,exfoliate,tan,body,coffee", "bath-body", "Kesar Naturals",
     sizes(("100 g", 399, 599, 40))),

    # ---- Nails
    ("Gel Shine Nail Polish", "Gel-shine finish without a UV lamp.",
     "All", "nail polish,nails,gel", "nails", "Velvet Rose",
     shades(249, 349, ("Berry Wine", "#6d1a36", 25), ("Baby Pink", "#f4b6c2", 30),
               ("Ocean Blue", "#2a6fb0", 20), ("Classic Red", "#c1121f", 25))),
    ("Cuticle Repair Oil", "Vitamin E oil for healthy nails and cuticles.",
     "All", "cuticle,nails,nail care,vitamin e", "nails", "PureLeaf",
     sizes(("10 ml", 199, 299, 60))),
]


STORE_TYPES = [  # keyword in product name -> product type (for GlowCart's own store-brand items)
    ("Serum", "Serum"), ("Gel", "Moisturiser"), ("Cream", "Moisturiser"), ("Sunscreen", "Sunscreen"),
    ("Face Oil", "Face Oil"), ("Face Wash", "Face Wash"), ("Lipstick", "Lipstick"), ("Foundation", "Foundation"),
    ("Mascara", "Mascara"), ("Kajal", "Eyeliner"), ("Eyeshadow", "Eyeshadow"), ("Lip Balm", "Lip Balm"),
    ("Hair Oil", "Hair Oil"), ("Shampoo", "Shampoo"), ("Hair Mask", "Hair Mask"), ("Parfum", "Perfume"),
    ("Mist", "Body Mist"), ("Lotion", "Body Lotion"), ("Scrub", "Body Scrub"), ("Nail Polish", "Nail Polish"),
    ("Cuticle", "Nail Care"),
]


def store_type(name):
    return next((t for k, t in STORE_TYPES if k.lower() in name.lower()), "")


def seed(live=False, db_path=None):
    db_path = Path(db_path or DB_PATH)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript((BASE / "schema.sql").read_text(encoding="utf-8"))

    conn.executemany("INSERT INTO categories (name, slug, icon) VALUES (?, ?, ?)", CATEGORIES)
    conn.executemany("INSERT INTO brands (name) VALUES (?)", [(b,) for b in BRANDS])
    cat_ids = {slug: i for i, slug in conn.execute("SELECT id, slug FROM categories")}
    brand_ids = {name: i for i, name in conn.execute("SELECT id, name FROM brands")}

    for name, desc, skin, tags, cat, brand, variants in PRODUCTS:
        cur = conn.execute(
            """INSERT INTO products (name, description, skin_type, tags, product_type, category_id, brand_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, desc, skin, tags, store_type(name), cat_ids[cat], brand_ids[brand]))
        conn.executemany(
            """INSERT INTO product_variants (product_id, kind, name, color_hex, price, mrp, stock)
               VALUES (?, ?, ?, ?, ?, ?, ?)""", [(cur.lastrowid, *v) for v in variants])

    conn.execute("INSERT INTO users (name, email, phone, password_hash, is_admin) VALUES (?, ?, ?, ?, 1)",
                 ("Admin", "admin@glowcart.com", "", generate_password_hash("admin123")))
    conn.execute("INSERT INTO users (name, email, phone, password_hash) VALUES (?, ?, ?, ?)",
                 ("Demo Shopper", "demo@glowcart.com", "9876543210", generate_password_hash("demo123")))
    conn.execute("""INSERT INTO addresses (user_id, label, name, phone, line, city, pincode, is_default)
                    VALUES (2, 'Home', 'Demo Shopper', '9876543210', '12, Anna Salai', 'Chennai', '600002', 1)""")
    conn.executemany("INSERT INTO reviews (product_id, user_id, rating, comment) VALUES (?, 2, ?, ?)",
                     [(1, 5, "Visible glow in two weeks!"), (5, 4, "No white cast, love it."),
                      (8, 5, "Ruby Red doesn't budge all day."), (14, 4, "Hair fall reduced noticeably.")])
    conn.commit()

    # Real brands, products and shades from the Makeup API
    import import_products
    print("Importing real products…")
    import_products.sync(conn, offline=not live)
    conn.execute("DELETE FROM live_changes")          # start the live feed empty
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'live_changes'")
    conn.commit()
    n_p, n_v = conn.execute("SELECT (SELECT COUNT(*) FROM products), (SELECT COUNT(*) FROM product_variants)").fetchone()
    conn.close()
    print(f"Database created at {db_path} –{n_p} products, {n_v} varieties")
    print("Admin login : admin@glowcart.com / admin123")
    print("Demo login  : demo@glowcart.com / demo123")


if __name__ == "__main__":
    import sys
    seed(live="--live" in sys.argv)   # --live: fetch the newest data from the API instead of the snapshot
