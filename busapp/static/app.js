"use strict";
// Arrivals board. Needs common.js ($, el, getJSON, header search).

const REFRESH_MS = 20_000;
const TZ = "America/Chicago";
const fmtTime = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "numeric", minute: "2-digit" });

const state = {
  stopId: null,
  model: null,
  data: null,
  clockOffsetMs: 0,   // server clock - browser clock
  refreshTimer: null,
};

// ---------- helpers ----------
const nowSec = () => (Date.now() + state.clockOffsetMs) / 1000;
const hhmm = (ts) => (ts == null ? "—" : fmtTime.format(new Date(ts * 1000)));

function fmtDelta(sec) {
  const s = Math.round(sec);
  if (Math.abs(s) < 60) return { text: "on time", cls: "ontime" };
  const m = Math.round(Math.abs(s) / 60);
  const cls = s > 0 ? (s >= 300 ? "very-late" : "late") : "early";
  return { text: `${s > 0 ? "+" : "−"}${m} min`, cls };
}


// ---------- arrivals board ----------
async function loadBoard() {
  if (!state.stopId) return;
  clearTimeout(state.refreshTimer);
  try {
    const url = `/api/stops/${encodeURIComponent(state.stopId)}/arrivals?model=${encodeURIComponent(state.model || "")}`;
    const data = await getJSON(url);
    if (data.stop.id !== state.stopId?.split(":")[0]) return;  // user switched stops mid-request
    state.clockOffsetMs = data.now_ts * 1000 - Date.now();
    state.data = data;
    renderBoard();
  } catch (err) {
    console.error(err);
    if (err.message.startsWith("404")) {
      $("board").hidden = true;
      $("empty").hidden = false;
      $("empty").replaceChildren(el("h2", {}, `No stop called “${state.stopId}”`),
        el("p", {}, "Try searching by name or stop code above."));
      state.stopId = null;
      return;
    }
    $("stop-meta").textContent = `Couldn't load arrivals: ${err.message}`;
  }
  state.refreshTimer = setTimeout(loadBoard, REFRESH_MS);
}

function renderBoard() {
  const d = state.data;
  $("empty").hidden = true;
  $("board").hidden = false;
  $("stop-name").textContent = d.stop.name;
  $("stop-meta").textContent =
    `Platforms ${d.stop.stop_ids.join(", ")} · updated ${hhmm(d.now_ts)}` +
    (d.realtime_feed_ts ? ` · MTD feed ${hhmm(d.realtime_feed_ts)}` : "");
  $("rt-warning").hidden = d.realtime_ok;
  $("no-rows").hidden = d.arrivals.length > 0;
  $("rows").replaceChildren(...d.arrivals.map(renderRow));
  tickCountdowns();
}

function deltaCell(ts, sched, extra) {
  if (ts == null) return el("td", { class: "num muted" }, "—", extra);
  const d = fmtDelta(ts - sched);
  return el("td", { class: "num" },
    el("span", { class: "time" }, hhmm(ts)),
    el("span", { class: `delta ${d.cls}` }, d.text),
    extra);
}

function renderRow(a) {
  const canceled = a.status === "canceled";
  const live = a.status === "live";
  const platform = a.platform.match(/\(([^)]*)\)\s*$/)?.[1] ?? a.stop_id;
  const modelNote = el("span", { class: "model-note" },
    a.model_n ? `${a.model_level} · n=${a.model_n}` : "no history yet");
  return el("tr", { class: canceled ? "canceled" : "" },
    el("td", {},
      el("span", { class: "pill", style: `background:#${a.route_color};color:#${a.route_text_color}` },
        a.route_short_name || a.route_id),
      el("span", { class: "route-name" }, a.route_long_name || "")),
    el("td", {},
      el("span", { class: "dest" }, a.headsign || ""),
      canceled ? el("span", { class: "badge" }, "CANCELED") : null,
      el("span", { class: "platform" }, platform + (a.vehicle_id ? ` · bus ${a.vehicle_id}` : ""))),
    el("td", { class: "num" }, el("span", { class: "time" }, hhmm(a.scheduled_ts))),
    deltaCell(a.mtd_ts, a.scheduled_ts),
    deltaCell(a.predicted_ts, a.scheduled_ts, modelNote),
    el("td", { class: "num" },
      el("span", { class: "eta", "data-ts": a.best_ts }),
      el("span", { class: "eta-src" },
        ...(live ? [el("span", { class: "dot live" }), "MTD live"] : canceled ? [] : ["predicted"]))));
}

function tickCountdowns() {
  const now = nowSec();
  for (const n of document.querySelectorAll(".eta[data-ts]")) {
    const secs = +n.dataset.ts - now;
    const mins = Math.floor(secs / 60);
    if (secs < 60) n.replaceChildren("Due");
    else n.replaceChildren(String(mins), el("small", {}, " min"));
  }
}
setInterval(tickCountdowns, 1000);

// ---------- accuracy panel ----------
const fmtErr = (s) => (s == null ? "—" : `${(s / 60).toFixed(1)} min`);
const fmtBias = (s) => (s == null ? "" : `bias ${s >= 0 ? "+" : "−"}${(Math.abs(s) / 60).toFixed(1)}`);

async function loadAccuracy() {
  try {
    const acc = await getJSON(`/api/accuracy?model=${encodeURIComponent(state.model || "")}`);
    renderAccuracy(acc);
  } catch (err) {
    console.error(err);
  }
}

function renderAccuracy(acc) {
  const sec = $("accuracy");
  const fair = acc.observations_with_history > 0;
  const statCell = (st, best) => el("td", { class: "num" },
    el("span", { class: "time", style: best ? "" : "font-weight:500" }, fmtErr(st.mae_s)),
    el("span", { class: "model-note" }, st.n ? `${fmtBias(st.bias_s)} · p90 ${fmtErr(st.p90_s)}` : ""));

  const rows = acc.by_horizon.map((h) => {
    const set = fair ? h : { ...h.all, ours: { n: 0, mae_s: null } };
    const maes = [set.schedule.mae_s, set.mtd.mae_s, set.ours.mae_s].filter((x) => x != null);
    const best = maes.length ? Math.min(...maes) : null;
    return el("tr", {},
      el("td", {}, `~${h.horizon_min} min before`),
      el("td", { class: "num" }, set.mtd.n.toLocaleString()),
      statCell(set.schedule, set.schedule.mae_s === best),
      statCell(set.mtd, set.mtd.mae_s === best),
      fair ? statCell(set.ours, set.ours.mae_s === best)
           : el("td", { class: "num muted" }, "needs a prior day"));
  });

  sec.replaceChildren(
    el("h2", {}, "How accurate is each estimate?"),
    el("p", { class: "muted" },
      `Average error against ${acc.observations.toLocaleString()} observed departures (last ${acc.days} days). ` +
      "MTD is scored on the estimate it showed about H minutes before the bus actually came. " +
      (fair
        ? "Our model only uses data from days before the one it predicts; all three columns use the same departures."
        : "Our model is scored once there's at least one full prior day of history.")),
    el("div", { class: "table-wrap" },
      el("table", {},
        el("thead", {}, el("tr", {},
          el("th", {}, "When you look"), el("th", { class: "num" }, "Departures"),
          el("th", { class: "num" }, "Schedule"), el("th", { class: "num" }, "MTD live"),
          el("th", { class: "num" }, `Our model (${acc.model})`))),
        el("tbody", {}, ...rows))));
  sec.hidden = false;
}
setInterval(loadAccuracy, 10 * 60_000);

// ---------- models, health, routing ----------
async function loadModels() {
  const { default: def, models } = await getJSON("/api/models");
  state.model = def;
  $("model").replaceChildren(...models.map((m) => el("option", { value: m }, m)));
  $("model").value = def;
}
$("model").addEventListener("change", (e) => { state.model = e.target.value; loadBoard(); loadAccuracy(); });
$("refresh").addEventListener("click", loadBoard);

async function loadHealth() {
  try {
    const h = await getJSON("/api/health");
    const since = h.collecting_since_ts ? new Date(h.collecting_since_ts * 1000).toLocaleString() : "—";
    const cov = h.coverage;
    const coverage = cov.gaps_missing_service
      ? `${cov.gaps_missing_service} collector gap(s) during service in ${cov.window_days} days ` +
        `(~${cov.missed_expected_observations.toLocaleString()} observations lost)`
      : `no service missed in ${cov.window_days} days` +
        (cov.gaps ? ` (${cov.gaps} gap(s) during untracked hours)` : "");
    $("health").textContent =
      `Collector ${h.collector_ok ? "running" : "NOT running"} · ` +
      `${h.observations.toLocaleString()} observed departures since ${since} · ` +
      `${h.mtd_prediction_snapshots.toLocaleString()} MTD prediction snapshots · ` +
      `${coverage} · stop search via ${h.rest_search_enabled ? "MTD API" : "local GTFS"}`;
  } catch (err) {
    console.error(err);
  }
}

function onRoute() {
  const id = decodeURIComponent(location.hash.slice(1));
  if (!id) return;
  state.stopId = id;
  loadBoard();
}
window.addEventListener("hashchange", onRoute);

(async function init() {
  await loadModels().catch(console.error);
  loadHealth();
  setInterval(loadHealth, 60_000);
  onRoute();
  loadAccuracy();
})();
