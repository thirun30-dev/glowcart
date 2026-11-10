// GlowBot chat widget
(function () {
  const fab = document.getElementById("chat-fab");
  const panel = document.getElementById("chat-panel");
  const log = document.getElementById("chat-log");
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  if (!fab) return;

  let history = [];
  try { history = JSON.parse(sessionStorage.getItem("glowbot") || "[]"); } catch (e) {}

  const esc = s => s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = s => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\*(.+?)\*/g, "<i>$1</i>").replace(/\n/g, "<br>");

  function bubble(role, text, products) {
    const div = document.createElement("div");
    div.className = "msg " + role;
    div.innerHTML = fmt(text);
    if (products && products.length) {
      const wrap = document.createElement("div");
      wrap.className = "chat-products";
      products.forEach(p => {
        const a = document.createElement("a");
        a.href = p.url;
        a.className = "chat-prod";
        a.innerHTML = `<span class="ci">${p.icon}</span><span><b>${esc(p.name)}</b><br>₹${p.price}` +
          (p.mrp > p.price ? ` <s>₹${p.mrp}</s>` : "") + (p.rating ? ` · ★${p.rating}` : "") + `</span>`;
        wrap.appendChild(a);
      });
      div.appendChild(wrap);
    }
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    return div;
  }

  function restore() {
    log.innerHTML = "";
    bubble("assistant", "Hi! I'm GlowBot 💖 Ask me for product suggestions, shades, prices or your order status.");
    history.forEach(h => bubble(h.role, h.content, h.products));
  }

  async function send(text) {
    if (!text.trim()) return;
    bubble("user", text);
    const typing = bubble("assistant typing", "…");
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, history: history.map(({ role, content }) => ({ role, content })) })
      });
      const data = await res.json();
      typing.remove();
      bubble("assistant", data.reply, data.products);
      history.push({ role: "user", content: text }, { role: "assistant", content: data.reply, products: data.products });
      history = history.slice(-20);
      try { sessionStorage.setItem("glowbot", JSON.stringify(history)); } catch (e) {}
    } catch (e) {
      typing.remove();
      bubble("assistant", "Sorry, I couldn't connect. Please try again.");
    }
  }

  fab.addEventListener("click", () => {
    panel.classList.toggle("open");
    if (panel.classList.contains("open")) { if (!log.children.length) restore(); input.focus(); }
  });
  document.getElementById("chat-close").addEventListener("click", () => panel.classList.remove("open"));
  form.addEventListener("submit", e => { e.preventDefault(); const t = input.value; input.value = ""; send(t); });
  document.querySelectorAll(".chip-q").forEach(b => b.addEventListener("click", () => send(b.textContent)));
})();

// Splash screen: show the logo once per browser session
(function () {
  const s = document.getElementById("splash");
  if (!s) return;
  let seen = false;
  try { seen = sessionStorage.getItem("splashSeen"); sessionStorage.setItem("splashSeen", "1"); } catch (e) {}
  if (seen) { s.remove(); return; }
  s.classList.add("show");
  setTimeout(() => { s.classList.add("hide"); setTimeout(() => s.remove(), 600); }, 1800);
})();

// Product page: switch variety without reloading
(function () {
  const opts = document.querySelectorAll("input[name=variant_id]");
  if (!opts.length) return;
  const price = document.getElementById("v-price"), mrp = document.getElementById("v-mrp"),
        off = document.getElementById("v-off"), stock = document.getElementById("v-stock"),
        label = document.getElementById("v-label"), thumb = document.getElementById("v-thumb"),
        btns = document.querySelectorAll(".buy button"), qty = document.getElementById("v-qty");
  const inr = n => "₹" + Number(n).toLocaleString("en-IN");
  function update(el) {
    const d = el.dataset, p = +d.price, m = +d.mrp, s = +d.stock;
    price.textContent = inr(p);
    mrp.textContent = m > p ? "M.R.P. " + inr(m) : "";
    off.textContent = m > p ? Math.floor((m - p) * 100 / m) + "% off" : "";
    label.textContent = d.name;
    stock.textContent = s === 0 ? "Out of stock" : s < 10 ? `Only ${s} left in stock` : "In stock";
    stock.className = s === 0 ? "oos" : s < 10 ? "instock low" : "instock";
    btns.forEach(b => b.disabled = s === 0);
    if (qty) {
      qty.innerHTML = "";
      for (let i = 1; i <= Math.max(1, Math.min(s, 10)); i++) qty.add(new Option(i, i));
    }
    if (thumb) thumb.style.setProperty("--shade", d.color || "transparent");
    history.replaceState(null, "", "?v=" + el.value);
  }
  opts.forEach(o => o.addEventListener("change", () => update(o)));
  const checked = document.querySelector("input[name=variant_id]:checked");
  if (checked) update(checked);
})();
