// GlowCart real-time updates: listens to /api/live (Server-Sent Events) and patches
// prices, stock and "Only X left" messages on whatever page is open, without reloading.
(function () {
  if (!("EventSource" in window)) return;
  const badge = document.getElementById("live-badge");
  const inr = n => "₹" + Number(n).toLocaleString("en-IN");
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const flash = el => { el.classList.remove("lv-flash"); void el.offsetWidth; el.classList.add("lv-flash"); };

  // ---- toasts (throttled so a busy store doesn't spam the shopper)
  const queue = [];
  let showing = false;
  function toast(html, href) {
    if (queue.length > 3) return;
    queue.push({ html, href });
    if (!showing) nextToast();
  }
  function nextToast() {
    const t = queue.shift();
    if (!t) { showing = false; return; }
    showing = true;
    const box = document.getElementById("toasts");
    const el = document.createElement(t.href ? "a" : "div");
    el.className = "toast";
    if (t.href) el.href = t.href;
    el.innerHTML = t.html;
    box.appendChild(el);
    setTimeout(() => el.classList.add("out"), 3200);
    setTimeout(() => { el.remove(); nextToast(); }, 3600);
  }

  // ---- product cards and product page header (product-level totals)
  function updateProduct(p) {
    document.querySelectorAll(`[data-live-product="${p.id}"]`).forEach(card => {
      const price = card.querySelector(".lp-price"), mrp = card.querySelector(".lp-mrp"),
            stock = card.querySelector(".lp-stock"), off = card.querySelector(".lp-off");
      if (price && price.textContent !== inr(p.price)) { price.textContent = inr(p.price); flash(price); }
      if (mrp) mrp.textContent = p.mrp > p.price ? inr(p.mrp) : "";
      if (off) off.textContent = p.mrp > p.price ? Math.floor((p.mrp - p.price) * 100 / p.mrp) + "% off" : "";
      if (off) off.style.display = p.mrp > p.price ? "" : "none";
      if (stock) {
        stock.textContent = p.stock === 0 ? "Out of stock" : p.stock < 10 ? `Only ${p.stock} left` : "";
        stock.className = "lp-stock small " + (p.stock === 0 ? "oos" : p.stock < 10 ? "low" : "");
        flash(stock);
      }
      const btn = card.querySelector(".lp-action button");
      if (btn) btn.disabled = p.stock === 0;
    });
  }

  // ---- one variety (shade/size): product page swatches, cart rows, admin inventory
  function updateVariant(v) {
    document.querySelectorAll(`[data-live-variant="${v.id}"]`).forEach(el => {
      const input = el.querySelector("input[name=variant_id]");
      if (input) {                                   // product page swatch
        input.dataset.stock = v.stock; input.dataset.price = v.price; input.dataset.mrp = v.mrp;
        el.classList.toggle("sold", v.stock === 0);
        el.title = input.dataset.name + (v.stock === 0 ? " – sold out" : "");
        if (input.checked) input.dispatchEvent(new Event("change"));
        flash(el);
      }
      if (el.classList.contains("cart-row")) {       // cart: warn if fewer left than you want
        const qty = +el.dataset.qty, warn = el.querySelector(".lv-warn"), box = el.querySelector("input[name=qty]");
        if (box) box.max = v.stock;
        if (warn) {
          warn.textContent = v.stock === 0 ? "⚠️ Just sold out" : v.stock < qty ? `⚠️ Only ${v.stock} left now, so update your quantity` :
                             v.stock < 10 ? `Hurry, only ${v.stock} left` : "";
          warn.className = "lv-warn small " + (v.stock < qty ? "oos" : "low");
        }
        flash(el);
      }
      const s = el.querySelector(".lv-stock-input");   // admin inventory
      if (s && document.activeElement !== s) { s.value = v.stock; flash(s); }
      const pr = el.querySelector("input[name=price]");
      if (pr && document.activeElement !== pr) pr.value = v.price;
    });
  }

  function addToSellingNow(ev) {
    const rail = document.getElementById("selling-now");
    if (!rail) return;
    rail.querySelector(".empty-rail")?.remove();
    if (rail.querySelector(`[data-live-product="${ev.product.id}"]`)) return;
    const a = document.createElement("a");
    a.className = "card mini lv-flash";
    a.href = "/product/" + ev.product.id;
    a.dataset.liveProduct = ev.product.id;
    a.innerHTML = `<span class="mini-tag">Just sold</span><b>${esc(ev.product.name)}</b>
      <span class="price"><span class="lp-price">${inr(ev.product.price)}</span></span>
      <span class="lp-stock small"></span>`;
    rail.prepend(a);
  }

  function addToAdminFeed(ev) {
    const feed = document.getElementById("admin-feed");
    if (!feed) return;
    feed.querySelector(".empty")?.remove();
    const li = document.createElement("li");
    li.className = "k-" + ev.kind + " lv-flash";
    const v = ev.variant;
    const detail = ev.kind === "price" ? `${inr(v.old_price)} → ${inr(v.price)}` : `stock ${v.old_stock ?? "–"} → ${v.stock}`;
    li.innerHTML = `<b>${ev.kind[0].toUpperCase() + ev.kind.slice(1)}</b> · ${esc(ev.product.name)} (${esc(v.name)}) ${detail}
      <small class="muted">${esc((ev.at || "").slice(11))}</small>`;
    feed.prepend(li);
    while (feed.children.length > 25) feed.lastChild.remove();
  }

  function onChange(ev) {
    updateProduct(ev.product);
    updateVariant(ev.variant);
    addToAdminFeed(ev);
    const link = "/product/" + ev.product.id + "?v=" + ev.variant.id;
    const label = `<b>${esc(ev.product.name)}</b>${ev.product.variant_count > 1 ? " · " + esc(ev.variant.name) : ""}`;
    if (ev.kind === "sale") {
      addToSellingNow(ev);
      toast(`🛍️ Someone just bought ${label}`, link);
    } else if (ev.kind === "price" && ev.variant.old_price && ev.variant.price < ev.variant.old_price) {
      toast(`💸 Price drop: ${label} now <b>${inr(ev.variant.price)}</b>`, link);
    } else if (ev.kind === "restock" && ev.variant.old_stock === 0) {
      toast(`✅ Back in stock: ${label}`, link);
    }
  }

  let es;
  function connect() {
    es = new EventSource("/api/live");
    es.addEventListener("hello", () => badge && badge.classList.add("on"));
    es.addEventListener("change", e => { try { onChange(JSON.parse(e.data)); } catch (err) { console.error(err); } });
    es.onerror = () => badge && badge.classList.remove("on");   // EventSource reconnects by itself
  }
  connect();
  window.addEventListener("pagehide", () => es && es.close());
})();
