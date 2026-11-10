"""GlowCart – an Amazon-style cosmetics store built with Flask + SQLite.

Run:
    pip install -r requirements.txt
    python app.py                 # open http://127.0.0.1:5000
    (python seed.py resets the database with sample data)

Optional: set ANTHROPIC_API_KEY to power the GlowBot chat assistant with Claude.
"""
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import date
from functools import wraps
from pathlib import Path

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template,
                   request, session, stream_with_context, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import amazon_products
import chatbot
import import_products

BASE = Path(__file__).parent
# On Vercel only /tmp is writable, and it is wiped whenever the server instance is recycled:
# the store is rebuilt from the bundled snapshot on each cold start.
ON_VERCEL = bool(os.environ.get("VERCEL"))
DB_PATH = Path(os.environ.get("GLOWCART_DB") or ("/tmp/glowcart.db" if ON_VERCEL else BASE / "glowcart.db"))

app = Flask(__name__)
if ON_VERCEL and not os.environ.get("GLOWCART_SECRET"):
    raise RuntimeError("Set GLOWCART_SECRET in Vercel → Project → Settings → Environment Variables.")
app.config["SECRET_KEY"] = os.environ.get("GLOWCART_SECRET", "change-me-in-production")
if ON_VERCEL and not DB_PATH.exists():
    import seed
    seed.seed(db_path=DB_PATH)

SORTS = {
    "relevance": "p.id DESC",
    "price_asc": "p.price ASC",
    "price_desc": "p.price DESC",
    "rating": "p.avg_rating DESC, p.review_count DESC",
    "discount": "(p.mrp - p.price) * 1.0 / p.mrp DESC",
}
PER_PAGE = 24
FREE_SHIPPING_OVER = 499
SHIPPING_FEE = 49
PAYMENT_METHODS = {"COD": "Cash on Delivery", "UPI": "UPI", "Card": "Credit / Debit card",
                   "NetBanking": "Net Banking"}
amazon_products.load_env_file()
if DB_PATH.exists():
    with sqlite3.connect(DB_PATH) as _conn:
        amazon_products.ensure_schema(_conn)        # adds the Amazon columns to older databases
AMAZON_REFRESH_MINUTES = int(os.environ.get("AMAZON_REFRESH_MINUTES", "0") or 0)
BANKS = ["State Bank of India", "HDFC Bank", "ICICI Bank", "Axis Bank", "Kotak Mahindra Bank",
         "Indian Bank", "Canara Bank"]


# ---------------------------------------------------------------- database
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, args=(), one=False):
    rows = get_db().execute(sql, args).fetchall()
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur


# ---------------------------------------------------------------- helpers
@app.before_request
def load_user():
    uid = session.get("user_id")
    g.user = query("SELECT * FROM users WHERE id = ?", (uid,), one=True) if uid else None


@app.context_processor
def inject_globals():
    return {
        "categories": query("SELECT * FROM categories ORDER BY id"),
        "cart_count": sum(session.get("cart", {}).values()),
        "user": g.user,
        "ai_engine": "Claude AI" if os.environ.get("ANTHROPIC_API_KEY") else "GlowBot",
    }


@app.template_filter("inr")
def inr(value):
    return f"₹{int(value):,}"


@app.template_filter("off")
def pct_off(row):
    return (row["mrp"] - row["price"]) * 100 // row["mrp"] if row["mrp"] else 0


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("Please sign in to continue.", "info")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not g.user["is_admin"]:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def cart_details():
    """Return (items, subtotal, shipping, total). The session cart maps variant_id -> qty."""
    cart = session.get("cart", {})
    if not cart:
        return [], 0, 0, 0
    ids = [int(v) for v in cart]
    marks = ",".join("?" * len(ids))
    rows = query(f"""SELECT v.id AS variant_id, v.name AS variant_name, v.kind, v.color_hex,
                            v.price, v.mrp, v.stock, p.id AS product_id, p.name, p.brand,
                            p.icon, p.category_slug, p.variant_count, p.image_url
                     FROM product_variants v JOIN product_listing p ON p.id = v.product_id
                     WHERE v.id IN ({marks})""", ids)
    items = []
    for r in rows:
        qty = min(cart[str(r["variant_id"])], r["stock"])
        if qty > 0:
            items.append({"v": r, "qty": qty, "line": qty * r["price"]})
    subtotal = sum(i["line"] for i in items)
    shipping = 0 if subtotal >= FREE_SHIPPING_OVER or subtotal == 0 else SHIPPING_FEE
    return items, subtotal, shipping, subtotal + shipping


def wished_ids():
    if not g.user:
        return set()
    return {r["product_id"] for r in query("SELECT product_id FROM wishlist WHERE user_id = ?",
                                           (g.user["id"],))}


# ---------------------------------------------------------------- catalogue
@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    cat = request.args.get("category", "")
    skin = request.args.get("skin", "")
    brand = request.args.get("brand", "")
    ptype = request.args.get("type", "")
    sort = request.args.get("sort", "relevance")
    min_price = request.args.get("min", type=int)
    max_price = request.args.get("max", type=int)
    page = max(1, request.args.get("page", 1, type=int))

    where, args = [], []
    if q:
        where.append("(p.name LIKE ? OR p.brand LIKE ? OR p.product_type LIKE ? OR p.tags LIKE ? OR p.description LIKE ?)")
        args += [f"%{q}%"] * 5
    for col, val in (("p.category_slug", cat), ("p.brand", brand), ("p.product_type", ptype)):
        if val:
            where.append(f"{col} = ?")
            args.append(val)
    if skin:
        where.append("(p.skin_type = ? OR p.skin_type = 'All')")
        args.append(skin)
    if min_price is not None:
        where.append("p.price >= ?")
        args.append(min_price)
    if max_price is not None:
        where.append("p.price <= ?")
        args.append(max_price)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total = query(f"SELECT COUNT(*) AS n FROM product_listing p{where_sql}", args, one=True)["n"]
    pages = max(1, -(-total // PER_PAGE))
    page = min(page, pages)
    products = query(f"""SELECT * FROM product_listing p{where_sql}
                         ORDER BY (p.stock > 0) DESC, {SORTS.get(sort, SORTS['relevance'])}
                         LIMIT ? OFFSET ?""", args + [PER_PAGE, (page - 1) * PER_PAGE])

    filtered = any([q, cat, skin, brand, ptype, min_price, max_price, page > 1])
    deals = popular = amazon = []
    if not filtered:
        amazon = query("SELECT * FROM product_listing p WHERE source = 'amazon' AND stock > 0 "
                       "ORDER BY updated_at DESC, external_rating_count DESC LIMIT 8")
        deals = query(f"SELECT * FROM product_listing p WHERE stock > 0 AND mrp > price ORDER BY {SORTS['discount']} LIMIT 8")
        popular = query("""SELECT p.* FROM product_listing p
                           JOIN (SELECT product_id, MAX(id) AS last FROM live_changes WHERE kind = 'sale'
                                 GROUP BY product_id ORDER BY last DESC LIMIT 8) s ON s.product_id = p.id
                           ORDER BY s.last DESC""")
    brands = query("""SELECT b.name, COUNT(*) AS n FROM products p JOIN brands b ON b.id = p.brand_id
                      GROUP BY b.id ORDER BY n DESC, b.name""")
    types = query("SELECT product_type AS name, COUNT(*) AS n FROM products WHERE product_type != '' GROUP BY 1 ORDER BY n DESC")
    args_no_page = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("index.html", products=products, deals=deals, popular=popular, amazon=amazon, q=q, cat=cat,
                           skin=skin, brand=brand, ptype=ptype, sort=sort, min_price=min_price,
                           max_price=max_price, wished=wished_ids(), brands=brands, types=types,
                           total=total, page=page, pages=pages, args_no_page=args_no_page)


@app.route("/product/<int:pid>")
def product(pid):
    p = query("SELECT * FROM product_listing WHERE id = ?", (pid,), one=True)
    if p is None:
        abort(404)
    variants = query("SELECT * FROM product_variants WHERE product_id = ? ORDER BY price, id", (pid,))
    selected = request.args.get("v", type=int)
    if selected not in {v["id"] for v in variants}:
        in_stock = [v for v in variants if v["stock"] > 0]
        selected = (in_stock or variants)[0]["id"]
    reviews = query("""SELECT r.*, u.name AS user_name FROM reviews r JOIN users u ON u.id = r.user_id
                       WHERE r.product_id = ? ORDER BY r.created_at DESC""", (pid,))
    related = query("""SELECT * FROM product_listing WHERE category_id = ? AND id != ?
                       ORDER BY avg_rating DESC LIMIT 4""", (p["category_id"], pid))
    can_review = bool(g.user and query(
        """SELECT 1 FROM order_items oi JOIN orders o ON o.id = oi.order_id
           WHERE o.user_id = ? AND oi.product_id = ? AND o.status != 'Cancelled'""",
        (g.user["id"], pid), one=True))
    return render_template("product.html", p=p, variants=variants, selected=selected,
                           reviews=reviews, related=related, can_review=can_review, wished=wished_ids())


@app.post("/product/<int:pid>/review")
@login_required
def add_review(pid):
    rating = request.form.get("rating", type=int)
    comment = request.form.get("comment", "").strip()[:1000]
    if rating not in range(1, 6):
        flash("Please choose a rating from 1 to 5.", "error")
    else:
        execute("""INSERT INTO reviews (product_id, user_id, rating, comment) VALUES (?, ?, ?, ?)
                   ON CONFLICT(product_id, user_id) DO UPDATE SET rating = excluded.rating,
                   comment = excluded.comment, created_at = CURRENT_TIMESTAMP""",
                (pid, g.user["id"], rating, comment))
        flash("Thanks for your review!", "success")
    return redirect(url_for("product", pid=pid))


# ---------------------------------------------------------------- wishlist
@app.post("/wishlist/<int:pid>")
@login_required
def toggle_wishlist(pid):
    if query("SELECT 1 FROM wishlist WHERE user_id = ? AND product_id = ?", (g.user["id"], pid), one=True):
        execute("DELETE FROM wishlist WHERE user_id = ? AND product_id = ?", (g.user["id"], pid))
        flash("Removed from wishlist.", "info")
    else:
        execute("INSERT OR IGNORE INTO wishlist (user_id, product_id) VALUES (?, ?)", (g.user["id"], pid))
        flash("Saved to wishlist ♥", "success")
    return redirect(request.referrer or url_for("index"))


@app.route("/wishlist")
@login_required
def wishlist():
    products = query("""SELECT p.* FROM product_listing p JOIN wishlist w ON w.product_id = p.id
                        WHERE w.user_id = ?""", (g.user["id"],))
    return render_template("wishlist.html", products=products, wished={p["id"] for p in products})


# ---------------------------------------------------------------- cart
@app.post("/cart/add/<int:pid>")
def cart_add(pid):
    vid = request.form.get("variant_id", type=int)
    if vid is None:
        p = query("SELECT default_variant_id FROM product_listing WHERE id = ?", (pid,), one=True)
        vid = p["default_variant_id"] if p else None
    v = query("SELECT id, name, stock FROM product_variants WHERE id = ? AND product_id = ?", (vid, pid), one=True)
    if v is None:
        abort(404)
    qty = max(1, request.form.get("qty", 1, type=int))
    cart = session.get("cart", {})
    new_qty = min(cart.get(str(v["id"]), 0) + qty, v["stock"])
    if new_qty <= 0:
        flash(f"Sorry, {v['name']} is out of stock.", "error")
    else:
        cart[str(v["id"])] = new_qty
        session["cart"] = cart
        flash(f"Added to cart ({v['name']}).", "success")
    if request.form.get("buy_now"):
        return redirect(url_for("checkout"))
    return redirect(request.referrer or url_for("index"))


@app.post("/cart/update/<int:vid>")
def cart_update(vid):
    cart = session.get("cart", {})
    qty = request.form.get("qty", 0, type=int)
    if qty <= 0:
        cart.pop(str(vid), None)
    else:
        v = query("SELECT stock FROM product_variants WHERE id = ?", (vid,), one=True)
        cart[str(vid)] = min(qty, v["stock"] if v else 0)
    session["cart"] = cart
    return redirect(url_for("cart"))


@app.route("/cart")
def cart():
    items, subtotal, shipping, total = cart_details()
    return render_template("cart.html", items=items, subtotal=subtotal, shipping=shipping,
                           total=total, free_over=FREE_SHIPPING_OVER)


# ---------------------------------------------------------------- checkout
@app.route("/checkout", methods=["GET", "POST"])
@login_required
def checkout():
    items, subtotal, shipping, total = cart_details()
    if not items:
        flash("Your cart is empty.", "info")
        return redirect(url_for("index"))
    addresses = query("SELECT * FROM addresses WHERE user_id = ? ORDER BY is_default DESC, id",
                      (g.user["id"],))
    ctx = dict(items=items, subtotal=subtotal, shipping=shipping, total=total,
               addresses=addresses, methods=PAYMENT_METHODS)

    if request.method == "GET":
        return render_template("checkout.html", **ctx)

    f = request.form
    method = f.get("payment", "COD")
    if method not in PAYMENT_METHODS:
        method = "COD"
    choice = f.get("address_id", "new")
    if choice != "new":
        a = query("SELECT * FROM addresses WHERE id = ? AND user_id = ?", (choice, g.user["id"]), one=True)
        if a is None:
            flash("Please choose a delivery address.", "error")
            return render_template("checkout.html", **ctx)
        ship = (a["name"], a["phone"], f"{a['line']}, {a['city']} – {a['pincode']}")
    else:
        err = validate_address(f)
        if err:
            flash(err, "error")
            return render_template("checkout.html", **ctx)
        ship = (f["name"].strip(), f["phone"].strip(),
                f"{f['line'].strip()}, {f['city'].strip()} – {f['pincode'].strip()}")
        if f.get("save_address"):
            save_address(f)

    db = get_db()
    try:
        db.execute("BEGIN")
        for i in items:   # reserve stock atomically – fails if someone else bought the last one
            cur = db.execute("UPDATE product_variants SET stock = stock - ? WHERE id = ? AND stock >= ?",
                             (i["qty"], i["v"]["variant_id"], i["qty"]))
            if cur.rowcount == 0:
                raise ValueError(f"{i['v']['name']} ({i['v']['variant_name']})")
        cur = db.execute(
            """INSERT INTO orders (user_id, subtotal, shipping, total, payment_method, payment_status,
                                   ship_name, ship_phone, ship_address)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (g.user["id"], subtotal, shipping, total, method,
             "Pay on delivery" if method == "COD" else "Pending", *ship))
        oid = cur.lastrowid
        db.executemany("""INSERT INTO order_items (order_id, product_id, variant_id, variant_name, quantity, unit_price)
                          VALUES (?, ?, ?, ?, ?, ?)""",
                       [(oid, i["v"]["product_id"], i["v"]["variant_id"], i["v"]["variant_name"],
                         i["qty"], i["v"]["price"]) for i in items])
        db.commit()
    except ValueError as e:
        db.rollback()
        flash(f"Not enough stock left for {e}. Please update your cart.", "error")
        return redirect(url_for("cart"))

    session["cart"] = {}
    if method == "COD":
        flash(f"Order #{oid} placed! Pay {inr(total)} when it arrives.", "success")
        return redirect(url_for("order_detail", oid=oid))
    return redirect(url_for("pay", oid=oid))


# ---------------------------------------------------------------- payment (demo gateway)
def luhn_ok(number):
    digits = [int(d) for d in number][::-1]
    total = sum(digits[0::2]) + sum(sum(divmod(2 * d, 10)) for d in digits[1::2])
    return total % 10 == 0


@app.route("/pay/<int:oid>", methods=["GET", "POST"])
@login_required
def pay(oid):
    order = query("SELECT * FROM orders WHERE id = ? AND user_id = ?", (oid, g.user["id"]), one=True)
    if order is None:
        abort(404)
    if order["payment_status"] != "Pending" or order["status"] == "Cancelled":
        return redirect(url_for("order_detail", oid=oid))

    method = request.values.get("method", order["payment_method"])
    if method not in ("UPI", "Card", "NetBanking"):
        method = "UPI"

    if request.method == "POST":
        f = request.form
        ref, error = None, None
        if method == "UPI":
            upi = f.get("upi_id", "").strip().lower()
            if not re.fullmatch(r"[a-z0-9.\-_]{2,}@[a-z]{2,}", upi):
                error = "Enter a valid UPI ID, e.g. name@okaxis"
            ref = upi
        elif method == "Card":
            num = re.sub(r"\D", "", f.get("card_number", ""))
            exp = f.get("expiry", "").strip()
            m = re.fullmatch(r"(\d{2})/(\d{2})", exp)
            today = date.today()
            if not (13 <= len(num) <= 19 and luhn_ok(num)):
                error = "That card number isn't valid."
            elif not m or not 1 <= int(m[1]) <= 12 or (2000 + int(m[2]), int(m[1])) < (today.year, today.month):
                error = "Enter a valid future expiry date as MM/YY."
            elif not re.fullmatch(r"\d{3,4}", f.get("cvv", "")):
                error = "Enter the 3 or 4 digit CVV."
            elif not f.get("card_name", "").strip():
                error = "Enter the name on the card."
            ref = f"Card •••• {num[-4:]}"
        else:
            bank = f.get("bank")
            if bank not in BANKS:
                error = "Choose your bank."
            ref = bank
        if error:
            flash(error, "error")
            return render_template("pay.html", order=order, method=method, banks=BANKS)

        txn = "GC" + secrets.token_hex(6).upper()
        db = get_db()
        db.execute("INSERT INTO payments (order_id, method, amount, status, reference) VALUES (?, ?, ?, 'Success', ?)",
                   (oid, method, order["total"], f"{ref} · {txn}"))
        db.execute("UPDATE orders SET payment_status = 'Paid', payment_method = ? WHERE id = ?", (method, oid))
        db.commit()
        flash(f"Payment of {inr(order['total'])} successful · Txn {txn}", "success")
        return redirect(url_for("order_detail", oid=oid))

    return render_template("pay.html", order=order, method=method, banks=BANKS)


# ---------------------------------------------------------------- orders
@app.route("/orders")
@login_required
def orders():
    rows = query("""SELECT o.*, SUM(oi.quantity) AS item_count FROM orders o
                    JOIN order_items oi ON oi.order_id = o.id
                    WHERE o.user_id = ? GROUP BY o.id ORDER BY o.id DESC""", (g.user["id"],))
    return render_template("orders.html", orders=rows)


@app.route("/orders/<int:oid>")
@login_required
def order_detail(oid):
    order = query("SELECT * FROM orders WHERE id = ?", (oid,), one=True)
    if order is None or (order["user_id"] != g.user["id"] and not g.user["is_admin"]):
        abort(404)
    items = query("""SELECT oi.*, p.name, c.icon, c.slug, v.color_hex FROM order_items oi
                     JOIN products p ON p.id = oi.product_id
                     JOIN categories c ON c.id = p.category_id
                     JOIN product_variants v ON v.id = oi.variant_id
                     WHERE oi.order_id = ?""", (oid,))
    payments = query("SELECT * FROM payments WHERE order_id = ? ORDER BY id", (oid,))
    return render_template("order_detail.html", order=order, items=items, payments=payments)


@app.post("/orders/<int:oid>/cancel")
@login_required
def cancel_order(oid):
    order = query("SELECT * FROM orders WHERE id = ? AND user_id = ?", (oid, g.user["id"]), one=True)
    if order is None or order["status"] != "Placed":
        flash("This order can no longer be cancelled.", "error")
        return redirect(url_for("orders"))
    db = get_db()
    for it in query("SELECT variant_id, quantity FROM order_items WHERE order_id = ?", (oid,)):
        db.execute("UPDATE product_variants SET stock = stock + ? WHERE id = ?", (it["quantity"], it["variant_id"]))
    new_pay = order["payment_status"]
    if order["payment_status"] == "Paid":
        new_pay = "Refunded"
        db.execute("INSERT INTO payments (order_id, method, amount, status, reference) VALUES (?, ?, ?, 'Refunded', ?)",
                   (oid, order["payment_method"], order["total"], "Refund to original payment method"))
    elif order["payment_status"] in ("Pending", "Pay on delivery"):
        new_pay = "Not charged"
    db.execute("UPDATE orders SET status = 'Cancelled', payment_status = ? WHERE id = ?", (new_pay, oid))
    db.commit()
    flash(f"Order #{oid} cancelled." + (" Your refund is on its way." if new_pay == "Refunded" else ""), "success")
    return redirect(url_for("order_detail", oid=oid))


# ---------------------------------------------------------------- customer account
def validate_address(f):
    if not all(f.get(k, "").strip() for k in ("name", "phone", "line", "city", "pincode")):
        return "Please fill in all address fields."
    if not re.fullmatch(r"[6-9]\d{9}", f["phone"].strip()):
        return "Enter a valid 10-digit Indian mobile number."
    if not re.fullmatch(r"[1-9]\d{5}", f["pincode"].strip()):
        return "Enter a valid 6-digit PIN code."
    return None


def save_address(f):
    first = not query("SELECT 1 FROM addresses WHERE user_id = ?", (g.user["id"],), one=True)
    execute("""INSERT INTO addresses (user_id, label, name, phone, line, city, pincode, is_default)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (g.user["id"], f.get("label", "Home") or "Home", f["name"].strip(), f["phone"].strip(),
             f["line"].strip(), f["city"].strip(), f["pincode"].strip(), int(first)))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    uid = g.user["id"]
    if request.method == "POST":
        f, action = request.form, request.form.get("action")
        if action == "profile":
            name, phone = f.get("name", "").strip(), f.get("phone", "").strip()
            if not name or (phone and not re.fullmatch(r"[6-9]\d{9}", phone)):
                flash("Enter your name and a valid 10-digit mobile number.", "error")
            else:
                execute("UPDATE users SET name = ?, phone = ? WHERE id = ?", (name, phone, uid))
                flash("Profile updated.", "success")
        elif action == "password":
            if not check_password_hash(g.user["password_hash"], f.get("current", "")):
                flash("Current password is wrong.", "error")
            elif len(f.get("new", "")) < 6:
                flash("New password must be at least 6 characters.", "error")
            else:
                execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(f["new"]), uid))
                flash("Password changed.", "success")
        elif action == "add_address":
            err = validate_address(f)
            if err:
                flash(err, "error")
            else:
                save_address(f)
                flash("Address saved.", "success")
        elif action == "delete_address":
            execute("DELETE FROM addresses WHERE id = ? AND user_id = ?", (f.get("id"), uid))
        elif action == "default_address":
            if query("SELECT 1 FROM addresses WHERE id = ? AND user_id = ?", (f.get("id"), uid), one=True):
                db = get_db()
                db.execute("UPDATE addresses SET is_default = (id = ?) WHERE user_id = ?", (f.get("id"), uid))
                db.commit()
        return redirect(url_for("account"))

    stats = query("""SELECT COUNT(*) AS orders, COALESCE(SUM(CASE WHEN status != 'Cancelled' THEN total END), 0) AS spent
                     FROM orders WHERE user_id = ?""", (uid,), one=True)
    addresses = query("SELECT * FROM addresses WHERE user_id = ? ORDER BY is_default DESC, id", (uid,))
    wish_count = query("SELECT COUNT(*) AS n FROM wishlist WHERE user_id = ?", (uid,), one=True)["n"]
    return render_template("account.html", stats=stats, addresses=addresses, wish_count=wish_count)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not name or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(password) < 6:
            flash("Enter a name, a valid email and a password of at least 6 characters.", "error")
        else:
            try:
                cur = execute("INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                              (name, email, generate_password_hash(password)))
                cart = session.get("cart", {})
                session.clear()
                session.update(user_id=cur.lastrowid, cart=cart)
                flash(f"Welcome to GlowCart, {name}! 🎉", "success")
                return redirect(url_for("index"))
            except sqlite3.IntegrityError:
                flash("An account with that email already exists.", "error")
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        u = query("SELECT * FROM users WHERE email = ?", (email,), one=True)
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            cart = session.get("cart", {})          # keep the guest cart after login
            session.clear()
            session.update(user_id=u["id"], cart=cart)
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))
        flash("Wrong email or password.", "error")
    return render_template("auth.html", mode="login")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ---------------------------------------------------------------- AI chat bot
@app.post("/api/chat")
def api_chat():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message", ""))[:500]
    history = data.get("history") if isinstance(data.get("history"), list) else []
    result = chatbot.answer(get_db(), message, g.user, history)
    db = get_db()
    uid = g.user["id"] if g.user else None
    db.executemany("INSERT INTO chat_messages (user_id, role, content) VALUES (?, ?, ?)",
                   [(uid, "user", message), (uid, "assistant", result["reply"])])
    db.commit()
    return jsonify(result)


# ---------------------------------------------------------------- real-time stock & prices
def change_payload(conn, rows):
    """Turn live_changes rows into JSON events with the product's new totals."""
    pids = sorted({r["product_id"] for r in rows})
    marks = ",".join("?" * len(pids))
    prods = {r["id"]: r for r in conn.execute(
        f"SELECT id, name, price, mrp, stock, variant_count FROM product_listing WHERE id IN ({marks})", pids)}
    vids = sorted({r["variant_id"] for r in rows})
    vnames = {r["id"]: r["name"] for r in conn.execute(
        f"SELECT id, name FROM product_variants WHERE id IN ({','.join('?' * len(vids))})", vids)}
    out = []
    for r in rows:
        p = prods.get(r["product_id"])
        if p is None:
            continue
        out.append({
            "id": r["id"], "kind": r["kind"], "at": r["created_at"],
            "variant": {"id": r["variant_id"], "name": vnames.get(r["variant_id"], ""), "stock": r["new_stock"],
                        "old_stock": r["old_stock"], "price": r["new_price"], "old_price": r["old_price"],
                        "mrp": r["new_mrp"]},
            "product": {"id": p["id"], "name": p["name"], "price": p["price"], "mrp": p["mrp"],
                        "stock": p["stock"], "variant_count": p["variant_count"]},
        })
    return out


@app.route("/api/live")
def live_stream():
    """Server-Sent Events: pushes every stock/price change to the browser as it happens.

    The browser reconnects automatically (EventSource) and resumes from Last-Event-ID,
    so each connection is kept short (default 5 minutes) to free the server thread.
    """
    since = request.headers.get("Last-Event-ID", type=int) or request.args.get("since", type=int)
    duration = min(request.args.get("duration", 300, type=float), 25 if ON_VERCEL else 600)  # browser reconnects

    def events():
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        last = since if since is not None else conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM live_changes").fetchone()[0]
        yield f"retry: 2000\nevent: hello\ndata: {json.dumps({'last': last})}\n\n"
        end, ping = time.time() + duration, time.time()
        try:
            while time.time() < end:
                rows = conn.execute("SELECT * FROM live_changes WHERE id > ? ORDER BY id LIMIT 200", (last,)).fetchall()
                if rows:
                    last = rows[-1]["id"]
                    for ev in change_payload(conn, rows):
                        yield f"id: {ev['id']}\nevent: change\ndata: {json.dumps(ev)}\n\n"
                elif time.time() - ping > 15:
                    ping = time.time()
                    yield ": ping\n\n"          # keeps proxies from closing the connection
                time.sleep(0.75)
        finally:
            conn.close()

    return Response(stream_with_context(events()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/stock")
def stock_snapshot():
    """Current price & stock for given product ids – e.g. /api/stock?ids=1,2,3"""
    ids = [int(i) for i in request.args.get("ids", "").split(",") if i.isdigit()][:200]
    if not ids:
        return jsonify({})
    marks = ",".join("?" * len(ids))
    rows = query(f"SELECT product_id, id, name, price, mrp, stock FROM product_variants WHERE product_id IN ({marks})", ids)
    out = {}
    for r in rows:
        out.setdefault(r["product_id"], []).append(dict(r))
    return jsonify(out)


# ---------------------------------------------------------------- admin
@app.route("/admin")
@admin_required
def admin():
    stats = query("""SELECT
        (SELECT COUNT(*) FROM products) AS products,
        (SELECT COUNT(*) FROM users WHERE is_admin = 0) AS customers,
        (SELECT COUNT(*) FROM orders) AS orders,
        (SELECT COALESCE(SUM(total), 0) FROM orders WHERE status != 'Cancelled') AS revenue,
        (SELECT COUNT(*) FROM chat_messages WHERE role = 'user') AS chats""", one=True)
    top = query("""SELECT p.name, SUM(oi.quantity) AS sold, SUM(oi.quantity * oi.unit_price) AS revenue
                   FROM order_items oi JOIN products p ON p.id = oi.product_id
                   JOIN orders o ON o.id = oi.order_id WHERE o.status != 'Cancelled'
                   GROUP BY p.id ORDER BY sold DESC LIMIT 5""")
    low_stock = query("""SELECT v.*, p.name AS product FROM product_variants v JOIN products p ON p.id = v.product_id
                         WHERE v.stock < 5 ORDER BY v.stock, p.name LIMIT 25""")
    recent = query("""SELECT o.*, u.name AS customer FROM orders o JOIN users u ON u.id = o.user_id
                      ORDER BY o.id DESC LIMIT 10""")
    brands = query("SELECT * FROM brands ORDER BY name")
    meta = {r["key"]: r["value"] for r in query("SELECT * FROM app_meta")}
    inv_q = request.args.get("inv", "").strip()
    inventory = []
    if inv_q:
        inventory = query("""SELECT v.*, p.name AS product, b.name AS brand FROM product_variants v
                             JOIN products p ON p.id = v.product_id JOIN brands b ON b.id = p.brand_id
                             WHERE p.name LIKE ? OR b.name LIKE ? OR v.name LIKE ?
                             ORDER BY p.name, v.price, v.id LIMIT 60""", [f"%{inv_q}%"] * 3)
    feed = query("""SELECT c.*, p.name AS product, v.name AS variant FROM live_changes c
                    JOIN products p ON p.id = c.product_id JOIN product_variants v ON v.id = c.variant_id
                    ORDER BY c.id DESC LIMIT 15""")
    amazon = {"provider": amazon_products.PROVIDER_LABELS.get(amazon_products.provider_name()),
              "count": query("SELECT COUNT(*) AS n FROM products WHERE source = 'amazon'", one=True)["n"],
              "searches": amazon_products.saved_searches(get_db()),
              "defaults": amazon_products.DEFAULT_SEARCHES,
              "refresh_minutes": AMAZON_REFRESH_MINUTES}
    return render_template("admin.html", stats=stats, top=top, low_stock=low_stock, recent=recent, brands=brands,
                           meta=meta, inv_q=inv_q, inventory=inventory, feed=feed, amazon=amazon)


def _amazon_summary(r):
    return (f"{r.get('products_added', 0)} new products, {r.get('products_updated', 0)} updated, "
            f"{r.get('prices_changed', 0)} price changes, {r.get('now_unavailable', 0)} now unavailable")


@app.post("/admin/amazon/import")
@admin_required
def admin_amazon_import():
    q = request.form.get("q", "").strip()
    slug = request.form.get("category") or None
    searches = [(q[:80], slug)] if q else amazon_products.DEFAULT_SEARCHES
    try:
        r = amazon_products.import_searches(get_db(), searches, log=lambda *_: None)
        flash(f"Amazon.in import: {_amazon_summary(r)}.", "success")
    except amazon_products.AmazonError as exc:
        get_db().rollback()
        flash(str(exc), "error")
    return redirect(url_for("admin"))


@app.post("/admin/amazon/refresh")
@admin_required
def admin_amazon_refresh():
    try:
        r = amazon_products.refresh(get_db(), log=lambda *_: None)
        execute("DELETE FROM app_meta WHERE key = 'amazon_error'")
        flash(f"Amazon.in prices refreshed: {_amazon_summary(r)}. Shoppers see the changes instantly.", "success")
    except amazon_products.AmazonError as exc:
        get_db().rollback()
        flash(str(exc), "error")
    return redirect(url_for("admin"))


@app.post("/admin/sync")
@admin_required
def admin_sync():
    try:
        r = import_products.sync(get_db(), offline=bool(request.form.get("offline")), verbose=False)
        flash(f"Catalogue synced from {r['sync_source']}: {r['products_added']} new products, "
              f"{r['shades_added']} new shades, {r['prices_changed']} price changes "
              f"(1 USD = ₹{r['usd_inr']}).", "success")
    except Exception as exc:
        get_db().rollback()
        flash(f"Sync failed: {exc}", "error")
    return redirect(url_for("admin"))


@app.post("/admin/variant/<int:vid>")
@admin_required
def admin_edit_variant(vid):
    price = request.form.get("price", type=int)
    mrp = request.form.get("mrp", type=int)
    stock = request.form.get("stock", type=int)
    if price is None or stock is None or price < 0 or stock < 0:
        flash("Enter a valid price and stock.", "error")
    else:
        execute("UPDATE product_variants SET price = ?, mrp = ?, stock = ? WHERE id = ?",
                (price, max(mrp or price, price), stock, vid))
        flash("Variety updated – shoppers see the change instantly.", "success")
    return redirect(request.referrer or url_for("admin"))


@app.post("/admin/product")
@admin_required
def admin_add_product():
    """Variants field: one per line → 'Name, price, mrp, stock[, #hex]'"""
    f = request.form
    try:
        variants = []
        for line in f.get("variants", "").splitlines():
            if not line.strip():
                continue
            parts = [x.strip() for x in line.split(",")]
            name, price, mrp, stock = parts[0], int(parts[1]), int(parts[2] or parts[1]), int(parts[3])
            hexcol = parts[4] if len(parts) > 4 and re.fullmatch(r"#[0-9a-fA-F]{6}", parts[4]) else None
            variants.append((f.get("kind", "Size"), name, hexcol, price, max(mrp, price), stock))
        if not variants or not f.get("name", "").strip():
            raise ValueError
        db = get_db()
        cur = db.execute("""INSERT INTO products (name, description, skin_type, tags, category_id, brand_id)
                            VALUES (?, ?, ?, ?, ?, ?)""",
                         (f["name"].strip(), f.get("description", "").strip(), f.get("skin_type", "All"),
                          f.get("tags", "").strip().lower(), int(f["category_id"]), int(f["brand_id"])))
        db.executemany("""INSERT INTO product_variants (product_id, kind, name, color_hex, price, mrp, stock)
                          VALUES (?, ?, ?, ?, ?, ?, ?)""", [(cur.lastrowid, *v) for v in variants])
        db.commit()
        flash(f"Product added with {len(variants)} variet{'y' if len(variants) == 1 else 'ies'}.", "success")
    except (KeyError, ValueError, IndexError, sqlite3.IntegrityError):
        get_db().rollback()
        flash("Couldn't add product – check the fields. Variants: one per line as 'Name, price, mrp, stock'.", "error")
    return redirect(url_for("admin"))


@app.post("/admin/restock/<int:vid>")
@admin_required
def admin_restock(vid):
    qty = request.form.get("qty", 0, type=int)
    if qty > 0:
        execute("UPDATE product_variants SET stock = stock + ? WHERE id = ?", (qty, vid))
        flash("Stock updated.", "success")
    return redirect(url_for("admin"))


@app.post("/admin/order/<int:oid>/status")
@admin_required
def admin_order_status(oid):
    status = request.form.get("status")
    if status in ("Placed", "Shipped", "Delivered"):
        execute("UPDATE orders SET status = ? WHERE id = ? AND status != 'Cancelled'", (status, oid))
        if status == "Delivered":   # cash collected on delivery
            execute("UPDATE orders SET payment_status = 'Paid' WHERE id = ? AND payment_method = 'COD'", (oid,))
    return redirect(url_for("admin"))


@app.errorhandler(404)
def not_found(_e):
    return render_template("message.html", title="Not found", text="We couldn't find that page."), 404


@app.errorhandler(403)
def forbidden(_e):
    return render_template("message.html", title="Access denied",
                           text="You don't have permission to view this page."), 403


def start_amazon_refresher():
    """Refresh Amazon prices every AMAZON_REFRESH_MINUTES (0 = off; each refresh uses API requests)."""
    if AMAZON_REFRESH_MINUTES > 0 and amazon_products.provider_name():
        import threading
        threading.Thread(target=amazon_products.refresh_loop, args=(DB_PATH, AMAZON_REFRESH_MINUTES),
                         daemon=True, name="amazon-refresh").start()
        print(f" * Amazon.in prices refresh every {AMAZON_REFRESH_MINUTES} min")


if __name__ == "__main__":
    if not DB_PATH.exists():
        import seed
        seed.seed()
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":  # only in the reloader's worker, not twice
        start_amazon_refresher()
    app.run(debug=True, threaded=True)
