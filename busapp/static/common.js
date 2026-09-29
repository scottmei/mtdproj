"use strict";
// Shared by every page: DOM helpers and the header's stop search.

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "style") n.style.cssText = v;
    else n.setAttribute(k, v);
  }
  for (const c of children) if (c != null) n.append(c);
  return n;
}

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

// "the 7 days before Tue, Sep 29": scores cover complete service days only
function completeDays(days, untilTs) {
  if (untilTs == null) return `last ${days} days`;
  const end = new Date(untilTs * 1000).toLocaleDateString("en-US",
    { timeZone: "America/Chicago", weekday: "short", month: "short", day: "numeric" });
  return `the ${days} service day${days === 1 ? "" : "s"} before ${end}`;
}

// ---------- header stop search ----------
(function initSearch() {
  const input = $("q"), list = $("suggest");
  if (!input || !list) return;
  let suggestions = [], active = -1, timer = null, seq = 0;

  function hide() { list.hidden = true; suggestions = []; active = -1; }

  function show(results) {
    suggestions = results;
    active = results.length ? 0 : -1;
    list.replaceChildren(...(results.length
      ? results.map((r, i) =>
          el("li", { role: "option", "aria-selected": String(i === 0), "data-i": i },
            el("span", {}, r.name), el("span", { class: "detail" }, r.detail || r.id)))
      : [el("li", { class: "muted" }, "No matching stops")]));
    list.hidden = false;
  }

  function highlight(i) {
    active = i;
    [...list.children].forEach((li, j) => li.setAttribute("aria-selected", String(j === i)));
  }

  function choose(stop) {
    hide();
    input.value = stop.name;
    const hash = encodeURIComponent(stop.id);
    if (location.pathname === "/") location.hash = hash;   // arrivals page: just switch stop
    else location.href = `/#${hash}`;                      // other pages: open the arrivals board
  }

  async function run(q) {
    const mine = ++seq;
    try {
      const { results } = await getJSON(`/api/stops/search?q=${encodeURIComponent(q)}`);
      if (mine === seq) show(results);   // ignore responses a newer search superseded
    } catch (err) {
      console.error(err);
    }
  }

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q) return hide();
    timer = setTimeout(() => run(q), 200);
  });
  input.addEventListener("keydown", (e) => {
    const n = suggestions.length;
    if (e.key === "ArrowDown" && n) { e.preventDefault(); highlight((active + 1) % n); }
    else if (e.key === "ArrowUp" && n) { e.preventDefault(); highlight((active - 1 + n) % n); }
    else if (e.key === "Enter" && active >= 0) { e.preventDefault(); choose(suggestions[active]); }
    else if (e.key === "Escape") hide();
  });
  input.addEventListener("blur", () => setTimeout(hide, 150));
  list.addEventListener("mousedown", (e) => {
    const li = e.target.closest("li[data-i]");
    if (li) choose(suggestions[+li.dataset.i]);
  });
})();
