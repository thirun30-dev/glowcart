-- GlowCart database schema (SQLite)
PRAGMA foreign_keys = ON;

DROP VIEW  IF EXISTS product_listing;
DROP TABLE IF EXISTS live_changes;
DROP TABLE IF EXISTS app_meta;
DROP TABLE IF EXISTS chat_messages;
DROP TABLE IF EXISTS payments;
DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS addresses;
DROP TABLE IF EXISTS reviews;
DROP TABLE IF EXISTS wishlist;
DROP TABLE IF EXISTS product_variants;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS brands;
DROP TABLE IF EXISTS categories;
DROP TABLE IF EXISTS users;

-- ---------------------------------------------------------------- customers
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    email         TEXT    NOT NULL UNIQUE,
    phone         TEXT    NOT NULL DEFAULT '',
    password_hash TEXT    NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE addresses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label       TEXT    NOT NULL DEFAULT 'Home',             -- Home / Work / Other
    name        TEXT    NOT NULL,
    phone       TEXT    NOT NULL,
    line        TEXT    NOT NULL,
    city        TEXT    NOT NULL,
    pincode     TEXT    NOT NULL,
    is_default  INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------- catalogue
CREATE TABLE categories (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL UNIQUE,
    slug  TEXT NOT NULL UNIQUE,
    icon  TEXT NOT NULL DEFAULT '✨'
);

CREATE TABLE brands (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL UNIQUE
);

CREATE TABLE products (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    description  TEXT    NOT NULL DEFAULT '',
    skin_type    TEXT    NOT NULL DEFAULT 'All',              -- All / Oily / Dry / Sensitive / Combination
    tags         TEXT    NOT NULL DEFAULT '',                 -- keywords the chatbot uses: "acne,pores"
    product_type TEXT    NOT NULL DEFAULT '',                 -- Lipstick, Foundation, Serum…
    category_id  INTEGER NOT NULL REFERENCES categories(id),
    brand_id     INTEGER NOT NULL REFERENCES brands(id),
    image_url    TEXT,
    source       TEXT    NOT NULL DEFAULT 'store',            -- store / makeup-api / amazon
    external_id  INTEGER UNIQUE,                              -- id in the Makeup API
    asin         TEXT    UNIQUE,                              -- Amazon product id (source = 'amazon')
    product_url  TEXT,                                        -- brand's product page / Amazon.in page
    external_rating REAL,                                     -- rating published by the API / Amazon
    external_rating_count INTEGER,                            -- number of Amazon ratings
    updated_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_products_category ON products(category_id);
CREATE INDEX idx_products_brand    ON products(brand_id);
CREATE INDEX idx_products_type     ON products(product_type);

-- Each product has one or more varieties: shades (with a colour) or sizes.
CREATE TABLE product_variants (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL DEFAULT 'Size',              -- Shade / Size
    name        TEXT    NOT NULL,                             -- "Ruby Red", "50 ml"
    color_hex   TEXT,                                         -- for shade swatches
    price       INTEGER NOT NULL CHECK (price >= 0),          -- rupees
    mrp         INTEGER NOT NULL CHECK (mrp >= price),
    stock       INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
    UNIQUE (product_id, name)
);
CREATE INDEX idx_variants_product ON product_variants(product_id);

CREATE TABLE reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
    rating      INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment     TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (product_id, user_id)
);

CREATE TABLE wishlist (
    user_id     INTEGER NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, product_id)
);

-- ---------------------------------------------------------------- orders & payments
CREATE TABLE orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    subtotal       INTEGER NOT NULL,
    shipping       INTEGER NOT NULL DEFAULT 0,
    total          INTEGER NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'Placed',         -- Placed / Shipped / Delivered / Cancelled
    payment_method TEXT    NOT NULL DEFAULT 'COD',            -- COD / UPI / Card / NetBanking
    payment_status TEXT    NOT NULL DEFAULT 'Pending',        -- Pending / Paid / Refunded / Pay on delivery
    ship_name      TEXT    NOT NULL,
    ship_phone     TEXT    NOT NULL,
    ship_address   TEXT    NOT NULL,
    created_at     TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE order_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id   INTEGER NOT NULL REFERENCES products(id),
    variant_id   INTEGER NOT NULL REFERENCES product_variants(id),
    variant_name TEXT    NOT NULL,
    quantity     INTEGER NOT NULL CHECK (quantity > 0),
    unit_price   INTEGER NOT NULL                             -- price at time of purchase
);

CREATE TABLE payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    method      TEXT    NOT NULL,
    amount      INTEGER NOT NULL,
    status      TEXT    NOT NULL,                             -- Success / Failed / Refunded
    reference   TEXT    NOT NULL,                             -- txn id, masked card, UPI id
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------- AI chat log
CREATE TABLE chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    role        TEXT    NOT NULL,                             -- user / assistant
    content     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------- real-time change log
-- Every stock or price change on a variety is written here by triggers.
-- The /api/live stream reads new rows and pushes them to every open browser.
CREATE TABLE live_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id  INTEGER NOT NULL,
    product_id  INTEGER NOT NULL,
    kind        TEXT    NOT NULL,                             -- sale / restock / price / new
    old_stock   INTEGER, new_stock INTEGER,
    old_price   INTEGER, new_price INTEGER, new_mrp INTEGER,
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TRIGGER trg_variant_changed AFTER UPDATE OF stock, price, mrp ON product_variants
WHEN OLD.stock IS NOT NEW.stock OR OLD.price IS NOT NEW.price OR OLD.mrp IS NOT NEW.mrp
BEGIN
    INSERT INTO live_changes (variant_id, product_id, kind, old_stock, new_stock, old_price, new_price, new_mrp)
    VALUES (NEW.id, NEW.product_id,
            CASE WHEN OLD.price IS NOT NEW.price OR OLD.mrp IS NOT NEW.mrp THEN 'price'
                 WHEN NEW.stock < OLD.stock THEN 'sale' ELSE 'restock' END,
            OLD.stock, NEW.stock, OLD.price, NEW.price, NEW.mrp);
END;

CREATE TRIGGER trg_variant_added AFTER INSERT ON product_variants
BEGIN
    INSERT INTO live_changes (variant_id, product_id, kind, new_stock, new_price, new_mrp)
    VALUES (NEW.id, NEW.product_id, 'new', NEW.stock, NEW.price, NEW.mrp);
END;

-- Small key/value store: last catalogue sync time, exchange rate used, etc.
CREATE TABLE app_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- ---------------------------------------------------------------- listing view
-- One row per product: cheapest variety's price/MRP, total stock, rating summary.
CREATE VIEW product_listing AS
SELECT p.*,
       b.name AS brand,
       c.name AS category, c.slug AS category_slug, c.icon AS icon,
       cheap.id    AS default_variant_id,
       cheap.price AS price,
       cheap.mrp   AS mrp,
       cheap.kind  AS variant_kind,
       (SELECT COALESCE(SUM(stock), 0) FROM product_variants WHERE product_id = p.id) AS stock,
       (SELECT COUNT(*) FROM product_variants WHERE product_id = p.id)                AS variant_count,
       (SELECT COALESCE(ROUND(AVG(rating), 1), 0) FROM reviews WHERE product_id = p.id) AS avg_rating,
       (SELECT COUNT(*) FROM reviews WHERE product_id = p.id)                         AS review_count
FROM products p
JOIN brands b     ON b.id = p.brand_id
JOIN categories c ON c.id = p.category_id
JOIN product_variants cheap ON cheap.id = (
     SELECT id FROM product_variants WHERE product_id = p.id ORDER BY price, id LIMIT 1);
