"use strict";
// Insights page. Needs common.js ($, el, header search).

const state = {
  by: "line",
  data: null,
  sortKey: null,     // null = server order (lateness, or natural order for hour/day/day of week)
  sortDir: -1,
  seq: 0,
};

const mins = (s, digits = 1) => (s == null ? "—" : `${(s / 60).toFixed(digits)}`);
const signedMin = (s) => (s == null ? "—" : `${s >= 0 ? "+" : "−"}${Math.abs(s / 60).toFixed(1)} min`);
const pct = (p) => `${p.toFixed(0)}%`;

// ---------- loading ----------
async function load() {
  const seq = ++state.seq;
  $("wrap").classList.add("loading");   // keep the previous table while refetching
  const params = new URLSearchParams({
    by: state.by, days: $("days").value, horizon: $("horizon").value,
    min_n: Math.max(1, +$("min-n").value || 1),
  });
  if ($("line").value) params.set("line", $("line").value);
  try {
    const r = await fetch(`/api/breakdown?${params}`);
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    const data = await r.json();
    if (seq !== state.seq) return;       // a newer request superseded this one
    state.data = data;
    fillLines(data.lines ?? []);   // absent from an older server: keep the page working
    render();
  } catch (err) {
    console.error(err);
    $("summary").textContent = `Couldn't load the breakdown: ${err.message}`;
  } finally {
    if (seq === state.seq) $("wrap").classList.remove("loading");
  }
}

function fillLines(lines) {
  const sel = $("line"), current = sel.value;
  const opts = [...new Set([...lines, ...(current ? [current] : [])])].sort();
  sel.replaceChildren(el("option", { value: "" }, "All lines"),
    ...opts.map((l) => el("option", { value: l }, l)));
  sel.value = current;
}

// ---------- rendering ----------
function sortedGroups() {
  const groups = [...state.data.groups];
  if (!state.sortKey) return groups;
  const k = state.sortKey;
  return groups.sort((a, b) => {
    const x = a[k], y = b[k];
    if (x == null && y == null) return 0;
    if (x == null) return 1;               // missing values always last
    if (y == null) return -1;
    return (typeof x === "string" ? x.localeCompare(y) : x - y) * state.sortDir;
  });
}

function scaleFor(groups) {
  // one shared scale for every bar, always including zero
  let lo = 0, hi = 0;
  for (const g of groups) {
    const ci = g.ci95_s ?? 0;
    lo = Math.min(lo, g.mean_delay_s - ci);
    hi = Math.max(hi, g.mean_delay_s + ci);
  }
  const span = hi - lo || 1;
  return (v) => ((v - lo) / span) * 100;   // percent across the track
}

function delayBar(g, x) {
  const zero = x(0), end = x(g.mean_delay_s);
  const late = g.mean_delay_s >= 0;
  const bar = el("span", {
    class: `dbar ${late ? "late-bar" : "early-bar"}`,
    style: `left:${Math.min(zero, end)}%;width:${Math.abs(end - zero)}%`,
  });
  const parts = [el("span", { class: "zero", style: `left:${zero}%` }), bar];
  if (g.ci95_s != null) {
    const a = x(g.mean_delay_s - g.ci95_s), b = x(g.mean_delay_s + g.ci95_s);
    parts.push(el("span", { class: "ci", style: `left:${a}%;width:${b - a}%` }));
  }
  return el("span", { class: "track" }, ...parts);
}

function render() {
  const d = state.data;
  const groups = sortedGroups();
  const x = scaleFor(groups);
  const hidden = d.groups_hidden
    ? ` · ${d.groups_hidden} more hidden (fewer than ${d.min_n} departures)` : "";
  const scope = d.line ? ` on ${d.line}` : "";
  $("summary").textContent =
    `${d.departures.toLocaleString()} observed departures${scope} in the last ${d.days} day(s) · ` +
    `${d.groups_shown} group(s) shown${hidden}. Accuracy columns compare against MTD's estimate ` +
    `from ${d.horizon_min} min ahead.`;

  $("rows").replaceChildren(...groups.map((g, i) => {
    const maes = [g.schedule_mae_s, g.mtd_mae_s, g.ours_mae_s].filter((v) => v != null);
    const best = maes.length ? Math.min(...maes) : null;
    const acc = (v) => el("td", { class: `num acc${v != null && v === best ? " best" : ""}` },
      v == null ? "—" : `${mins(v)} min`);
    const label = d.by === "stop"
      ? el("a", { href: `/#${encodeURIComponent(g.key)}`, title: "Open this stop's arrivals" }, g.label)
      : g.label;
    return el("tr", {},
      el("td", {},
        g.color ? el("span", { class: "swatch", style: `background:#${g.color}` }) : null,
        label),
      el("td", { class: "num" }, g.n.toLocaleString(),
        el("span", { class: "sub" }, `${g.trips.toLocaleString()} trips`)),
      el("td", { class: "bar-col", tabindex: "0", "data-i": i },
        el("span", { class: "bar-value" }, signedMin(g.mean_delay_s),
          g.ci95_s != null ? el("span", { class: "sub-inline" }, ` ±${mins(g.ci95_s)}`) : null),
        delayBar(g, x)),
      el("td", { class: "num" }, signedMin(g.median_delay_s)),
      el("td", { class: "num" }, pct(g.pct_late_5min)),
      el("td", { class: "num" }, pct(g.pct_early_1min)),
      acc(g.schedule_mae_s), acc(g.mtd_mae_s), acc(g.ours_mae_s));
  }));
  state.rendered = groups;
  updateSortHeaders();
}

// ---------- tooltip (enhances; every value is also in the table) ----------
function showTip(cell) {
  const g = state.rendered[+cell.dataset.i];
  const tip = $("tooltip");
  const ci = g.ci95_s != null
    ? `95% CI ${signedMin(g.mean_delay_s - g.ci95_s)} to ${signedMin(g.mean_delay_s + g.ci95_s)}`
    : "Not enough trips for a confidence interval";
  tip.replaceChildren(
    el("strong", {}, signedMin(g.mean_delay_s)),
    el("span", {}, g.label),
    el("span", {}, ci),
    el("span", {}, `${g.n.toLocaleString()} departures on ${g.trips.toLocaleString()} trips, ` +
      `${g.service_days} service day${g.service_days === 1 ? "" : "s"}`));
  tip.hidden = false;
  const r = cell.getBoundingClientRect();
  const w = tip.offsetWidth;
  tip.style.left = `${Math.min(window.scrollX + r.left, window.scrollX + document.documentElement.clientWidth - w - 16)}px`;
  tip.style.top = `${window.scrollY + r.bottom + 6}px`;
}
const hideTip = () => { $("tooltip").hidden = true; };
$("rows").addEventListener("pointerover", (e) => { const c = e.target.closest(".bar-col"); if (c) showTip(c); });
$("rows").addEventListener("pointerout", (e) => { if (e.target.closest(".bar-col")) hideTip(); });
$("rows").addEventListener("focusin", (e) => { const c = e.target.closest(".bar-col"); if (c) showTip(c); });
$("rows").addEventListener("focusout", hideTip);

// ---------- controls ----------
function updateSortHeaders() {
  for (const b of document.querySelectorAll("th button[data-sort]")) {
    const th = b.parentElement;
    th.setAttribute("aria-sort", b.dataset.sort === state.sortKey
      ? (state.sortDir > 0 ? "ascending" : "descending") : "none");
  }
}

document.querySelector("thead").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-sort]");
  if (!b || !state.data) return;
  const k = b.dataset.sort;
  state.sortDir = state.sortKey === k ? -state.sortDir : (k === "label" ? 1 : -1);
  state.sortKey = k;
  render();
});

function selectBy(by) {
  const b = $("by").querySelector(`button[data-by="${by}"]`);
  if (!b) return false;
  state.by = by;
  state.sortKey = null;
  for (const x of $("by").querySelectorAll("button")) x.setAttribute("aria-checked", String(x === b));
  saveHash();
  return true;
}

function saveHash() {   // the grouping and line are bookmarkable
  const h = new URLSearchParams({ by: state.by });
  if ($("line").value) h.set("line", $("line").value);
  history.replaceState(null, "", `#${h}`);
}

$("by").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-by]");
  if (b && selectBy(b.dataset.by)) load();
});
for (const id of ["days", "horizon", "min-n"]) $(id).addEventListener("change", load);
$("line").addEventListener("change", () => { saveHash(); load(); });
$("filters").addEventListener("submit", (e) => { e.preventDefault(); load(); });

const initial = new URLSearchParams(location.hash.slice(1));
if (initial.get("line")) {
  fillLines([initial.get("line")]);
  $("line").value = initial.get("line");
}
selectBy(initial.get("by") || "line") || selectBy("line");
load();
