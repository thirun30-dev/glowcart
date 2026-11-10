# GlowCart – Amazon-style cosmetics store (Python + SQL)

A working e-commerce web app built with **Flask** (Python) and **SQLite** (SQL), with **real products** and **real-time updates**.

## What's new in v3
### Real product data
- **460 products:** 438 real products from the Makeup API, plus GlowCart's 22 own-brand products for skincare, haircare and fragrance
- **50 real brands**, including NYX, Maybelline, L'Oréal, Dior, Clinique, e.l.f., Revlon, CoverGirl, Smashbox and Glossier
- **Over 3,000 real shades**, each with its real colour code, shown as swatches. Each shade is its own variety, with its own price and stock
- Real product names, descriptions, ratings, photos and links to the brand's product page
- Prices are converted from USD, GBP and CAD to ₹ at the day's exchange rate and rounded the Indian retail way (₹949, ₹1,299)
- New filters for **brand** and **product type** (Lipstick, Foundation, Eyeliner…), and 24 products per page

### Real-time updates
- When a customer buys, the admin changes a price or stock, or the catalogue syncs, **every open page updates instantly**, with no refresh:
  - prices, MRP and % off
  - "Only 3 left", "Out of stock", sold-out shades, and the Add to cart button
  - cart warnings ("Only 2 left now, so update your quantity")
  - pop-up notices ("🛍️ Someone just bought…", "💸 Price drop…", "✅ Back in stock…")
  - a **Selling right now** row on the home page and a **Live activity** feed for the admin
- How it works: **SQL triggers** on `product_variants` write every change to `live_changes`, and `/api/live` streams the new rows to browsers using **Server-Sent Events**. Because it runs through the database, changes made by any program show up, including the simulator and the importer
- The green ● LIVE badge in the header shows the connection is up. The browser reconnects automatically and catches up on anything it missed

### Real products from Amazon.in
- Imports **real Amazon.in products**: real titles, brands, photos, ratings, and live ₹ price and M.R.P.
- They are sold through the normal GlowCart cart and checkout, and appear in a **Live from Amazon.in** row on the home page
- **Refresh Amazon prices** on the admin dashboard, or turn on auto-refresh. Price changes reach open pages instantly through the same live system
- Amazon publishes no stock counts, so stock is demo inventory. It follows Amazon's "Only 3 left" and goes to 0 when Amazon says "Currently unavailable"
- Works with either provider:
  1. **RapidAPI "Real-Time Amazon Data"**: free tier, set `RAPIDAPI_KEY`
  2. **Amazon Product Advertising API 5.0**: official, needs an approved Amazon Associates account; set `AMAZON_PAAPI_ACCESS_KEY`, `AMAZON_PAAPI_SECRET_KEY` and `AMAZON_PARTNER_TAG`

```bash
copy .env.example .env               # then put your key in .env
python amazon_products.py            # import the default beauty searches (lipstick, serum, shampoo…)
python amazon_products.py "kajal"    # import a search of your own
python amazon_products.py --refresh  # update prices and availability
```
Admins can also import and refresh from the dashboard. To refresh automatically while the app runs, set `AMAZON_REFRESH_MINUTES=30` in `.env`. On RapidAPI each refresh uses one request per saved search, so keep an eye on your monthly quota.

## Run it
```bash
pip install -r requirements.txt
python app.py                    # open http://127.0.0.1:5000
```
On first run it builds the database from the bundled real-product snapshot, so no internet is needed.

**Watch the live updates:** open the site, then in a second terminal run
```bash
python simulate_shoppers.py      # random purchases, restocks and flash sales (add --fast for more)
```

**Get the newest real data and exchange rate** (needs internet):
```bash
python import_products.py        # sync: adds new products and shades, refreshes prices, keeps your stock
python seed.py --live            # or: reset the whole store using fresh API data
```
Admins can also click **Sync now** on the dashboard.

**Logins:** shopper `demo@glowcart.com` / `demo123` · admin `admin@glowcart.com` / `admin123`  
**Test card:** `4111 1111 1111 1111`, any future expiry · **Test UPI:** `name@okaxis`

## Data notes
- Source: [Makeup API](https://makeup-api.herokuapp.com), a free public cosmetics database. `data/makeup_api_snapshot.json` is a copy of 472 of its records, so the app works offline.
- The API has no stock levels and no Indian MRPs. Stock is generated demo inventory for your store, and imported products show their converted price without an invented discount.
- Exchange rate: live from open.er-api.com when you sync, otherwise ₹94 per US dollar (Nov 2026).
- Product photos come from the API's image links, so some may be missing. When a photo doesn't load, the card shows a category icon instead.

## Features
Start screen with logo · search · category, type, brand, skin and price filters · product pages with shade swatches ·
wishlist · cart · checkout with saved addresses · COD, UPI, card and net-banking payments (demo) · order tracking and cancellation with refunds ·
reviews · customer account · **GlowBot AI chatbot** (understands brands, product types, concerns and budgets; uses Claude when `ANTHROPIC_API_KEY` is set) ·
admin dashboard with live activity, price and stock editing, catalogue sync, and order management.

## Project layout
```
app.py                routes: shop, cart, checkout, payment, account, chat, live stream, admin
import_products.py    real-product importer and sync (Makeup API + exchange rate)
amazon_products.py    Amazon.in importer and price refresh (RapidAPI or PA-API 5.0)
simulate_shoppers.py  generates live store activity for demos
chatbot.py            GlowBot (built-in assistant + Claude)
schema.sql            tables, triggers, listing view
seed.py               store setup and sample data
data/                 offline snapshot of real products
templates/, static/   pages, styles, chat.js, live.js, logo.svg
```

## Deploy on Vercel
1. Push this folder to a GitHub repository.
2. On [vercel.com](https://vercel.com), choose **Add New → Project** and import the repository. Vercel detects Flask from `app.py` and `requirements.txt`, so you don't need any build settings.
3. Under **Environment Variables**, add `GLOWCART_SECRET`, a long random string. You can make one with `python -c "import secrets; print(secrets.token_hex(32))"`. Optionally add `RAPIDAPI_KEY` (or the PA-API keys) and `ANTHROPIC_API_KEY`.
4. Click **Deploy**.

**How it behaves on Vercel.** Vercel runs the app as serverless functions, and only `/tmp` can be written to:
- Each new server instance builds a fresh store from the bundled snapshot in under a second. **New accounts, orders, carts, reviews, admin edits and imported Amazon products are temporary.** They disappear when Vercel recycles the instance, which can happen after a few minutes of no traffic. When several instances run at once, each one has its own copy of the data.
- Live updates work within one instance. The stream reconnects about every 25 seconds.
- Automatic Amazon refresh is off. Use the admin buttons instead.

That's fine for a demo or a portfolio link. For a store that keeps its data, host it somewhere with a persistent disk, such as Render with a disk, Railway, or a VPS. Another option is to move the database to a hosted service such as Turso or Neon Postgres.

## Before going live
- Set `GLOWCART_SECRET`, turn off debug, and add CSRF protection (Flask-WTF)
- Run with a server that handles streaming, such as `gunicorn -k gevent -w 1 app:app`, or put Redis pub/sub behind `/api/live`
- Swap the demo payment page for Razorpay or Stripe
- Move to PostgreSQL for heavy traffic. Its `LISTEN/NOTIFY` replaces the change-log polling
