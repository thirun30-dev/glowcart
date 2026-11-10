"""Simulate shoppers so you can watch GlowCart update in real time.

Run in a second terminal while the app is open in your browser:
    python simulate_shoppers.py            # one event every ~2 seconds, until Ctrl+C
    python simulate_shoppers.py --fast     # one event every ~0.5 seconds

It writes ordinary SQL updates (buy, restock, price change). The database triggers log them,
and every open page updates instantly through /api/live.
"""
import random
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "glowcart.db"


def step(conn, rng):
    roll = rng.random()
    if roll < 0.75:                                                     # a sale
        v = conn.execute("""SELECT id, stock FROM product_variants WHERE stock > 0
                            ORDER BY RANDOM() LIMIT 1""").fetchone()
        qty = min(v[1], rng.choice([1, 1, 1, 2]))
        conn.execute("UPDATE product_variants SET stock = stock - ? WHERE id = ? AND stock >= ?", (qty, v[0], qty))
        msg = f"sold {qty} of variety #{v[0]}"
    elif roll < 0.9:                                                    # restock (prefer sold-out shades)
        v = conn.execute("SELECT id FROM product_variants ORDER BY stock = 0 DESC, RANDOM() LIMIT 1").fetchone()
        qty = rng.randint(5, 25)
        conn.execute("UPDATE product_variants SET stock = stock + ? WHERE id = ?", (qty, v[0]))
        msg = f"restocked variety #{v[0]} (+{qty})"
    else:                                                               # flash sale: 10–20% off a whole product
        p = conn.execute("SELECT product_id FROM product_variants ORDER BY RANDOM() LIMIT 1").fetchone()[0]
        pct = rng.choice([10, 15, 20])
        conn.execute("""UPDATE product_variants SET mrp = MAX(mrp, price),
                        price = MAX(99, CAST(ROUND(MAX(mrp, price) * (100 - ?) / 100.0 / 10) * 10 - 1 AS INTEGER))
                        WHERE product_id = ?""", (pct, p))
        msg = f"flash sale {pct}% off product #{p}"
    conn.commit()
    return msg


if __name__ == "__main__":
    if not DB_PATH.exists():
        sys.exit("Run  python seed.py  first.")
    delay = 0.5 if "--fast" in sys.argv else 2.0
    conn = sqlite3.connect(DB_PATH, timeout=10)
    rng = random.Random()
    print("Simulating shoppers – open http://127.0.0.1:5000 and watch the prices and stock change. Ctrl+C to stop.")
    try:
        while True:
            print(" ", step(conn, rng))
            time.sleep(delay * rng.uniform(0.6, 1.4))
    except KeyboardInterrupt:
        print("\nStopped.")
