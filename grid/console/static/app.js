/* grid/autonomy console — vanilla JS, no build step.
   Polls the console backend (same origin), renders the fleet, ledger,
   run cards, reliability, config editor and logs. */

"use strict";

/* ── tiny helpers ─────────────────────────────────────────────────── */

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v; // trusted templates only
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    node.append(c.nodeType ? c : document.createTextNode(c));
  }
  return node;
}

function fmtPrice(p) {
  if (p === null || p === undefined || isNaN(p)) return "—";
  const n = Number(p);
  if (n >= 1000) return n.toLocaleString("en-US", { maximumFractionDigits: 2 });
  if (n >= 10) return n.toFixed(3);
  if (n >= 0.1) return n.toFixed(4);
  return n.toPrecision(3);
}
const fmtUsd = (v) => (v === null || v === undefined || isNaN(v))
  ? "—" : `$${Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
const fmtSignedUsd = (v) => (v === null || v === undefined || isNaN(v))
  ? "—" : `${Number(v) >= 0 ? "+" : "−"}$${Math.abs(Number(v)).toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
const isNum = (v) => typeof v === "number" && isFinite(v) ||
  (typeof v === "string" && v !== "" && !isNaN(Number(v)));
const fmtNum = (v, d = 2) => (v === null || v === undefined || isNaN(v))
  ? "—" : Number(v).toFixed(d);
const fmtPct = (v) => (v === null || v === undefined || isNaN(v))
  ? "—" : `${(Number(v) * 100).toFixed(1)}%`;
const fmtDouble = (days) => (days === null || days === undefined || isNaN(days) || Number(days) <= 0)
  ? "—" : Number(days) < 365 ? `~${Math.round(Number(days))}d`
  : Number(days) < 730 ? `~${Math.round(Number(days) / 30.44)}mo`
  : `~${(Number(days) / 365).toFixed(1)}y`;

function relTime(iso) {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400 * 2) return `${(s / 3600).toFixed(1)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

/* epoch-seconds variant (daemon state uses epoch floats, not ISO) */
function relTimeEpoch(ts) {
  const n = Number(ts);
  if (!isFinite(n) || n <= 0) return "\u2014";
  return relTime(new Date(n * 1000).toISOString());
}
/* Human-readable "how long has this bot been held" — H:MM for under 48h,
   "Nd" above. Mirrors the on-the-hour precision an operator wants when
   deciding whether a bot is stale (24h+) without pulling out a calculator
   for the decimal "24.3h" the old version emitted. */
const heldFor = (iso) => {
  if (!iso) return null;
  const ms = Date.now() - Date.parse(iso);
  if (isNaN(ms) || ms < 0) return null;
  const m = Math.floor(ms / 60000);
  if (m < 1) return "<1m";
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}:${String(m % 60).padStart(2, "0")}`;
  return `${Math.floor(h / 24)}d`;
};

/* Rough "expected close by" for a slot: how many hours until net PnL
   (realized + mark) reaches the profit-exit target, assuming the model's
   projected /24h grid-income rate holds. "Rough" is the point — the exit
   is daemon-owned (take_profit_pct × budget), not a fixed clock, so this
   is a trend estimate, not a promise. Falls back to "—" when any input is
   missing or the projected rate isn't positive (a rate of zero or negative
   means the model can't see income, so no honest ETA exists). */
function estimateCloseBy(bot) {
  const obs = bot.observed || {};
  const net = (isNum(obs.realized_pnl) ? Number(obs.realized_pnl) : 0)
            + (isNum(obs.unrealized_pnl) ? Number(obs.unrealized_pnl) : 0);
  const rate = isNum(bot.projected_24h_usd) ? Number(bot.projected_24h_usd) / 24 : null;
  if (rate == null || rate <= 0) return "—";
  const budget = isNum(bot.committed) ? Number(bot.committed) : null;
  if (budget == null || budget <= 0) return "—";
  const tp = isNum(bot.take_profit_pct) ? Number(bot.take_profit_pct)
    : isNum((lastOverview || {}).config_digest && (lastOverview || {}).config_digest.take_profit_pct)
      ? Number((lastOverview || {}).config_digest.take_profit_pct) : 0.10;
  const target = tp * budget;
  const missing = target - net;
  if (missing <= 0) return "now";
  const hrs = missing / rate;
  if (!isFinite(hrs) || hrs <= 0) return "—";
  if (hrs < 24) return `${Math.round(hrs)}h`;
  return `${Math.round(hrs / 24)}d`;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || res.statusText), { data });
  return data;
}

function toast(msg, bad = false, ms = 4200) {
  const t = el("div", { class: `toast${bad ? " toast--bad" : ""}` }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), ms);
}

/* navigator.clipboard with a one-shot fallback (file:// or older WebViews
   reject it; execCommand("copy") on a temporary textarea still works). */
async function copyText(s) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(s);
      return true;
    }
  } catch (_) { /* fall through */ }
  try {
    const ta = document.createElement("textarea");
    ta.value = s;
    ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.append(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch (_) { return false; }
}

/* keep Tab focus inside an open modal (confirm dialogs + the chart modal);
   returns an untrap fn the caller runs on close */
function trapModalFocus(box) {
  const onKey = (e) => {
    if (e.key !== "Tab") return;
    const f = [...box.querySelectorAll("button, [href], input, select, textarea")]
      .filter((n) => !n.disabled);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  box.addEventListener("keydown", onKey);
  return () => box.removeEventListener("keydown", onKey);
}

/* ── confirm modal ────────────────────────────────────────────────── */

function confirmDialog({ title, body, label = "Confirm", danger = false, checkbox = null, checkbox2 = null }) {
  return new Promise((resolve) => {
    const root = $("#modal-root");
    const box = el("div", { class: "modal-backdrop" });
    const checkRef = { input: null };
    const checkRef2 = { input: null };
    const modal = el("div", { class: "modal", role: "dialog", "aria-modal": "true" },
      el("h3", {}, title),
      el("div", { class: "modal-body" }, ...body),
      checkbox ? el("label", { class: "check-line" },
        (checkRef.input = el("input", { type: "checkbox" })),
        el("span", {}, checkbox)) : null,
      checkbox2 ? el("label", { class: "check-line" },
        (checkRef2.input = el("input", { type: "checkbox" })),
        el("span", {}, checkbox2)) : null,
      el("div", { class: "modal-actions" },
        el("button", { class: "btn", onclick: () => done(false) }, "Cancel"),
        el("button", { class: `btn ${danger ? "btn--danger" : "btn--primary"}`, onclick: () => done(true) }, label)));
    const untrap = trapModalFocus(modal);
    function done(ok) {
      untrap();
      root.innerHTML = "";
      document.removeEventListener("keydown", onKey);
      resolve({ ok, checked: checkRef.input ? checkRef.input.checked : false,
                checked2: checkRef2.input ? checkRef2.input.checked : false });
    }
    function onKey(e) { if (e.key === "Escape") done(false); }
    document.addEventListener("keydown", onKey);
    box.append(modal);
    box.addEventListener("mousedown", (e) => { if (e.target === box) done(false); });
    root.append(box);
    modal.querySelector(".modal-actions .btn:last-child").focus();
  });
}

/* ── tabs ─────────────────────────────────────────────────────────── */

const VIEWS = ["fleet", "decisions", "reports", "optimizer", "reliability", "config", "logs"];
let activeView = "fleet";

function selectView(name) {
  activeView = name;
  for (const v of VIEWS) {
    $(`#tab-${v}`).setAttribute("aria-selected", String(v === name));
    $(`#view-${v}`).hidden = v !== name;
  }
  location.hash = name;
  if (name === "fleet") loadOverview(); // immediate render, don't wait for the poll tick
  if (name === "decisions") loadDecisions();
  if (name === "reports") loadReports();
  if (name === "optimizer") loadOptimizer();
  if (name === "reliability") loadReliability();
  if (name === "config") { loadConfig(); loadLlm(); }
  if (name === "logs") loadLogs(true);
}

for (const v of VIEWS) $(`#tab-${v}`).addEventListener("click", () => selectView(v));

/* back/forward buttons and manual hash edits switch views too */
window.addEventListener("hashchange", () => {
  const h = (location.hash || "#fleet").slice(1);
  if (VIEWS.includes(h) && h !== activeView) selectView(h);
});

/* arrow-key tab navigation (WAI-ARIA tabs pattern) */
document.querySelector(".tabs").addEventListener("keydown", (e) => {
  if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
  const tabs = VIEWS.map((v) => $(`#tab-${v}`));
  const i = tabs.indexOf(document.activeElement);
  if (i < 0) return;
  e.preventDefault();
  const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
  next.focus();
  selectView(VIEWS[tabs.indexOf(next)]);
});

/* ── overview / statusbar / fleet ─────────────────────────────────── */

/* ── freshness banner ──────────────────────────────────────────────── */

/* Each tab backed by a snapshot state file (Decisions journal, Reports
   index, Reliability ledger, Config.yaml, Logs) gets a top-of-tab banner
   that says (a) when the underlying file was last written, (b) which
   source produced it, and (c) the honest truth when the WT-era brain is
   closed in this workspace. The data in the table below is real — but
   new rows will not appear because the autonomy loop is not running.
   The banner turns that implicit staleness explicit. */

function freshnessBadge(f) {
  if (!f) return null;
  const age = (typeof f.age_s === "number") ? f.age_s : null;
  const ageStr = age == null ? "—" :
    age < 60 ? `${age}s ago` :
    age < 3600 ? `${Math.round(age/60)}m ago` :
    age < 86400 ? `${Math.round(age/3600)}h ago` :
    `${Math.round(age/86400)}d ago`;
  const atStr = f.at_iso ? String(f.at_iso).slice(0, 19).replace("T", " ") + "Z" : "—";
  // The brain was closed at 2026-09-14T21:42:37Z (state/engine.json
  // since timestamp). Anything written before that instant is a frozen
  // snapshot from the WT-era brain. config.yaml is intentionally
  // exempt — it's still the authoritative wiring even if pre-pivot.
  const pivotEpoch = Date.UTC(2026, 8, 14, 21, 42, 37);  // 2026-09-14T21:42:37Z
  const kind = f.kind || "state file";
  const isConfig = /config\.yaml$/i.test(f.path || "") || /config/i.test(kind);
  const predatesPivot = !isConfig &&
    (typeof f.at === "number") && (f.at * 1000) < pivotEpoch;
  return { ageStr, atStr, predatesPivot, kind,
          path: f.path || "" };
}

function renderFreshnessBanner(slotId, freshness) {
  const el0 = slotId ? document.getElementById(slotId) : null;
  if (!el0) return;
  if (!freshness) { el0.innerHTML = ""; return; }
  const b = freshnessBadge(freshness);
  if (!b) { el0.innerHTML = ""; return; }
  if (b.predatesPivot) {
    el0.innerHTML = `<div class="banner banner--info" style="margin-bottom:14px;">
      <div>
        <div class="banner-title">Stale snapshot — brain closed</div>
        This ${esc(b.kind)} was last written <b>${esc(b.atStr)}</b>
        (${esc(b.ageStr)}), before the WT-era pivot on 2026-09-14. The autonomy
        loop is closed in this workspace — no new entries will appear until
        M3 lands. The rows below are a frozen snapshot from the last WT-era
        deliberation cycle.
      </div></div>`;
  } else if (b.atStr === "—") {
    el0.innerHTML = `<div class="banner banner--info" style="margin-bottom:14px;">
      <div>
        <div class="banner-title">${esc(b.kind)} state not present</div>
        ${esc(freshness.note || "the underlying file does not exist")}
      </div></div>`;
  } else {
    el0.innerHTML = `<div class="banner banner--ok" style="margin-bottom:14px;">
      <div>
        <div class="banner-title">${esc(b.kind)} fresh</div>
        Last written <b>${esc(b.atStr)}</b> (${esc(b.ageStr)}).
        ${esc(b.path)}
      </div></div>`;
  }
}

let lastOverview = null;
let lastPnlPoints = null;   // /api/pnl points (newest-first)

async function loadOverview() {
  let ov;
  try {
    ov = await api("/api/overview");
  } catch (e) {
    renderStatusbar(null);
    return;
  }
  lastOverview = ov;
  renderStatusbar(ov);
  if (activeView === "fleet") {
    renderReadiness(ov);
    renderFleet(ov);
    renderFleetHeader(ov);
    renderFeed(ov.journal_tail || []);
    renderSummary(ov);
    drawPnlChart(lastPnlPoints || []);
    loadSlotCharts(ov);
    // fire-and-forget — server caches the ping for 60s; awaiting it would
    // block the rest of the overview render for up to 9s on a slow provider.
    renderLlmBrains();
  }
}

function renderStatusbar(ov) {
  const bar = $("#statusbar");
  if (!ov) {
    bar.innerHTML = `<span class="chip chip--bad"><span class="dot"></span>console backend unreachable</span>`;
    return;
  }
  const d = ov.daemon || {};
  const chips = [];
  if (d.running) {
    chips.push(`<span class="chip chip--ok"><span class="dot pulse"></span>mission <b>${esc(d.mode || "?")}</b> \u00b7 ${esc(d.supervisor)} \u00b7 pid ${esc(d.pid)}</span>`);
  } else {
    chips.push(`<span class="chip chip--bad"><span class="dot"></span>mission stopped</span>`);
  }
  const eng = ov.engine || null;
  if (eng) {
    chips.push(`<span class="chip chip--ok" style="border-color:var(--violet);color:var(--violet)" title="${esc(eng.note || "active execution engine (state/engine.json)")}"><span class="dot pulse"></span>engine <b>${esc(eng.engine)}${eng.mode ? ` \u00b7 ${esc(eng.mode)}` : ""}</b></span>`);
  }
  const ftf = ov.ft_fleet || {};
  const insts = ftf.instances || [];
  const running = insts.filter((i) => i.api_ok).length;
  const committed = insts.reduce((a, i) => a + (isNum(i.open_stake) ? Number(i.open_stake) : 0), 0);
  chips.push(`<span class="chip"><span class="dot"></span>fleet <b>${running}/${ftf.active ?? running}</b> \u00b7 ${esc(fmtUsd(committed))} committed (dry-run)</span>`);
  chips.push(d.kill_file
    ? `<span class="chip chip--bad"><span class="dot"></span><b>KILL armed</b></span>`
    : `<span class="chip"><span class="dot"></span>KILL clear</span>`);
  chips.push(ov.pocketbase && ov.pocketbase.up
    ? `<span class="chip"><span class="dot"></span>PB journal up</span>`
    : `<span class="chip"><span class="dot" style="background:var(--ink-faint)"></span>PB journal down</span>`);
  bar.innerHTML = chips.join("");
  // keep the header subtitle in sync with the actual daemon mode
  // (textContent — no HTML escaping needed)
  const modeLabel = (ov && ov.daemon && ov.daemon.mode) || "\u2014";
  $("#mission-sub").textContent = eng
    ? `mission console \u00b7 engine: ${eng.engine}${eng.mode ? ` (${eng.mode})` : ""} \u00b7 daemon: ${modeLabel}`
    : `mission console \u00b7 ${modeLabel} \u00b7 connecting\u2026`;
}

const chartCache = {};   // "venue:symbol:interval" -> {at (epoch ms), data}
const CHART_TTL_MS = 5 * 60 * 1000;
const chartInflight = {};   // same key -> promise, dedupes parallel polls
let slotChartsBusy = false;

async function fetchChart(venue, symbol, interval = "1h", bars = 96) {
  const key = `${venue}:${symbol}:${interval}`;
  const hit = chartCache[key];
  if (hit && Date.now() - hit.at < CHART_TTL_MS) return hit.data;
  if (chartInflight[key]) return chartInflight[key];
  chartInflight[key] = (async () => {
    try {
      const data = await api(`/api/chart?venue=${encodeURIComponent(venue)}`
        + `&symbol=${encodeURIComponent(symbol)}`
        + `&interval=${encodeURIComponent(interval)}&bars=${bars}`);
      if (!data || data.error || !Array.isArray(data.bars) || !data.bars.length)
        throw new Error((data && data.error) || "no bars");
      chartCache[key] = { at: Date.now(), data };
      return data;
    } finally { delete chartInflight[key]; }
  })();
  return chartInflight[key];
}

/* fetch /api/chart (1h × 96 bars) for each distinct venue:symbol on the
   fleet in parallel; then paint whatever is cached. Fail-soft: a rejected
   fetch keeps the previous sparkline / placeholder untouched. */
async function loadSlotCharts(ov) {
  if (slotChartsBusy) return;
  slotChartsBusy = true;
  try {
    const seen = new Set();
    const jobs = [];
    const sources = [...(ov.bots || []),
                     ...(((ov.ft_fleet || {}).instances) || [])];
    for (const b of sources) {
      if (!b || !b.venue || !b.symbol) continue;
      const key = `${b.venue}:${b.symbol}`;
      if (seen.has(key)) continue;
      seen.add(key);
      jobs.push(fetchChart(b.venue, b.symbol));
    }
    await Promise.allSettled(jobs);
    renderSlotSparklines();
  } finally { slotChartsBusy = false; }
}

/* inline sparkline per .slot-spark node: closes min-max scaled (3px pad),
   teal when the window is up, crimson when down, plus dashed channel
   high/low refs when the bot carries a channel. Idempotent per data
   epoch (dataset.at) so re-renders don't thrash. */
function renderSlotSparklines() {
  const bots = (lastOverview && lastOverview.bots) || [];
  for (const node of document.querySelectorAll(".slot-spark")) {
    const key = node.dataset.key || "";
    const hit = chartCache[`${key}:1h`];
    if (!hit || !hit.data) continue;
    const bars = hit.data.bars || [];
    if (bars.length < 2) continue;
    const stamp = String(hit.at);
    if (node.dataset.at === stamp) continue;
    node.dataset.at = stamp;
    const closes = [];
    for (const b of bars) { const c = Number(b && b.c); if (isFinite(c)) closes.push(c); }
    if (closes.length < 2) continue;
    const W = 220, H = 48, P = 3;
    let lo = Math.min(...closes), hi = Math.max(...closes);
    if (hi - lo < 1e-12) { const e = Math.abs(hi) * 0.001 || 0.001; hi += e; lo -= e; }
    const X = (i) => P + (i / (bars.length - 1)) * (W - 2 * P);
    const Y = (v) => P + (1 - (v - lo) / (hi - lo)) * (H - 2 * P);
    let pts = "";
    bars.forEach((b, i) => {
      const c = Number(b && b.c);
      if (isFinite(c)) pts += `${X(i).toFixed(2)},${Y(c).toFixed(2)} `;
    });
    const first = closes[0], last = closes[closes.length - 1];
    const up = last >= first;
    const delta = first ? ((last - first) / first) * 100 : 0;
    let refs = "";
    const bot = bots.find((b) => String(b.slot) === String(node.dataset.slot));
    let ch = (bot && bot.channel) || null;
    if (!ch) {
      // instance cards: dataset.slot is "ft:<bot_code>" — the live channel
      // comes from the server's recomputed geometry (channel_live)
      const insts = (((lastOverview && lastOverview.ft_fleet) || {}).instances) || [];
      const inst = insts.find((x) => `ft:${x.bot_code}` === String(node.dataset.slot));
      const lc = inst && inst.channel_live;
      if (lc && isNum(lc.high) && isNum(lc.low)) ch = lc;
    }
    if (ch && isNum(ch.high) && isNum(ch.low)) {
      const yH = Math.max(P, Math.min(H - P, Y(Number(ch.high)))).toFixed(1);
      const yL = Math.max(P, Math.min(H - P, Y(Number(ch.low)))).toFixed(1);
      refs = `<line x1="0" y1="${yH}" x2="${W}" y2="${yH}" class="spark-ref"/>`
        + `<line x1="0" y1="${yL}" x2="${W}" y2="${yL}" class="spark-ref"/>`;
    }
    const slot = node.querySelector(".spark-slot");
    if (slot) slot.innerHTML = `
      <svg class="spark-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
        ${refs}
        <polyline class="spark-line${up ? "" : " spark-line--down"}" points="${pts.trim()}"/>
      </svg>`;
    const d = node.querySelector(".spark-delta");
    if (d) {
      d.className = `spark-delta mono ${up ? "spark-delta--up" : "spark-delta--down"}`;
      d.textContent = `\u0394 ${up ? "+" : "\u2212"}${Math.abs(delta).toFixed(2)}%`;
    }
  }
}

/* the big chart modal: 96×1h closes as a line + light area fill, dashed
   channel high/mid/low with right-edge labels, lo/hi/last captions and
   the bar window. Reuses #modal-root; Escape/backdrop/Close all clean
   the key listener up (confirmDialog's onKey pattern).

   Cold-cache: a slot card with a not-yet-fetched sparkline still has a
   clickable .slot-spark. The old version rendered the modal with
   "no chart data cached … yet" while the fetch was in flight in the
   background, then left the operator staring at it. We now show a
   spinner, wait for the in-flight fetch (with a 12s timeout — tvcli is
   normally <3s for a cached Binance candle window), and re-render once
   data lands. */
function openMarketModal(key, slot) {
  const parts = String(key).split(":");
  const venue = parts[0] || "?", symbol = parts.slice(1).join(":") || "?";
  const root = $("#modal-root");
  const box = el("div", { class: "modal-backdrop" });

  const W = Math.max(320, Math.min(window.innerWidth * 0.9, 900)), H = 360;
  const RX = 62;   // right gutter for channel labels

  const modal = el("div", { class: "modal modal--chart", role: "dialog", "aria-modal": "true" },
    el("h3", {}, `MARKET — ${esc(venue)}:${esc(symbol)}`),
    el("div", { class: "modal-body" },
      el("div", { class: "mk-chart" })),
    el("div", { class: "modal-actions" },
      el("button", { class: "btn", onclick: () => done() }, "Close")));
  const chartHost = modal.querySelector(".mk-chart");

  function paint(bars) {
    let ch = null;
    const insts = (((lastOverview && lastOverview.ft_fleet) || {}).instances) || [];
    const inst = insts.find((x) => `ft:${x.bot_code}` === String(slot));
    if (inst && inst.channel_live) ch = inst.channel_live;
    let chartHTML = `<div class="mk-empty">no chart data cached for ${esc(venue)}:${esc(symbol)} yet</div>`;
    let captions = "";
    if (bars.length >= 2) {
      const closes = [];
      for (const b of bars) { const c = Number(b && b.c); if (isFinite(c)) closes.push(c); }
      if (closes.length >= 2) {
        let lo = Math.min(...closes), hi = Math.max(...closes);
        if (isNum(ch && ch.low)) lo = Math.min(lo, Number(ch.low));
        if (isNum(ch && ch.high)) hi = Math.max(hi, Number(ch.high));
        if (hi - lo < 1e-12) { const e = Math.abs(hi) * 0.001 || 0.001; hi += e; lo -= e; }
        const padY = (hi - lo) * 0.08;
        lo -= padY; hi += padY;
        const T = 10, B = 10;
        const X = (i) => 8 + (i / (bars.length - 1)) * (W - RX - 14);
        const Y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
        let pts = "", area = `M ${X(0).toFixed(1)},${(H - B).toFixed(1)}`;
        bars.forEach((b, i) => {
          const c = Number(b && b.c);
          if (!isFinite(c)) return;
          pts += `${X(i).toFixed(1)},${Y(c).toFixed(1)} `;
          area += ` L ${X(i).toFixed(1)},${Y(c).toFixed(1)}`;
        });
        area += ` L ${X(bars.length - 1).toFixed(1)},${(H - B).toFixed(1)} Z`;
        const first = closes[0], last = closes[closes.length - 1];
        const up = last >= first;
        let refs = "";
        const chLine = (v, label) => {
          if (!isNum(v)) return "";
          const y = Y(Number(v)).toFixed(1);
          const lbl = label ? `${esc(label)} ${fmtPrice(v)}` : fmtPrice(v);
          return `<line x1="8" y1="${y}" x2="${W - RX}" y2="${y}" class="mk-ch"/>`
            + `<text x="${W - RX + 6}" y="${Number(y) + 3}" class="mk-label">${lbl}</text>`;
        };
        if (ch) refs = chLine(ch.high, "hi") + chLine(ch.mid, "mid") + chLine(ch.low, "lo");
        chartHTML = `
          <svg class="mk-svg" viewBox="0 0 ${W.toFixed(0)} ${H}" role="img"
               aria-label="1h closes for ${esc(venue)}:${esc(symbol)}">
            <path class="mk-area${up ? "" : " mk-area--down"}" d="${area}"/>
            ${refs}
            <polyline class="mk-line${up ? "" : " mk-line--down"}" points="${pts.trim()}"/>
          </svg>`;
        const t0 = Number(bars[0] && bars[0].t), tN = Number(bars[bars.length - 1] && bars[bars.length - 1].t);
        captions = `
          <div class="mk-captions mono">
            <span>lo <b>${fmtPrice(Math.min(...closes))}</b></span>
            <span>hi <b>${fmtPrice(Math.max(...closes))}</b></span>
            <span>last <b>${fmtPrice(last)}</b></span>
            <span class="mk-delta ${up ? "mk-delta--up" : "mk-delta--down"}">
              \u0394 ${up ? "+" : "\u2212"}${Math.abs(first ? ((last - first) / first) * 100 : 0).toFixed(2)}%</span>
            <span class="mk-window">${bars.length} \u00d7 1h bars \u00b7 ${relTimeEpoch(t0)} \u2192 ${relTimeEpoch(tN)}</span>
          </div>`;
      }
    }
    chartHost.innerHTML = chartHTML + captions;
  }

  // First paint from the cache; if cold, request a fetch + show a spinner.
  const cached = chartCache[`${key}:1h`];
  const cachedBars = (cached && cached.data && cached.data.bars) || [];
  if (cachedBars.length >= 2) {
    paint(cachedBars);
  } else {
    chartHost.innerHTML = `<div class="mk-empty"><span class="spinner"></span> fetching ${esc(venue)}:${esc(symbol)} 1h bars…</div>`;
    // Fire (or wait on an in-flight) fetch with a bounded timeout — never
    // strand the operator on a spinner if tvcli is down.
    let timer;
    const timeout = new Promise((_, rej) => { timer = setTimeout(() => rej(new Error("timeout")), 12000); });
    const work = fetchChart(venue, symbol, "1h", 96)
      .then((d) => (d && d.bars) || [])
      .catch(() => null);
    Promise.race([work, timeout]).then((bars) => {
      clearTimeout(timer);
      // If the user closed the modal in the meantime, chartHost is no
      // longer in the DOM — skip the second paint.
      if (!chartHost.isConnected) return;
      paint(bars && bars.length >= 2 ? bars : []);
    });
  }

  function done() {
    untrap();
    root.innerHTML = "";
    document.removeEventListener("keydown", onKey);
  }
  function onKey(e) { if (e.key === "Escape") done(); }
  const untrap = trapModalFocus(modal);
  document.addEventListener("keydown", onKey);
  box.append(modal);
  box.addEventListener("mousedown", (e) => { if (e.target === box) done(); });
  root.append(box);
  modal.querySelector(".modal-actions .btn").focus();
}

/* the readiness strip: the mission's own dependency + capacity diagnostics,
   surfaced as a row of glanceable instrument cells. Answers the three
   questions an operator asks before trusting the loop — is the freqtrade
   engine responsive on each active instance, are the strategy files in
   place, is the LLM chain up (the optimizer / NLG hint uses it). */
function renderReadiness(ov) {
  const el0 = $("#readiness");
  if (!el0) return;
  const r = ov.readiness;
  if (!r || !r.reachable) {
    el0.innerHTML = `<div class="readiness-head"><span class="card-title">Readiness</span></div>
      <div class="readiness-cells"><span class="ready-cell ready-cell--off">readiness payload not available — refresh</span></div>`;
    el0.hidden = false;
    return;
  }

  const cells = [];
  const on = (v) => v ? "ready-cell--on" : "ready-cell--off";
  const dot = (v) => v ? "●" : "○";
  const env = r.llm_env || {};

  // freqtrade instances — one cell per active dry-run engine.
  const fleet = r.ft_fleet || [];
  if (!fleet.length) {
    cells.push(`<div class="ready-cell ready-cell--off" title="no freqtrade instance running">
      <span class="ready-dot">○</span><span class="ready-key">engine</span><span class="ready-val">0 instances</span>
    </div>`);
  } else {
    for (const inst of fleet) {
      const up = !!inst.api_ok;
      cells.push(`<div class="ready-cell ${up ? "ready-cell--on" : "ready-cell--warn"}"
          title="${esc(inst.bot_code)} · ${esc(inst.pair || "?")} · port ${inst.port}${up ? " · /api/v1/ping ok" : " · not yet ready"}">
        <span class="ready-dot">${dot(up)}</span><span class="ready-key">${esc(inst.bot_code)}</span>
        <span class="ready-val">${esc(inst.pair || "?")} :${inst.port}</span>
      </div>`);
    }
  }

  // .venv-ft — the freqtrade binary + deps (read-only borrow from M3).
  cells.push(`<div class="ready-cell ${on(r.venv_ft)}" title="freqtrade venv (read-only borrow from grid-autonomy's M3 env)">
    <span class="ready-dot">${dot(r.venv_ft)}</span><span class="ready-key">.venv-ft</span>
    <span class="ready-val">${r.venv_ft ? "up" : "missing"}</span>
  </div>`);

  // GridStrategy files + tuned params override.
  cells.push(`<div class="ready-cell ${on(r.strategy_files)}" title="grid/strategies/{GridStrategy.py, GridStrategy.json, grid_geometry.py}">
    <span class="ready-dot">${dot(r.strategy_files)}</span><span class="ready-key">strategy</span>
    <span class="ready-val">${r.strategy_files ? "up" : "missing"}</span>
  </div>`);

  // PocketBase — the WT-era brain's journal store (grid-autonomy). Probed
  // for continuity, but the standalone stack never reads it: live data
  // comes from workspace artifacts (trades DBs, state files, engine REST).
  cells.push(`<div class="ready-cell ${on(r.pocketbase)}" title="WT-era journal store from the retired brain (grid-autonomy) — not part of the standalone stack; live reads come from workspace artifacts (trades DB, state files, engine REST)">
    <span class="ready-dot">${dot(r.pocketbase)}</span><span class="ready-key">pocketbase</span>
    <span class="ready-val">${r.pocketbase ? "up (unused)" : "not wired"}</span>
  </div>`);

  // LLM providers — presence booleans from the workspace sidecar
  // (state/llm.env); the swarm consumer is the brain (M3), not the engine.
  for (const [k, present] of Object.entries(env)) {
    cells.push(`<div class="ready-cell ${on(present)}" title="${esc(k)} present in env">
      <span class="ready-dot">${dot(present)}</span><span class="ready-key">${esc(k)}</span><span class="ready-val">${present ? "up" : "down"}</span>
    </div>`);
  }
  if (!Object.keys(env).length) {
    cells.push(`<div class="ready-cell ready-cell--off" title="no LLM provider env keys present"><span class="ready-dot">○</span><span class="ready-key">llm</span><span class="ready-val">none</span></div>`);
  }

  el0.innerHTML = `<div class="readiness-head"><span class="card-title">Readiness</span>
      <span class="mono readiness-at">${esc(relTime(ov.at))}</span></div>
    <div class="readiness-cells">${cells.join("")}</div>`;
  el0.hidden = false;
}

/* freqtrade dry-run instance card — the fleet-board counterpart of the
   retired WT slot card. Everything on it is live: registry identity,
   engine REST marks, the dry-run trades DB, and the channel recomputed
   the way GridStrategy does it. All sources are workspace artifacts. */
function instanceCardHTML(i) {
  const ch = i.channel_live || {};
  const open = Number(i.open_trades || 0);
  const prof = isNum(i.open_profit_abs) ? Number(i.open_profit_abs) : null;
  const profPct = isNum(i.open_profit_pct) ? Number(i.open_profit_pct) : null;
  const realized = Number(i.realized || 0);
  const row = (k, v) => `<div class="row"><span class="k">${k}</span><span class="v">${v}</span></div>`;
  const rows = [
    row("pair", `<span class="mono">${esc(i.pair || "?")} · 1h · :${esc(i.port ?? "?")}</span>`),
    row("wallet", i.wallet_total != null
      ? `<span class="mono">${fmtUsd(i.wallet_total)} USDC</span> <span class="badge badge--dim" title="freqtrade dry_run wallet — virtual funds only, dry_run: true is hardcoded in the instance config">dry-run</span>`
      : "—"),
    row("position", open
      ? `${open} open${i.open_enter_tag ? ` · ${esc(i.open_enter_tag)}` : ""}${prof != null
        ? ` · mark <b class="${prof >= 0 ? "m-value--good" : "m-value--bad"}">${fmtSignedUsd(prof)}</b>${profPct != null ? ` (${profPct >= 0 ? "+" : ""}${profPct.toFixed(2)}%)` : ""}`
        : ""}`
      : "flat"),
    row("realized", `<span class="mono">${fmtSignedUsd(realized)}</span> · ${i.closed_trades ?? 0} closed · ${i.fills ?? 0} grid fills (${i.fills_24h ?? 0}/24h)`),
    row("channel", ch.low != null
      ? `<span class="mono">${fmtNum(ch.low, 0)} \u2013 ${fmtNum(ch.high, 0)} · step ${fmtNum(ch.step_pct, 2)}% · ${ch.grids} lines</span> <span class="badge badge--dim" title="recomputed live from the engine's last analyzed candle — GridStrategy never persists geometry">live</span>`
      : "computing\u2026"),
  ].join("");
  const spark = `<div class="slot-spark" data-key="${esc(i.venue || "")}:${esc(i.symbol || "")}" data-slot="ft:${esc(i.bot_code)}" title="1h \u00d7 96 closes (tvcli market data)">
    <div class="spark-slot"><div class="empty-note" style="padding:10px 4px">chart loading\u2026</div></div>
    <div class="spark-delta mono"></div></div>`;
  return `
    <div class="slot-head">
      <span class="venue-tag venue-tag--${esc(i.venue || "hyperliquid")}">${esc(i.venue || "?")}</span>
      <b class="mono">${esc(i.bot_code)}</b>
      <span class="badge ${i.api_ok ? "badge--ok" : "badge--bad"}">${i.api_ok ? "running" : "down"}</span>
      <span class="badge badge--violet" title="freqtrade dry-run — virtual funds only; nothing in this workspace can place a live order">DRY-RUN</span>
    </div>
    ${spark}
    <div class="mini-kv">${rows}</div>
    <div class="mono" style="font-size:10.5px;color:var(--ink-faint);margin-top:8px">
      ${i.started_at ? `up since ${esc(String(i.started_at).replace("T", " ").slice(5, 16))} · ` : ""}${i.last_fill_at ? `last fill ${esc(String(i.last_fill_at).replace("T", " ").slice(5, 16))}Z` : "no fills yet"}
    </div>`;
}

/* The standalone fleet board: one live card per freqtrade instance.
   Keyed on bot_code so a poll tick updates contents in place (no
   entrance-animation replay); retired cards — including WT-era slot
   cards left over from before the pivot — drop out of the board. */
function renderFleetInstances(instances, board) {
  const want = instances || [];
  const have = new Map();
  for (const node of board.querySelectorAll(":scope > .slot-card"))
    have.set(node.dataset.slot, node);
  const targetKeys = [];
  for (const i of want) {
    const key = `ft:${i.bot_code}`;
    targetKeys.push(key);
    let node = have.get(key);
    const sig = [i.api_ok, i.wallet_total, i.open_trades, i.open_profit_abs,
                 i.open_profit_pct, i.open_enter_tag, i.realized,
                 i.closed_trades, i.fills, i.fills_24h, i.last_fill_at,
                 i.started_at, i.channel_live && i.channel_live.grids,
                 i.channel_live && i.channel_live.step_pct].join("|");
    if (!node) {
      node = document.createElement("div");
      node.className = "slot-card";
      node.dataset.slot = key;
      node.dataset.rev = sig;
      node.innerHTML = instanceCardHTML(i);
      board.append(node);
    } else if (node.dataset.rev !== sig) {
      node.dataset.rev = sig;
      node.innerHTML = instanceCardHTML(i);
    }
    node.dataset.venue = i.venue || "";
    const spark = node.querySelector(".slot-spark");
    if (spark && !spark.dataset.wired) {
      spark.dataset.wired = "1";
      spark.style.cursor = "zoom-in";
      spark.addEventListener("click", () => openMarketModal(spark.dataset.key, spark.dataset.slot));
    }
  }
  for (const [key, node] of have) {
    if (!targetKeys.includes(key)) node.remove();
  }
}

function renderFleet(ov) {
  const board = $("#slot-board");
  if (((ov.engine || {}).engine === "freqtrade")
      && Array.isArray((ov.ft_fleet || {}).instances)) {
    // standalone board — live freqtrade instances replace the WT-era slot
    // map, which rendered the frozen September-8 state.json fleet as if
    // it were live
    renderFleetInstances(ov.ft_fleet.instances, board);
    return _renderFleetBanners(ov);
  }
  _renderFleetBanners(ov);   // no freqtrade fleet registered — banners only
}

/* Fleet banner strip — engine declaration + mission state + lifecycle
   buttons. Shared by the standalone board and the WT-era fallback. */
function _renderFleetBanners(ov) {
  const banners = [];
  const d = ov.daemon || {};
  const eng = ov.engine || null;
  if (eng) {
    const legacy = eng.legacy || {};
    const ftf = ov.ft_fleet || {};
    banners.push(`<div class="banner banner--engine">
      <div><div class="banner-title">Execution engine: ${esc(eng.engine)}${eng.mode ? ` — ${esc(eng.mode)}` : ""}</div>
      ${eng.note ? `<div>${esc(eng.note)}</div>` : ""}
      <div class="banner-sub">freqtrade fleet: <b>${ftf.active ?? 0}</b> active${ftf.archived_count ? ` · ${ftf.archived_count} archived` : ""}${legacy.closed_at ? ` · legacy ${esc(legacy.engine || "wundertrading")} engine closed ${esc(String(legacy.closed_at).slice(0, 16).replace("T", " "))}` : ""}</div></div>
    </div>`);
  }
  if (!d.running) {
    banners.push(`<div class="banner banner--bad">
      <div><div class="banner-title">Mission is not running</div>
      Last persisted state is shown below (stale ${esc(relTime((ov.ctl && ov.ctl.status && ov.ctl.status.last_cycle) || null))}).
      ${d.kill_file ? "The KILL file is armed — clear it before starting." : ""}</div>
      <span style="margin-left:auto;display:flex;gap:8px;flex:none">
        ${d.kill_file ? `<button class="btn" id="b-unkill">Clear KILL</button>` : ""}
        <button class="btn btn--primary" id="b-start-dry">Start mission</button>
      </span></div>`);
  } else if (d.kill_file) {
    banners.push(`<div class="banner banner--bad">
      <div><div class="banner-title">KILL file armed</div>The daemon halts at the next loop tick. Clear it to keep the fleet running.</div>
      <button class="btn" id="b-unkill" style="margin-left:auto">Clear KILL</button></div>`);
  } else if (d.mode === "dry-run") {
    banners.push(`<div class="banner banner--info">
      <div><div class="banner-title">Dry-run mode</div>The freqtrade engine is trading with virtual funds on the configured pair — no real orders. The grid_geometry channel is computed live from the ATR (tuned params band_atr 4.2 / step_factor 0.21).</div>
      <button class="btn" id="b-restart" style="margin-left:auto;flex:none">Restart mission</button></div>`);
  }
  const bn = $("#fleet-banner");
  bn.innerHTML = banners.join("");
  const wire = (id, fn) => { const n = bn.querySelector(id); if (n) n.addEventListener("click", fn); };
  wire("#b-unkill", ctlUnkill);
  wire("#b-start-dry", () => ctlStart(false));
  wire("#b-restart", () => ctlRestart());
}

let feedSig = "";
function renderFeed(journal) {
  const feed = $("#feed");
  const rows = [...journal].reverse().slice(0, 60); // newest first
  $("#feed-age").textContent = relTime(rows[0] && rows[0].at);
  // skip the re-render when the tail is unchanged — keeps text selection
  // and avoids pointless DOM churn on every 5s poll
  const sig = `${journal.length}|${journal.length ? journal[0].at : ""}|${journal.length ? journal[journal.length - 1].at : ""}`;
  if (sig === feedSig) return;
  feedSig = sig;
  feed.innerHTML = rows.map((e) => {
    const at = String(e.at || "").slice(11, 19);
    const kind = String(e.kind || "?").replace(/_/g, "-");
    return `<li><span class="f-at">${esc(at)}</span><span class="f-kind k--${esc(kind)}">${esc(kind)}</span><span class="f-msg">${esc(e.msg || "")}</span></li>`;
  }).join("") || `<li><span class="f-msg">No engine events yet — grid fills, trade opens and closes land here as the dry-run engine trades.</span></li>`;
}

/* "LLM brains" — small panel that maps the operator's headline question
   ("is Mistral actually driving the fast lane right now?") onto one
   block. Live ping + role routing, updated on every overview poll but
   lazy-loaded so the first paint isn't blocked on a 9s subprocess. */
async function renderLlmBrains() {
  const box = $("#llm-brains");
  const atEl = $("#llm-brains-at");
  if (!box) return;
  let d;
  try { d = await api("/api/llm/health"); }
  catch (e) { box.innerHTML = `<div class="empty-note">Provider health unreachable.</div>`; return; }
  const results = d.results || [];
  const roles = d.roles || {};
  const arbProv = d.arbiter_provider || "mistral";
  if (atEl && d.at) atEl.textContent = `pinged ${relTime(d.at)}`;
  const dot = (ok) => ok ? "●" : "○";
  const cls = (ok) => ok ? "ready-cell--on" : "ready-cell--off";
  const provRow = results.map((r) => {
    const name = r.provider;
    const labelMap = { cf: "CF Workers AI", nvidia: "NVIDIA", openrouter: "OpenRouter", mistral: "Mistral" };
    const lbl = labelMap[name] || name;
    const err = r.ok ? "" : (r.error || "FAIL");
    return `<div class="ready-cell ${cls(r.ok)}" title="${esc(err || 'ok')}">
      <span class="ready-dot">${dot(r.ok)}</span>
      <span class="ready-key">${esc(lbl)}</span>
      <span class="ready-val">${r.ok ? `${r.latency_ms}ms` : "down"}</span>
    </div>`;
  }).join("");
  // Role → provider pin matrix (which agent uses which model). The arbiter
  // (fast-lane capital reallocation) is NOT in the swarm role list — it is
  // driven by `config.optimizer.llm_provider` and surfaced separately as
  // `arbiter_provider` so the operator can answer "is Mistral actually
  // doing the fast lane right now?" at a glance.
  const swarmRoles = d.role_keys || [];
  const arbPinned = roles.optimizer;
  const arbUsing = arbPinned || arbProv || "mistral";
  const roleRow = (label, roleKey) => {
    const using = roles[roleKey] || "follow chain";
    return `<div class="row"><span class="k" title="Pinned provider for ${roleKey}. Default follows the chain.">${esc(label)}</span>
      <span class="v">${using === "follow chain"
        ? `<span class="badge badge--dim">follow chain</span>`
        : `<span class="badge badge--violet">${esc(using)}</span>`}</span></div>`;
  };
  const arbRow = `<div class="row"><span class="k" title="Pinned provider for the fast-lane arbiter. Default = ${esc(arbProv || 'mistral')}. Override: config.optimizer.llm_provider.">arbiter (fast lane)</span>
    <span class="v"><span class="badge badge--violet">${esc(arbUsing)}</span>${arbPinned ? "" : ` <span class="mono" style="color:var(--ink-faint);font-size:10.5px">default</span>`}</span></div>`;
  const swarmRows = swarmRoles.map((r) => roleRow(r.replace(/_/g, " "), r)).join("");
  box.innerHTML = `
    <div class="readiness-cells" style="margin-bottom:8px">${provRow}</div>
    <div class="mini-kv">${arbRow}${swarmRows}</div>`;
}

function renderSummary(ov) {
  const d = ov.daemon || {};
  const cd = ov.config_digest || {};
  const eng = ov.engine || null;
const ftf = ov.ft_fleet || {};
  const active = (ftf.instances || []).filter((i) => i.status === "running");
  const legacyClosed = (eng && eng.legacy && eng.legacy.closed_at)
    ? String(eng.legacy.closed_at).slice(0, 10)
    : null;
  const engRows = eng
    ? `<div class="row"><span class="k">Execution engine</span><span class="v" title="${esc(eng.note || "")}"><b>${esc(eng.engine)}</b>${eng.mode ? ` · ${esc(eng.mode)}` : ""}${legacyClosed ? ` · WT legacy closed ${esc(legacyClosed)}` : ""}</span></div>
       <div class="row"><span class="k">freqtrade fleet</span><span class="v">${ftf.active ?? 0} active · ${ftf.archived_count ?? 0} archived${active.length ? ` — ${active.slice(0, 5).map((i) => `${esc(i.bot_code)} ${esc(i.pair || "")} (${esc(i.status)})`).join(", ")}` : ""}</span></div>`
    : "";
  const dryRunLine = eng && eng.engine === "freqtrade"
    ? `<div class="row"><span class="k">Dry-run wallet</span><span class="v" title="total dry-run wallet across active instances (the only capital at risk in this workspace)">${active.length ? active.map((i) => `${esc(i.bot_code)} ${fmtUsd(i.slot_balance)}`).join(" · ") || "—" : "—"}</span></div>`
    : "";
  $("#fleet-summary").innerHTML = `
    ${engRows}
    ${dryRunLine}
    <div class="row"><span class="k">Mode</span><span class="v">${esc(d.mode || "—")}${d.supervisor === "grid/dev" ? " · grid/dev" : d.supervisor ? ` · ${esc(d.supervisor)}` : ""}</span></div>
    <div class="row"><span class="k">Archetypes tracked</span><span class="v">${Object.keys((ov.reliability || {}).archetypes || {}).length}</span></div>
    <div class="row"><span class="k">Pair</span><span class="v">${active.length ? esc(active[0].pair || "—") : "—"}</span></div>
    <div class="row"><span class="k">Engine uptime</span><span class="v">${active.length ? `${active.length} running` : "0"}</span></div>`;
}

/* Fleet PnL header — hero + cells + timeline, derived live from the
   freqtrade instances (per-instance trades DB + engine REST marks). The
   WT-era daemon /status shape and its model-based projection cells are
   retired with the brain. */
function renderFleetHeader(ov) {
  const box = $("#pnl-header");
  if (!box) return;
  const insts = ((ov.ft_fleet || {}).instances) || [];
  const num = (v) => (isNum(v) ? Number(v) : null);
  const p = {
    source: insts.length ? "freqtrade dry-run (live)" : null,
    realized: insts.reduce((a, i) => a + (num(i.realized) || 0), 0),
    committed: insts.reduce((a, i) => a + (num(i.open_stake) || 0), 0),
    fills: insts.reduce((a, i) => a + (num(i.fills_24h) || 0), 0) || null,
  };
  const marks = insts.map((i) => num(i.open_profit_abs)).filter((v) => v !== null);
  p.unrealized = marks.length ? marks.reduce((a, v) => a + v, 0) : 0;
  p.net = p.realized + p.unrealized;
  const wallets = insts.map((i) => num(i.wallet_total)).filter((v) => v !== null);
  p.total = wallets.length ? wallets.reduce((a, v) => a + v, 0) : null;
  p.idle = p.total != null ? p.total - p.committed : null;
  const nBots = insts.filter((i) => i.api_ok).length;

  const netCls = p.net == null ? "m-value--dim" : p.net > 0 ? "m-value--good" : p.net < 0 ? "m-value--bad" : "m-value--dim";
  const idlePct = (p.idle == null || !p.total) ? null : (p.idle / p.total) * 100;
  const realizedSub = `<div class="pnl-sub pnl-sub--faint">realized = closed dry-run trades only \u00b7 unrealized = open position mark (engine REST)</div>`;

  let capCell;
  if (nBots) {
    const i0 = insts.find((i) => i.api_ok);
    capCell = `<div class="pnl-cell" title="one dry-run freqtrade instance per grid bot \u00b7 no platform cap applies locally">
      <div class="m-label">freqtrade engine</div>
      <div class="m-value">${nBots} running${i0 && i0.pair ? ` \u00b7 ${esc(i0.pair)}` : ""}</div>
      <div class="pnl-sub pnl-sub--faint">dry-run wallet ${i0 && i0.wallet_total != null ? fmtUsd(i0.wallet_total) : "\u2014"}</div>
    </div>`;
  } else {
    capCell = `<div class="pnl-cell" title="no freqtrade instance is currently running on this workspace">
      <div class="m-label">freqtrade engine</div>
      <div class="m-value m-value--dim">0 running</div>
      <div class="pnl-sub pnl-sub--faint">start the mission with grid/dev start</div></div>`;
  }

  box.innerHTML = `
    <div class="pnl-header-grid">
      <div class="pnl-hero">
        <div class="pnl-hero-label">TRUE NET PnL <span class="pnl-hero-note" title="realized + unrealized mark of the dry-run fleet">${p.source ? `\u00b7 ${esc(p.source)}` : "\u00b7 no data yet"}</span></div>
        <div class="pnl-hero-value ${netCls}">${p.net == null ? "\u2014" : fmtSignedUsd(p.net)}</div>
      </div>
      <div class="pnl-cells">
        <div class="pnl-cell"><div class="m-label">realized</div>
          <div class="m-value ${p.realized > 0 ? "m-value--good" : p.realized < 0 ? "m-value--bad" : "m-value--dim"}">${fmtSignedUsd(p.realized)}</div>
          ${realizedSub}</div>
        <div class="pnl-cell"><div class="m-label">unrealized</div>
          <div class="m-value ${p.unrealized > 0 ? "m-value--good" : p.unrealized < 0 ? "m-value--bad" : "m-value--dim"}">${fmtSignedUsd(p.unrealized)}</div></div>
        <div class="pnl-cell"><div class="m-label">committed / idle</div>
          <div class="m-value">${fmtUsd(p.committed)} <span class="pnl-pct">(${idlePct == null ? "?" : idlePct.toFixed(0) + "% idle"})</span></div>
          <div class="pnl-sub">${fmtUsd(p.idle)} idle of ${fmtUsd(p.total)} wallet${idlePct != null ? ` \u00b7 ${(100 - idlePct).toFixed(0)}% committed` : ""}</div></div>
        <div class="pnl-cell"><div class="m-label">fills 24h</div>
          <div class="m-value">${p.fills == null ? "\u2014" : p.fills}</div>
          <div class="pnl-sub">${nBots} instance${nBots === 1 ? "" : "s"} running</div></div>
        ${capCell}
      </div>
      <div class="pnl-chart">
        <div class="m-label">net \u00b7 realized — <span id="pnl-chart-meta">no history yet</span></div>
        <canvas id="pnl-canvas" width="360" height="96" role="img" aria-label="fleet PnL timeline"></canvas>
      </div>
    </div>`;
  drawPnlChart(lastPnlPoints || []);
}

/* PnL timeline — inline canvas (no CDN, works offline). Two series:
   net (solid + area) and realized (thin), zero line, newest on the right. */
function drawPnlChart(points) {
  const canvas = document.getElementById("pnl-canvas");
  const meta = document.getElementById("pnl-chart-meta");
  if (!canvas) return;
  const pts = (points || []).slice().reverse().filter((p) => p && p.at); // oldest → newest
  if (meta) meta.textContent = pts.length
    ? `${pts.length} point${pts.length === 1 ? "" : "s"} \u00b7 ${relTime(pts[pts.length - 1].at)}`
    : "no history yet";
  const ctx = canvas.getContext && canvas.getContext("2d");
  if (!ctx) return;
  const dpr = window.devicePixelRatio || 1;
  const W = 360, H = 96;
  if (canvas.width !== W * dpr) { canvas.width = W * dpr; canvas.height = H * dpr; }
  canvas.style.width = `${W}px`; canvas.style.height = `${H}px`;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if (pts.length < 1) {
    ctx.fillStyle = "#7C8B83";
    ctx.font = "11px 'IBM Plex Mono', monospace";
    ctx.fillText("no pnl history yet — dry-run engine PnL accrues per closed trade", 8, H / 2);
    return;
  }
  const val = (p, k) => {
    const f = p.fleet || {};
    return isNum(f[k]) ? Number(f[k]) : null;
  };
  const series = [
    { key: "net", color: "#0A7E6D", fill: "rgba(10,126,109,0.10)", width: 2 },
    { key: "realized", color: "#9A5B04", fill: null, width: 1.25 },
  ];
  const vals = [];
  for (const p of pts) for (const s of series) { const v = val(p, s.key); if (v !== null) vals.push(v); }
  if (!vals.length) {
    ctx.fillStyle = "#7C8B83";
    ctx.font = "11px 'IBM Plex Mono', monospace";
    ctx.fillText("pnl points present but no realized/net values", 8, H / 2);
    return;
  }
  let min = Math.min(0, ...vals), max = Math.max(0, ...vals);
  if (max - min < 1e-9) { max += 0.5; min -= 0.5; }
  const pad = (max - min) * 0.12;
  min -= pad; max += pad;
  const padL = 8, padR = 8, padT = 6, padB = 6;
  const X = (i) => padL + (pts.length === 1 ? (W - padL - padR) / 2
    : (i / (pts.length - 1)) * (W - padL - padR));
  const Y = (v) => padT + (1 - (v - min) / (max - min)) * (H - padT - padB);
  // zero line
  if (min < 0 && max > 0) {
    ctx.strokeStyle = "rgba(24,36,32,0.25)";
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(padL, Y(0)); ctx.lineTo(W - padR, Y(0)); ctx.stroke();
    ctx.setLineDash([]);
  }
  for (const s of series) {
    const xy = [];
    pts.forEach((p, i) => { const v = val(p, s.key); if (v !== null) xy.push([X(i), Y(v)]); });
    if (!xy.length) continue;
    if (s.fill) {
      ctx.beginPath();
      ctx.moveTo(xy[0][0], H - padB);
      for (const [x, y] of xy) ctx.lineTo(x, y);
      ctx.lineTo(xy[xy.length - 1][0], H - padB);
      ctx.closePath();
      ctx.fillStyle = s.fill; ctx.fill();
    }
    ctx.beginPath();
    xy.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.strokeStyle = s.color; ctx.lineWidth = s.width;
    ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.stroke();
    const lastPt = xy[xy.length - 1];
    ctx.fillStyle = s.color;
    ctx.beginPath(); ctx.arc(lastPt[0], lastPt[1], 2.4, 0, Math.PI * 2); ctx.fill();
  }
}

async function loadPnlTimeline() {
  let data;
  try { data = await api("/api/pnl"); }
  catch (e) { lastPnlPoints = null; drawPnlChart([]); return; }
  lastPnlPoints = (data && data.points) || [];
  drawPnlChart(lastPnlPoints);
}

/* ── decisions ────────────────────────────────────────────────────── */

let lastEngineEvents = [];               // live engine decision stream

async function loadDecisions() {
  let payload = null;
  try { payload = await api("/api/decisions?limit=400"); }
  catch (e) { toast(`decisions: ${e.message}`, true); return; }
  lastEngineEvents = payload.engine_events || [];
  renderEngineDecisions();
  renderDecisions();
}

/* Live engine decisions — the standalone system's decision stream:
   grid-line fills + trade opens/closes, derived server-side from each
   instance's dry-run trades DB. The WT-era deliberate → guard → deploy
   ledger (state/decisions.jsonl, last written 2026-09-14 21:33:58Z,
   pre-pivot) is preserved on disk but no longer rendered: it has had no
   writer since the brain was retired with the legacy engine. */
function renderEngineDecisions() {
  const slot = $("#dec-engine");
  if (!slot) return;
  const evs = lastEngineEvents || [];
  slot.innerHTML = `
    <div class="banner banner--engine" style="display:block">
      <div>
        <div class="banner-title">Live engine decisions — freqtrade dry-run (${evs.length} event${evs.length === 1 ? "" : "s"})</div>
        <div class="banner-sub">Grid fills, trade opens and closes from the running instance(s) — derived live from state/ft_fleet/&lt;bot&gt;/tradesv3.dryrun.sqlite. The retired brain's decision ledger (state/decisions.jsonl) is preserved on disk and no longer displayed.</div>
      </div>
    </div>`;
}

function renderDecisions() {
  const q = ($("#dec-filter").value || "").toLowerCase();
  const evs = lastEngineEvents.filter((e) => !q
    || String(e.msg || "").toLowerCase().includes(q)
    || String(e.kind || "").toLowerCase().includes(q)
    || String(e.slot || "").toLowerCase().includes(q));
  $("#dec-count").textContent = `${evs.length} event${evs.length === 1 ? "" : "s"}`;
  $("#dec-body").innerHTML = evs.map((e) => {
    const kind = String(e.kind || "?").replace("engine-", "");
    return `<tr>
      <td class="td-mono">${esc(String(e.at || "").replace("T", " ").slice(5, 19))}Z</td>
      <td class="td-mono"><b>${esc(e.slot || "?")}</b></td>
      <td><span class="badge ${e.kind === "engine-fill" ? "badge--violet" : "badge--dim"}">${esc(kind)}</span></td>
      <td>${esc(e.msg || "")}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="4"><div class="empty-note">No engine events yet — grid fills, trade opens and closes land here as the dry-run engine trades.</div></td></tr>`;
}

$("#dec-filter").addEventListener("input", renderDecisions);

/* ── run cards ────────────────────────────────────────────────────── */

async function loadReports() {
  let payload;
  try { payload = await api("/api/reports"); }
  catch (e) { toast(`run cards: ${e.message}`, true); return; }
  renderEngineSession(payload.engine || null);
}

/* Live engine session banner — what the standalone system is actually
   running right now, the standalone system's live run card. */
function renderEngineSession(eng) {
  const slot = $("#rc-engine");
  if (!slot) return;
  if (!eng || !(eng.instances || []).length) {
    slot.innerHTML = `<div class="banner banner--info" style="display:block"><div>
      <div class="banner-title">Engine session</div>
      <div>No freqtrade instance registered — start the mission with grid/dev start.</div></div></div>`;
    return;
  }
  const rows = eng.instances.map((i) => {
    const ch = i.channel || {};
    const mark = isNum(i.open_profit_abs) ? Number(i.open_profit_abs) : null;
    return `<tr>
      <td class="td-mono"><b>${esc(i.bot_code || "?")}</b></td>
      <td class="td-mono">${esc(i.pair || "?")}</td>
      <td><span class="badge ${i.api_ok ? "badge--ok" : "badge--bad"}">${i.api_ok ? "running" : "down"}</span></td>
      <td class="td-mono">${i.open_trades ?? 0} open · ${i.closed_trades ?? 0} closed · ${i.fills ?? 0} fills</td>
      <td class="td-mono">mark <b class="${mark != null ? (mark >= 0 ? "m-value--good" : "m-value--bad") : "m-value--dim"}">${mark != null ? fmtSignedUsd(mark) : "\u2014"}</b> · realized ${fmtSignedUsd(Number(i.realized || 0))}</td>
      <td class="td-mono">${ch.low != null ? `${fmtNum(ch.low, 0)}\u2013${fmtNum(ch.high, 0)} · ${ch.grids} lines · step ${fmtNum(ch.step_pct, 2)}%` : "\u2014"}</td>
      <td class="td-mono">${i.wallet_total != null ? fmtUsd(i.wallet_total) + " USDC" : "\u2014"}</td>
    </tr>`;
  }).join("");
  slot.innerHTML = `<div class="banner banner--engine" style="display:block">
    <div><div class="banner-title">Live engine session (dry-run) — ${esc(String(eng.at || "").replace("T", " ").slice(5, 19))}Z</div>
    <div class="banner-sub">This row is what the workspace engine is actually doing; the WT-era report archive stays on disk under state/reports/ and is no longer displayed.</div>
    <table class="ledger" style="margin-top:8px">
      <thead><tr><th>bot</th><th>pair</th><th>state</th><th>trades</th><th>pnl</th><th>channel</th><th>wallet</th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`;
}

/* ── position optimizer (advisory recommendations + apply gate) ─────── */

let optimizerData = null;

async function loadOptimizer() {
  // /api/optimizer carries the workspace truth (applicable:false in this
  // workspace). Drive the panel from it; fall back to /api/recommendations
  // ONLY for legacy WT-era daemon compatibility.
  let f = null;
  try { f = await api("/api/optimizer"); }
  catch (e) { f = null; }
  if (f && f.applicable === false) {
    optimizerData = null;
    renderOptimizer(f);
    // the ctl-plane companions (/status, /optimizer/swap-log,
    // /position-sweeps) don't apply either — render them empty and stop.
    renderFastOptimizer(null, null);
    renderPositionAnalysis(null, null);
    renderDataSources(null);
    renderSwapLog(null);
    return;
  }
  let d;
  try { d = await api("/api/recommendations?limit=200"); }
  catch (e) { toast(`optimizer: ${e.message}`, true); d = null; }
  if (d) { optimizerData = d; renderOptimizer(d); }
  let st = null;
  try { st = await api("/api/status"); }
  catch (e) { st = null; }
  if (!st || st.error) st = null;
  let sweeps = null;
  try { sweeps = await api("/api/position-sweeps"); }
  catch (e) { sweeps = null; }
  let swapLog = null;
  try { swapLog = await api("/api/optimizer/swap-log"); }
  catch (e) { swapLog = null; }
  renderFastOptimizer(f, st);
  renderPositionAnalysis(st, sweeps);
  renderDataSources(st);
  renderSwapLog(swapLog);
}

/* Standalone-workspace Optimizer panel: geom + tuned params + reliability
   ledger + journal tail. Renders in place of the WT-era "pending recs /
   applied recs" tables when /api/optimizer signals applicable=false. */
function renderStandaloneOptimizer(d) {
  const fast = d.fast || {};
  const geom = fast.geom || [];
  const tuned = fast.tuned_params || {};
  const tunedParams = tuned.params || {};
  const rel = fast.reliability || {};
  const decisions = fast.decisions_tail || [];

  const bn = $("#opt-banner");
  if (bn) {
    bn.innerHTML = `<div class="banner banner--info"><div>
      <div class="banner-title">Optimizer — not applicable in this workspace</div>
      ${esc(d.reason || "execution engine is freqtrade-dry-run; GridStrategy handles per-candle grid adjustments in-strategy.")}
      The panel below shows the deployed grid geometry, the M2-hyperopt
      params in effect, and the reliability ledger for context.</div></div>`;
  }

  const geomRows = geom.map((g) => `
    <tr>
      <td class="td-mono"><b>${esc(g.bot_code)}</b></td>
      <td class="td-mono">${esc(g.low ?? "—")} – ${esc(g.high ?? "—")}</td>
      <td class="td-mono">${esc(g.mid ?? "—")}</td>
      <td class="td-mono" title="band_atr">${esc(g.band_atr ?? "—")}</td>
      <td class="td-mono">${g.atr_pct_derived == null ? "—" : fmtNum(g.atr_pct_derived, 3)}%</td>
      <td class="td-mono">${esc(g.step_pct ?? "—")}%</td>
      <td class="td-mono">${esc(g.grids ?? "—")}</td>
      <td class="td-mono">${fmtUsd(g.amount_per_trade)}</td>
      <td class="td-mono">${fmtUsd(g.distributed_notional)}</td>
    </tr>`).join("") || `<tr><td colspan="9" class="empty-note" style="padding:14px;">No live geometry — start the engine with grid/dev start (geometry is recomputed from the engine's last analyzed candle; nothing persists it).</td></tr>`;

  const tunedRows = Object.entries(tunedParams).map(([k, v]) =>
    `<tr><td class="td-mono"><b>${esc(k)}</b></td><td class="td-mono">${esc(typeof v === "object" ? JSON.stringify(v) : v)}</td></tr>`).join("") ||
    `<tr><td colspan="2" class="empty-note" style="padding:14px;">No tuned params yet.</td></tr>`;

  const relRows = Object.entries(rel).map(([arch, s]) => {
    const pf = isNum(s.profit_factor) ? Number(s.profit_factor) : 0;
    const samples = s.samples ?? 0;
    const recent = isNum(s.recent_pf) ? Number(s.recent_pf) : 0;
    const tier = !samples ? "—"
      : recent < 1.0 ? "killed"
      : samples >= 30 && pf >= 1.3 ? "full"
      : samples >= 10 ? "probe" : "base";
    return `<tr>
      <td><b>${esc(arch)}</b></td>
      <td class="td-mono">${samples}</td>
      <td class="td-mono">${esc(s.synthetic_samples ?? 0)} synth</td>
      <td class="td-mono ${pf >= 1.3 ? "m-value--good" : pf < 1.0 ? "m-value--bad" : ""}">${fmtNum(pf, 2)}</td>
      <td class="td-mono ${recent >= 1.0 ? "m-value--good" : "m-value--bad"}">${fmtNum(recent, 2)}</td>
      <td class="td-mono">${tier}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="6" class="empty-note" style="padding:14px;">Reliability ledger empty.</td></tr>`;

  const decisionRows = decisions.map((x) => `
    <tr>
      <td class="td-mono">${esc(String(x.at || "").replace("T", " ").slice(0, 16))}</td>
      <td class="td-mono">${esc(x.bot_code || x.slot || "—")}</td>
      <td>${esc(x.action || x.kind || "—")}</td>
      <td><div class="rationale" title="${esc(x.reason || x.rationale || "")}">${esc(x.reason || x.rationale || "—")}</div></td>
    </tr>`).join("") || `<tr><td colspan="4" class="empty-note" style="padding:14px;">No decisions recorded yet.</td></tr>`;

  const fastEl = $("#opt-fast");
  if (fastEl) {
    const geomAt = (geom[0] && geom[0].computed_at)
      ? String(geom[0].computed_at).slice(11, 19) + "Z" : null;
    fastEl.innerHTML = `
      <div class="card-head"><span class="card-title">Deployed grid geometry</span>
        ${geomAt ? `<span class="sub" style="color:var(--ink-faint)">recomputed live · ${esc(geomAt)}</span>` : ""}</div>
      <div class="card-body--tight table-wrap">
        <table class="ledger">
          <thead><tr><th>bot</th><th>low – high</th><th>mid</th><th>band_atr</th><th>atr%</th><th>step%</th><th>grids</th><th>/trade</th><th>distributed</th></tr></thead>
          <tbody>${geomRows}</tbody>
        </table>
      </div>`;
  }

  const latestEl = $("#opt-latest-analysis");
  if (latestEl) {
    latestEl.innerHTML = `
      <div class="card-head"><span class="card-title">Tuned params (GridStrategy.json · M2 hyperopt v1)</span></div>
      <div class="card-body--tight table-wrap">
        <table class="ledger">
          <thead><tr><th>param</th><th>value</th></tr></thead>
          <tbody>${tunedRows}</tbody>
        </table>
      </div>`;
  }

  const sourcesEl = $("#opt-data-sources");
  if (sourcesEl) {
    sourcesEl.innerHTML = `
      <div class="card-head"><span class="card-title">Reliability ledger</span></div>
      <div class="card-body--tight table-wrap">
        <table class="ledger">
          <thead><tr><th>archetype</th><th>samples</th><th>synthetic</th><th>PF</th><th>recent PF</th><th>tier</th></tr></thead>
          <tbody>${relRows}</tbody>
        </table>
      </div>`;
  }

  const trackersEl = $("#opt-trackers");
  if (trackersEl) {
    trackersEl.innerHTML = "";
  }

  const swapsEl = $("#opt-swaps");
  if (swapsEl) {
    swapsEl.innerHTML = `
      <div class="card-head"><span class="card-title">WT-era decision journal (frozen tail)</span>
        <span class="sub" style="color:var(--ink-faint)">pre-pivot brain journal — the live engine stream is on the Decisions tab</span></div>
      <div class="card-body--tight table-wrap">
        <table class="ledger">
          <thead><tr><th>at</th><th>slot</th><th>action</th><th>rationale</th></tr></thead>
          <tbody>${decisionRows}</tbody>
        </table>
      </div>`;
  }

  // Pending · unapplied and Applied tables aren't relevant in this
  // workspace — replace them with an explanation.
  const pendingBody = document.querySelector("#opt-pending-body");
  const appliedBody = document.querySelector("#opt-applied-body");
  if (pendingBody) pendingBody.innerHTML = `<tr><td colspan="9" class="empty-note" style="padding:14px;">Not applicable: GridStrategy recomputes the channel every candle inside the strategy; there is no separate slow-loop apply queue in this workspace.</td></tr>`;
  if (appliedBody) appliedBody.innerHTML = `<tr><td colspan="9" class="empty-note" style="padding:14px;">—</td></tr>`;
  const pendingCount = document.querySelector("#opt-pending-count");
  const appliedCount = document.querySelector("#opt-applied-count");
  if (pendingCount) pendingCount.textContent = "n/a";
  if (appliedCount) appliedCount.textContent = "n/a";
}

function blockedByBadge(b) {
  if (b === "applied") return `<span class="badge badge--ok">applied</span>`;
  if (b === "apply disabled") return `<span class="badge badge--dim" title="position_optimizer.apply is false in config.yaml — advisory mode, recs never auto-edit freqtrade grids">apply disabled</span>`;
  if (b === "rate limit") return `<span class="badge badge--warn" title="max_apply_per_day persisted recommendations for today already reached">rate limit</span>`;
  if (b === "journal-only") return `<span class="badge badge--dim" title="dry-run mirror: recommendation is journaled only, never persisted to PocketBase">journal-only</span>`;
  return `<span class="badge badge--violet" title="would apply on its next eligibility check">eligible</span>`;
}

function renderOptimizer(d) {
  // Standalone workspace path — the WT-era slow-loop optimizer doesn't
  // apply (it ran against WunderTrading's grid_bots; GridStrategy handles
  // per-candle grid adjustments in-strategy). Render the truthful geom +
  // tuned params + reliability ledger panel instead of error toasts.
  if (d && d.applicable === false) { renderStandaloneOptimizer(d); return; }

  const recs = (d && d.recommendations) || [];
  const applyEnabled = !!(d && d.apply);
  const maxDay = (d && d.max_apply_per_day) ?? "?";
  const persistedToday = (d && d.persisted_today) ?? "?";

  const bn = $("#opt-banner");
  if (bn) {
    if (applyEnabled) {
      bn.innerHTML = `<div class="banner banner--info"><div>
        <div class="banner-title">Autonomous apply enabled</div>
        position_optimizer.apply is true — the daemon may edit freqtrade grids directly. ${persistedToday}/${maxDay} recommendations persisted today.</div></div>`;
    } else {
      bn.innerHTML = `<div class="banner banner--warn"><div>
        <div class="banner-title">Advisory mode — recommendations are NOT applied</div>
        position_optimizer.apply is false in config.yaml. Every recommendation below is journaled/persisted only; nothing auto-edits freqtrade. ${persistedToday}/${maxDay} persisted today (cap: max_apply_per_day).</div></div>`;
    }
  }

  const pending = recs.filter((r) => !r.applied);
  const applied = recs.filter((r) => !!r.applied);
  $("#opt-pending-count").textContent = `${pending.length} pending \u00b7 ${applied.length} applied`;
  $("#opt-applied-count").textContent = `${applied.length} applied`;

  const row = (r, appliedMode) => {
    const delta = r.expected_delta_pct;
    const at = appliedMode ? (r.applied_at || r.at) : r.at;
    return `<tr>
      <td class="td-mono" title="${esc(r.at || "")}">${esc(String(at || "").replace("T", " ").slice(5, 16))}</td>
      <td class="td-mono">${esc(r.slot ?? "\u2014")}</td>
      <td class="td-mono">${esc(r.venue || "")}:${esc(r.symbol || "?")}</td>
      <td><span class="badge badge--violet">${esc(r.recommendation || "?")}</span></td>
      <td class="td-mono ${(delta || 0) >= 0 ? "m-value--good" : "m-value--bad"}" title="expected 24h profit improvement">${delta == null ? "\u2014" : `${delta >= 0 ? "+" : ""}${fmtNum(delta, 2)}%`}</td>
      <td class="td-mono">${r.confidence == null ? "\u2014" : fmtNum(r.confidence, 2)}</td>
      <td class="td-mono">${esc(r.trigger || "\u2014")}</td>
      ${appliedMode ? "" : `<td>${blockedByBadge(r.blocked_by)}</td>`}
      <td><div class="rationale" title="${esc(r.rationale || "")}">${esc(r.rationale || "\u2014")}</div></td>
    </tr>`;
  };

  // Pending: group by slot (then venue+symbol) so a slot that has been
  // re-evaluated several times in a row shows ONE row with the latest rec
  // + an expandable history. Most slots have 0–1 pending; the ones that
  // have been oscillating across the apply gate are the interesting case.
  const bySlot = new Map();
  for (const r of pending) {
    const key = `${r.slot ?? "—"}|${r.venue}|${r.symbol}`;
    if (!bySlot.has(key)) bySlot.set(key, []);
    bySlot.get(key).push(r);
  }
  const groupRows = [];
  for (const [key, recs] of bySlot) {
    recs.sort((a, b) => (b.at || "").localeCompare(a.at || ""));
    const [head, ...rest] = recs;
    const gKey = `g-${key.replace(/[^a-zA-Z0-9_-]/g, "_")}`;
    const latestDelta = isNum(head.expected_delta_pct) ? Number(head.expected_delta_pct) : null;
    const deltaCls = latestDelta == null ? "m-value--dim"
      : latestDelta > 0 ? "m-value--good" : "m-value--bad";
    const latestConf = isNum(head.confidence) ? fmtNum(head.confidence, 2) : "—";
    const blocked = blockedByBadge(head.blocked_by);
    const at = esc(String(head.at || "").replace("T", " ").slice(5, 16));
    const slot = esc(head.slot ?? "—");
    const market = `<span class="venue-tag venue-tag--${esc(head.venue)}">${esc(head.venue || "")}</span>:${esc(head.symbol || "?")}`;
    const rec = `<span class="badge badge--violet">${esc(head.recommendation || "?")}</span>`;
    const delta = `<span class="${deltaCls}">${latestDelta == null ? "—" : `${latestDelta >= 0 ? "+" : ""}${fmtNum(latestDelta, 2)}%`}</span>`;
    const hasMore = rest.length > 0;
    const chev = hasMore
      ? `<span class="rel-chevron" aria-hidden="true">▸</span>`
      : `<span class="rel-chevron" style="visibility:hidden">▸</span>`;
    const trigger = esc(head.trigger || "—");
    const blockedCell = blocked;
    const rationale = `<div class="rationale" title="${esc(head.rationale || "")}">${esc(head.rationale || "—")}</div>`;
    groupRows.push(`<tr class="rec-group" data-gkey="${gKey}" data-count="${recs.length}">
      <td class="td-mono" title="${esc(head.at || "")}">${at}</td>
      <td class="td-mono">${slot}</td>
      <td class="td-mono">${market}</td>
      <td>${rec}</td>
      <td class="td-mono">${delta}</td>
      <td class="td-mono">${latestConf}</td>
      <td class="td-mono">${trigger}</td>
      <td>${blockedCell}</td>
      <td>${chev} ${hasMore ? `<span class="mono" style="color:var(--ink-faint);font-size:10.5px">+${rest.length} earlier</span>` : ""} ${rationale}</td>
    </tr>`);
    if (hasMore) {
      const hist = rest.map((r) => row(r, false)).join("");
      groupRows.push(`<tr class="rec-detail" data-gkey="${gKey}" hidden><td colspan="9">
        <div class="empty-note" style="margin:0 0 6px;font-size:11px">earlier recs for this slot (oldest first)</div>
        <table class="ledger rec-detail-table">${hist}</table>
      </td></tr>`);
    }
  }
  $("#opt-pending-body").innerHTML = groupRows.join("") ||
    `<tr><td colspan="9"><div class="empty-note">No pending recommendations \u2014 the position optimizer emits one when a bot\u2019s grid is off-price by more than the drift threshold (15 min cadence).</div></td></tr>`;
  // expand/collapse for grouped recs
  for (const head of document.querySelectorAll("#opt-pending-body tr.rec-group")) {
    head.addEventListener("click", (e) => {
      // ignore the inner rationale text selection
      if (window.getSelection && window.getSelection().toString()) return;
      const det = document.querySelector(`#opt-pending-body tr.rec-detail[data-gkey="${head.dataset.gkey}"]`);
      if (!det) return;
      const show = det.hidden;
      det.hidden = !show;
      const chev = head.querySelector(".rel-chevron");
      if (chev) chev.textContent = show ? "▾" : "▸";
    });
  }
  $("#opt-applied-body").innerHTML = applied.map((r) => row(r, true)).join("") ||
    `<tr><td colspan="8"><div class="empty-note">Nothing applied yet${applyEnabled ? "" : " \u2014 apply is disabled in config (advisory mode)"}.</div></td></tr>`;
}

/* ── fast slot optimizer (2–5m cadence capital reallocation) ───────── */

function renderFastOptimizer(f, st) {
  const box = $("#opt-fast");
  if (!box) return;
  const o = f && f.optimizer;
  if (!o) {
    // ctl plane down or the fetch itself failed — quiet fail-soft note
    const why = (f && (f.error || f.detail)) || "unreachable";
    box.innerHTML = `
      <div class="card-head"><span class="card-title">Fast slot optimizer</span>
        <span class="spacer"></span><span class="badge badge--warn" title="daemon ctl plane not responding">offline</span></div>
      <div class="card-body"><div class="empty-note">Fast-optimizer status unavailable (${esc(why)}) — fail-soft: this panel refills automatically once the daemon ctl plane is reachable again. Swaps paused while it is down.</div></div>`;
    return;
  }
  const rep = o.last_report || {};
  const hunt = rep.hunt || {};
  const idle = rep.idle || [];
  const vetoes = rep.vetoes || [];
  const cap = rep.capital || {};
  const cacheAge = f.screen_cache_age_s == null ? null : `${fmtNum(f.screen_cache_age_s, 0)}s`;
  const kv = (k, v, title = "") =>
    `<div class="row"><span class="k"${title ? ` title="${esc(title)}"` : ""}>${esc(k)}</span><span class="v">${v}</span></div>`;

  box.innerHTML = `
    <div class="card-head"><span class="card-title">Fast slot optimizer</span>
      <span class="spacer"></span><span class="mono" style="font-size:10.5px;color:var(--ink-faint)" title="last report ${esc(rep.at || "—")}">report ${esc(relTime(rep.at))}${cacheAge ? ` · screen cache ${esc(cacheAge)}` : ""}</span></div>
    <div class="card-body"><div class="mini-kv">
      ${kv("State", `${o.enabled ? '<span class="badge badge--ok">enabled</span>' : '<span class="badge badge--dim">disabled</span>'} · every ${esc(o.interval_min ?? "—")} min`, "fast capital-reallocation loop (optimizer.py)")}
      ${kv("Cycles", `${esc(o.cycles ?? "—")} · swaps ${esc(o.swaps_total ?? 0)}`, "completed cycles; total slot swaps executed through the guard/churn machinery")}
      ${kv("Last cycle", esc(relTime(o.last_at)))}
      ${kv("Capital", `${fmtUsd(cap.committed_usd)} committed / ${fmtUsd(cap.deployable_ceiling_usd)} ceiling · ${fmtUsd(cap.idle_committed_usd)} idle · ${esc(cap.free_slots ?? "—")} free slot(s)`, "deployable ceiling = free capital available to commit to challengers")}
      ${kv("Projected /24h", (st && st.pnl && isNum(st.pnl.projected_24h_usd)) ? fmtUsd(Number(st.pnl.projected_24h_usd)) : "—", "model-based expected grid income per 24h, net of round-trip fees (from the fleet PnL snapshot)")}
      ${kv("Projected return /yr", (st && st.pnl && isNum(st.pnl.projected_annual_return_pct)) ? `${fmtNum(Number(st.pnl.projected_annual_return_pct), 1)}%` : "—", "approximate annualized return on committed capital")}
      ${kv("Projected doubling time", (st && st.pnl && isNum(st.pnl.projected_double_days)) ? fmtDouble(Number(st.pnl.projected_double_days)) : "—", "approximate time for committed capital to double if the projected rate held")}
    </div></div>
    <div class="card-body--tight table-wrap">
      <table class="ledger">
        <thead><tr><th>idle slot</th><th>market</th><th>reasons</th></tr></thead>
        <tbody>
          ${idle.map((s) => `<tr>
            <td class="td-mono">${esc(s.slot ?? "—")}</td>
            <td class="td-mono">${esc(s.venue || "")}:${esc(s.symbol || "?")}</td>
            <td>${(s.reasons || []).map((r) => `<span class="badge badge--warn">${esc(r)}</span>`).join(" ") || "—"}</td>
          </tr>`).join("") || `<tr><td colspan="3"><div class="empty-note">No idle slots in the last report — every slot is pulling its weight.</div></td></tr>`}
        </tbody>
      </table>
    </div>
    <div class="card-body--tight table-wrap">
      <table class="ledger">
        <thead><tr><th>challenger</th><th>regime</th><th>score</th><th>harvest 24h</th></tr></thead>
        <tbody>
          ${(hunt.top3 || []).map((c) => `<tr>
            <td class="td-mono"><span class="venue-tag venue-tag--${esc(c.venue)}">${esc(c.venue)}</span>:${esc(c.symbol)}</td>
            <td><span class="badge badge--dim">${esc(c.regime || "?")}</span></td>
            <td class="td-mono">${esc(fmtNum(c.score_final, 1))}</td>
            <td class="td-mono ${(c.harvest_net_pct_24h || 0) >= 0 ? "m-value--good" : "m-value--bad"}">${c.harvest_net_pct_24h == null ? "—" : `${Number(c.harvest_net_pct_24h) >= 0 ? "+" : ""}${fmtNum(c.harvest_net_pct_24h, 2)}%`}</td>
          </tr>`).join("") || `<tr><td colspan="4"><div class="empty-note">No challenger hunt yet — the first cycle populates the top-3.</div></td></tr>`}
        </tbody>
      </table>
    </div>
    <div class="card-body--tight table-wrap">
      <table class="ledger">
        <thead><tr><th>vetoed slot</th><th>reason</th></tr></thead>
        <tbody>
          ${vetoes.map((v) => `<tr>
            <td class="td-mono">${esc(v.slot ?? "—")}</td>
            <td><div class="rationale" title="${esc(v.reason || "")}">${esc(v.reason || "—")}</div></td>
          </tr>`).join("") || `<tr><td colspan="2"><div class="empty-note">No recent vetoes — nothing blocked by the guard/churn bounds.</div></td></tr>`}
        </tbody>
      </table>
    </div>
    ${arbiterVerdictHTML(rep.arbiter)}`;
}

/* Last arbiter verdict (Mistral by default) — when the fast loop DID
   consult the model and what it said. Hidden when the loop has not
   needed an arbiter call yet (the band pre-filter skips the call when
   no swap is numerically possible — a healthy steady-state). */
function arbiterVerdictHTML(arb) {
  if (!arb || typeof arb !== "object") return "";
  const slot = arb.slot;
  const approve = arb.approve === true;
  const conf = isNum(arb.confidence) ? fmtNum(arb.confidence, 2) : "—";
  const pick = arb.challenger || "—";
  const reason = arb.reason || "";
  const degraded = arb.llm_degraded === true;
  const llm = arb.llm || (arb.provider || "mistral");
  const verdictBadge = approve
    ? `<span class="badge badge--ok">approve</span>`
    : `<span class="badge badge--bad">reject</span>`;
  return `<div class="card-body--tight table-wrap" style="border-top:1px solid var(--rule)">
    <div class="card-head" style="padding:6px 0 4px"><span class="card-title" style="font-size:12.5px">Last arbiter verdict</span>
      <span class="spacer"></span>
      ${degraded ? '<span class="badge badge--warn" title="LLM chain unavailable — rule fallback was used instead of the model">degraded</span>' : `<span class="mono" style="font-size:10.5px;color:var(--ink-faint)">${esc(llm)}</span>`}
    </div>
    <div class="mini-kv" style="padding:4px 0">
      <div class="row"><span class="k">verdict</span><span class="v">${verdictBadge} · slot ${esc(slot ?? "—")} → ${esc(pick)} · conf ${conf}</span></div>
      ${reason ? `<div class="row"><span class="k">reason</span><span class="v" title="${esc(reason)}">${esc(reason.slice(0, 200))}${reason.length > 200 ? "…" : ""}</span></div>` : ""}
    </div>
  </div>`;
}

/* ── position optimizer: latest per-bot analysis + sweep log ──────── */

/* The Pending/Applied tables below only carry recs gated at Δ≥2% — by
   design, so they sit empty most of the time. This card shows what the
   engine LAST concluded per bot (state.active_bots[*].position_optimizer,
   via ctl /status), including keeps and sub-threshold deltas, plus the
   journal sweep log underneath. */
function renderPositionAnalysis(st, sweeps) {
  const box = $("#opt-latest-analysis");
  if (!box) return;
  const ab = (st && typeof st.active_bots === "object" && st.active_bots) || {};
  const rows = Object.entries(ab).map(([slot, bot]) => {
    const po = (bot && typeof bot.position_optimizer === "object"
      && bot.position_optimizer) || {};
    return {
      slot, symbol: (bot || {}).symbol, venue: (bot || {}).venue,
      rec: po.last_recommendation ?? null,
      delta: isNum(po.last_delta_pct) ? Number(po.last_delta_pct) : null,
      conf: isNum(po.last_confidence) ? Number(po.last_confidence) : null,
      trigger: po.last_trigger ?? null,
      at: po.last_analyzed_at ?? null,
      hop: po.last_fetch_hop ?? null,
    };
  }).filter((r) => r.at != null || r.rec != null)
    .sort((a, b) => (Number(b.at) || 0) - (Number(a.at) || 0));

  const sweepList = (sweeps && Array.isArray(sweeps.sweeps)
    ? sweeps.sweeps : []).slice(0, 10);
  const recBadge = (rec) => rec === "keep"
    ? `<span class="badge badge--dim">keep</span>`
    : `<span class="badge badge--violet">${esc(rec || "?")}</span>`;

  box.innerHTML = `
    <div class="card-head"><span class="card-title">Latest position analysis</span>
      <span class="spacer"></span><span class="mono" style="font-size:11px;color:var(--ink-faint)" title="per-bot last analysis from state.active_bots[*].position_optimizer (all recs, including keeps and sub-threshold Δ — the Pending/Applied tables below only carry Δ≥2% gated recs)">${rows.length} bot${rows.length === 1 ? "" : "s"} analyzed</span></div>
    <div class="card-body--tight table-wrap">
      <table class="ledger">
        <thead><tr>
          <th>slot</th><th>market</th><th>rec</th><th>Δ%</th>
          <th>conf</th><th>trigger</th><th>analyzed</th><th>candle hop</th>
        </tr></thead>
        <tbody>
          ${rows.map((r) => `<tr>
            <td class="td-mono">${esc(r.slot)}</td>
            <td class="td-mono"><span class="venue-tag venue-tag--${esc(r.venue || "")}">${esc(r.venue || "")}</span>:${esc(r.symbol || "?")}</td>
            <td>${recBadge(r.rec)}</td>
            <td class="td-mono ${(r.delta || 0) >= 0 ? "m-value--good" : "m-value--bad"}">${r.delta == null ? "\u2014" : `${r.delta >= 0 ? "+" : ""}${fmtNum(r.delta, 2)}`}</td>
            <td class="td-mono">${r.conf == null ? "\u2014" : fmtNum(r.conf, 2)}</td>
            <td class="td-mono">${esc(r.trigger || "\u2014")}</td>
            <td class="td-mono" title="${esc(r.at != null ? String(r.at) : "")}">${esc(relTimeEpoch(r.at))}</td>
            <td>${r.hop == null ? "\u2014" : `<span class="badge badge--dim" title="how the analysis candles were fetched">${esc(r.hop)}</span>`}</td>
          </tr>`).join("") || `<tr><td colspan="8"><div class="empty-note">No bot has been analyzed yet \u2014 the position optimizer runs on its 15 min cadence (plus an on-entry pass after every deploy).</div></td></tr>`}
        </tbody>
      </table>
    </div>
    <details style="padding:10px 14px;border-top:1px solid var(--rule)">
      <summary class="mono" style="font-size:11px;color:var(--ink-faint);cursor:pointer">sweep history \u00b7 last ${sweepList.length} (journal)</summary>
      <ul class="feed" style="max-height:220px;overflow:auto">
        ${sweepList.map((e) => `<li><span class="f-at">${esc(String(e.at || "").replace("T", " ").slice(5, 16))}</span><span class="f-kind k--${esc(String(e.kind || "?").replace(/_/g, "-"))}">${esc(String(e.kind || "?").replace(/_/g, "-"))}</span><span class="f-msg">${esc(e.msg || "")}</span></li>`).join("") || `<li><span class="f-msg">No position-optimizer journal entries yet.</span></li>`}
      </ul>
    </details>`;
}

/* ── tvcli data sources: what the candle/confluence feeds found ───── */

/* Debugging surface for every tvcli-backed consumer: which hop served
   each candle fetch (direct / vision mirror / tvcli), and what the
   screen's /hunt confluence pass found per skill. All from ctl /status
   data_sources (fail-soft empty shapes). */
function renderDataSources(st) {
  const box = $("#opt-data-sources");
  if (!box) return;
  const ds = (st && typeof st.data_sources === "object"
    && st.data_sources) || null;
  if (!ds) {
    box.innerHTML = `
      <div class="card-head"><span class="card-title">tvcli data sources</span>
        <span class="spacer"></span><span class="badge badge--warn" title="ctl /status not responding">offline</span></div>
      <div class="card-body"><div class="empty-note">Data-source observability unavailable — this panel refills automatically once the daemon ctl plane is reachable again.</div></div>`;
    return;
  }
  const events = Array.isArray(ds.fetch_events) ? ds.fetch_events : [];
  const hs = (ds.hunt_stats && typeof ds.hunt_stats === "object")
    ? ds.hunt_stats : {};
  const skills = (hs.skills && typeof hs.skills === "object") ? hs.skills : {};
  const hopCounts = {};
  for (const e of events) {
    const h = e && e.hop;
    if (h) hopCounts[h] = (hopCounts[h] || 0) + 1;
  }
  const hops = Object.entries(hopCounts).sort((a, b) => b[1] - a[1]);
  const hopBadge = (h) => h === "tvcli"
    ? `<span class="badge badge--violet" title="TradingView WebSocket via the tvcli /fetch fallback">${esc(h)}</span>`
    : h === "vision"
      ? `<span class="badge badge--ok" title="Binance public data mirror (data-api.binance.vision)">${esc(h)}</span>`
      : `<span class="badge badge--dim" title="primary venue API (e.g. Hyperliquid)">${esc(h)}</span>`;
  const skillRows = Object.entries(skills).map(([name, s]) => {
    const hunted = Number((s || {}).hunted) || 0;
    const ok = Number((s || {}).ok) || 0;
    const allOk = hunted > 0 && ok === hunted;
    return `<tr>
      <td class="td-mono">${esc(name)}</td>
      <td class="td-mono">${ok}/${hunted}</td>
      <td>${allOk ? '<span class="badge badge--ok">all parsed</span>' : hunted === 0 ? '<span class="badge badge--dim">not hunted</span>' : `<span class="badge badge--warn">${hunted - ok} failed</span>`}</td>
    </tr>`;
  }).join("");

  box.innerHTML = `
    <div class="card-head"><span class="card-title">tvcli data sources</span>
      <span class="spacer"></span><span class="mono" style="font-size:10.5px;color:var(--ink-faint)" title="candle-hop attribution (market_regime fetch ring) + screen /hunt confluence counters — what the tvcli-backed systems found">${events.length ? `${events.length} recent fetch(es)` : "no fetches yet"}</span></div>
    <div class="card-body"><div class="mini-kv">
      <div class="row"><span class="k">candle hops</span><span class="v">${hops.length ? hops.map(([h, n]) => `${hopBadge(h)} \u00d7${n}`).join(" ") : "\u2014"}</span></div>
      <div class="row"><span class="k">confluence boosted</span><span class="v" title="candidates whose score_final the tvcli bonus moved in the last screen">${esc(String(hs.candidates_boosted ?? "\u2014"))} candidate(s)</span></div>
    </div></div>
    ${skillRows ? `<div class="card-body--tight table-wrap">
      <table class="ledger">
        <thead><tr><th>hunt skill</th><th>ok / hunted</th><th>state</th></tr></thead>
        <tbody>${skillRows}</tbody>
      </table>
    </div>` : `<div class="card-body"><div class="empty-note">No confluence hunt reported yet — the screen runs it over its top candidates (every rescreen).</div></div>`}
    <details style="padding:10px 14px;border-top:1px solid var(--rule)">
      <summary class="mono" style="font-size:11px;color:var(--ink-faint);cursor:pointer">candle fetch log \u00b7 last ${Math.min(events.length, 12)}</summary>
      <ul class="feed" style="max-height:200px;overflow:auto">
        ${events.slice(-12).reverse().map((e) => `<li><span class="f-at">${esc(relTimeEpoch(e && e.ts))}</span><span class="f-kind">${esc(String((e && e.venue) || "?"))}:${esc(String((e && e.symbol) || "?"))} ${esc(String((e && e.interval) || ""))}</span><span class="f-msg">${esc(String((e && e.hop) || "?"))} \u00b7 ${esc(String((e && e.rows) ?? "?"))} rows \u00b7 ${esc(String((e && e.ms) ?? "?"))} ms</span></li>`).join("") || `<li><span class="f-msg">No candle fetches recorded yet this daemon process.</span></li>`}
      </ul>
    </details>`;
}

/* ── fast-optimizer swap log + per-slot idle trackers ─────────────── */

/* Two tables: (1) per-slot idle timing — when each slot last saw a
   fill (the dials that drive the optimizer's idle flag), and (2) the
   swap_log itself — every swap the loop has ATTEMPTED with the ok/not
   verdict (a single cycle can record both a veto and the eventual
   succeed once a different challenger cleared). The last arbiter
   verdict is repeated here too in case the operator opened the tab
   directly without seeing renderFastOptimizer. */
function renderSwapLog(sl) {
  const idTrack = $("#opt-trackers");
  const idSwaps = $("#opt-swaps");
  if (!idTrack && !idSwaps) return;
  const trackers = (sl && sl.trackers) || [];
  const swaps = (sl && sl.swaps) || [];
  const arb = sl && sl.last_arbiter;
  const meta = `<span class="mono" style="font-size:10.5px;color:var(--ink-faint)">${esc(sl ? (sl.cycles || 0) : 0)} cycles · ${esc(sl ? (sl.swaps_total || 0) : 0)} swaps total</span>`;
  if (idTrack) {
    idTrack.innerHTML = `
      <div class="card-head"><span class="card-title">Per-slot idle trackers</span>
        <span class="spacer"></span>${meta}</div>
      <div class="card-body--tight table-wrap">
        <table class="ledger">
          <thead><tr><th>slot</th><th>last fills</th><th>idle (min)</th><th>last increase</th></tr></thead>
          <tbody>
            ${trackers.map((t) => {
              const idle = t.idle_min;
              const cls = idle == null ? "m-value--dim"
                : idle >= 60 ? "m-value--bad"
                : idle >= 15 ? "m-value--warn" : "m-value--dim";
              return `<tr>
                <td class="td-mono">${esc(t.slot ?? "—")}</td>
                <td class="td-mono">${isNum(t.last_fills) ? fmtNum(t.last_fills, 1) : "—"}</td>
                <td class="td-mono"><span class="${cls}">${idle == null ? "—" : fmtNum(idle, 0)}</span></td>
                <td class="td-mono">${t.last_increase_at ? esc(relTimeEpoch(t.last_increase_at)) : "—"}</td>
              </tr>`;
            }).join("") || `<tr><td colspan="4"><div class="empty-note">No slot trackers yet — the first optimize cycle populates them.</div></td></tr>`}
          </tbody>
        </table>
      </div>`;
  }
  if (idSwaps) {
    const arbHead = arb && typeof arb === "object"
      ? `<div class="card-head" style="padding:6px 0 0"><span class="card-title" style="font-size:12.5px">Last arbiter verdict</span>
          <span class="spacer"></span>
          ${arb.llm_degraded === true ? '<span class="badge badge--warn">degraded</span>' : `<span class="mono" style="font-size:10.5px;color:var(--ink-faint)">${esc(arb.llm || arb.provider || "mistral")}</span>`}
        </div>
        <div class="mini-kv" style="padding:4px 0 8px">
          <div class="row"><span class="k">verdict</span><span class="v">${arb.approve === true ? '<span class="badge badge--ok">approve</span>' : '<span class="badge badge--bad">reject</span>'} · slot ${esc(arb.slot ?? "—")} → ${esc(arb.challenger || "—")} · conf ${isNum(arb.confidence) ? fmtNum(arb.confidence, 2) : "—"}</span></div>
          ${arb.reason ? `<div class="row"><span class="k">reason</span><span class="v" title="${esc(arb.reason)}">${esc(arb.reason.slice(0, 200))}${arb.reason.length > 200 ? "…" : ""}</span></div>` : ""}
        </div>` : "";
    idSwaps.innerHTML = `
      ${arbHead}
      <div class="card-head"><span class="card-title">Swap log</span>
        <span class="spacer"></span><span class="mono" style="font-size:10.5px;color:var(--ink-faint)">last ${swaps.length}</span></div>
      <div class="card-body--tight table-wrap">
        <table class="ledger">
          <thead><tr><th>at</th><th>slot</th><th>verdict</th></tr></thead>
          <tbody>
            ${swaps.map((s) => `<tr>
              <td class="td-mono" title="${esc(s.at_iso || String(s.at || ""))}">${s.at_iso ? esc(String(s.at_iso).replace("T", " ").slice(5, 16)) : esc(relTimeEpoch(s.at))}</td>
              <td class="td-mono">${esc(s.slot ?? "—")}</td>
              <td>${s.ok ? '<span class="badge badge--ok">swapped</span>' : '<span class="badge badge--bad">vetoed</span>'}</td>
            </tr>`).join("") || `<tr><td colspan="3"><div class="empty-note">No swaps yet — the optimizer cycles every ${esc("2–5")} min; a swap only happens when the arbiter approves one inside the relaxed Δscore band.</div></td></tr>`}
          </tbody>
        </table>
      </div>`;
  }
}

/* ── reliability ──────────────────────────────────────────────────── */

async function loadReliability() {
  let rel;
  try { rel = await api("/api/reliability"); }
  catch (e) { toast(`reliability: ${e.message}`, true); return; }
  renderFreshnessBanner("rel-freshness", rel && rel.freshness);
  const ladder = rel.ladder || {};
  const archs = Object.entries(rel.archetypes || {}).sort((a, b) =>
    (b[1].samples || 0) - (a[1].samples || 0));

  // snapshot-staleness note: the ledger is a file snapshot refreshed by the
  // daemon's 24h health cycle — past that (+grace) it is stale evidence.
  const noteBox = $("#rel-note");
  if (noteBox) {
    const age = rel.ledger_age_h;
    const notes = [];
    if (rel.stale) {
      notes.push(`<div class="banner banner--warn"><div><div class="banner-title">Reliability ledger is a stale snapshot (${fmtNum(age, 1)}h old)</div>
        The 24h refresh cadence has been missed — the daemon may be down or its health cycle has not run. Treat every aggregate below as last-known, not live.</div></div>`);
    } else if (rel.missing || (rel.note && !rel.stale)) {
      notes.push(`<div class="banner banner--info"><div>${esc(rel.note || "No closed round-trips yet.")}</div></div>`);
    } else if (age != null) {
      notes.push(`<div class="banner banner--info"><div>Ledger snapshot age: <b>${fmtNum(age, 1)}h</b> (refresh cadence ${esc(rel.refresh_cadence_h ?? 24)}h).</div></div>`);
    }
    const anySynth = archs.some(([, s]) => (s.synthetic_samples || 0) > 0);
    if (anySynth) {
      notes.push(`<div class="banner banner--bad"><div><div class="banner-title">Synthetic/seeded samples pollute the ledger</div>
        Archetypes below carry seeded or backfilled samples (see the “real / synth” column). Expectancy and profit factor include them — they are not evidence from live round-trips.</div></div>`);
    }
    // Ladder thresholds card — pinned at the top so an operator can read
    // OFF the page exactly what an archetype needs to climb (or what
    // would kill it).
    const kt = (rel.kill_thresholds) || {};
    const full = ladder.full_samples || 30, probe = ladder.probe_samples || 10;
    notes.push(`<div class="banner banner--info" style="margin:8px 0 0"><div>
      <div class="banner-title">Sizing ladder thresholds</div>
      <div class="mini-kv" style="font-size:12px">
        <div class="row"><span class="k">base → probe</span><span class="v"><b>${probe}</b> closed samples</span></div>
        <div class="row"><span class="k">probe → full</span><span class="v"><b>${full}</b> closed samples AND PF ≥ <b>${ladder.pf_pass ?? 1.3}</b></span></div>
        <div class="row"><span class="k">recent PF kills archetype</span><span class="v">PF &lt; <b>${ladder.pf_kill ?? 1.0}</b> on last <b>${kt.recent_window ?? 20}</b> trips (binding only with ≥ <b>${kt.kill_min_samples ?? 10}</b> samples)</span></div>
        <div class="row"><span class="k">live gate</span><span class="v">≥ <b>${kt.live_min_samples ?? 30}</b> samples AND PF ≥ <b>${ladder.pf_pass ?? 1.3}</b> AND recent PF ≥ <b>${ladder.pf_kill ?? 1.0}</b></span></div>
      </div>
    </div></div>`);
    noteBox.innerHTML = notes.join("");
  }
  $("#rel-body").innerHTML = archs.map(([name, s]) => {
    const full = ladder.full_samples || 30, probe = ladder.probe_samples || 10;
    const pctFull = Math.min(100, ((s.samples || 0) / full) * 100);
    const tierBadge = {
      base: "badge--dim", probe: "badge--violet",
      full: "badge--ok", killed: "badge--bad",
    }[s.tier] || "badge--dim";
    const synth = s.synthetic_samples || 0;
    const real = s.real_samples ?? s.samples ?? 0;
    const synthCell = synth > 0
      ? `<span class="m-value--bad" title="${synth} synthetic/seeded samples pollute the aggregates">${real} / <b>${synth}</b></span>`
      : `${real} / 0`;
    const pfReal = s.profit_factor_real ?? s.profit_factor;
    const recentReal = s.recent_pf_real ?? s.recent_pf;
    const expReal = s.expectancy_usd_real ?? s.expectancy_usd;
    // ladder_next cell — tells the operator how many samples to the
    // NEXT tier, so the table answers "what unlocks the next ladder
    // rung" without clicking into a card.
    const ladderCell = s.tier === "killed"
      ? `<span class="badge badge--bad" title="recent PF &lt; 1.0 with ≥ kill_min_samples trips — refuses new deployments">kill-flagged</span>`
      : s.tier === "full"
      ? `<span class="mono" style="color:var(--ink-faint);font-size:10.5px">top rung</span>`
      : `${s.ladder_progress_pct != null ? `<span class="tier-track-mini"><span class="fill" style="width:${s.ladder_progress_pct.toFixed(1)}%"></span></span>` : ""}<span class="mono" style="font-size:10.5px;color:var(--ink-faint)">→ ${esc(s.ladder_next)} @ ${esc(s.ladder_next_at)}</span>`;
    return `<tr class="rel-row" data-arch="${esc(name)}" role="button" tabindex="0" title="click to expand recent closed round-trips">
      <td><span class="rel-chevron" aria-hidden="true">▸</span> <b>${esc(name)}</b></td>
      <td class="td-mono">${esc(s.samples ?? 0)}</td>
      <td class="td-mono">${synthCell}</td>
      <td><div class="tier-track" title="${esc(s.samples)} / ${full} samples to full">
        <div class="fill${s.tier === "killed" ? " fill--killed" : ""}" style="width:${pctFull.toFixed(1)}%"></div>
        <div class="mark" style="left:${(probe / full * 100).toFixed(1)}%" title="probe @${probe}"></div>
      </div></td>
      <td class="td-mono">${ladderCell}</td>
      <td class="td-mono ${(pfReal || 0) >= (ladder.pf_pass || 1.3) ? "m-value--good" : ""}"${synth ? ` title="includes ${synth} synthetic samples"` : ""}>${esc(fmtNum(pfReal, 2))}${synth ? "†" : ""}</td>
      <td class="td-mono ${(recentReal || 0) < (ladder.pf_kill || 1.0) ? "m-value--bad" : ""}">${esc(fmtNum(recentReal, 2))}</td>
      <td class="td-mono">${esc(fmtPct(s.win_rate))}</td>
      <td class="td-mono">${fmtUsd(expReal)}${synth ? "†" : ""}</td>
      <td class="td-mono">${fmtUsd(s.max_dd_usd)}</td>
      <td><span class="badge ${tierBadge}">${esc(s.tier)}</span></td>
    </tr>`;
  }).join("") || `<tr><td colspan="11"><div class="empty-note">No closed round-trips yet — the ledger fills as bots complete trades (24h refresh, or force one from Fleet).</div></td></tr>`;
  wireReliabilityExpansion();
}

/* Lazy-load /api/reliability/archive and inject a sub-row with the recent
   closed round-trips. First click fetches; subsequent clicks toggle. */
function wireReliabilityExpansion() {
  for (const row of document.querySelectorAll("#rel-body tr.rel-row")) {
    const open = () => toggleReliabilityRow(row);
    row.addEventListener("click", open);
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
  }
}

async function toggleReliabilityRow(row) {
  const next = row.nextElementSibling;
  if (next && next.classList.contains("rel-detail")) {
    next.remove();
    const chev = row.querySelector(".rel-chevron");
    if (chev) chev.textContent = "▸";
    return;
  }
  const arch = row.dataset.arch;
  const chev = row.querySelector(".rel-chevron");
  if (chev) chev.textContent = "▾";
  const det = document.createElement("tr");
  det.className = "rel-detail";
  det.innerHTML = `<td colspan="11"><div class="empty-note">Loading recent closed round-trips…</div></td>`;
  row.after(det);
  let resp;
  try { resp = await api(`/api/reliability/archive?limit=20`); }
  catch (e) {
    det.innerHTML = `<td colspan="11"><div class="empty-note">Trip history unavailable (${esc(e.message)})</div></td>`;
    return;
  }
  const trips = ((resp && resp.archetypes) || {})[arch] || [];
  if (!trips.length) {
    det.innerHTML = `<td colspan="11"><div class="empty-note">No archived trades for <b>${esc(arch)}</b> yet — archive grows when a bot rotates out.</div></td>`;
    return;
  }
  const rows = trips.map((t) => {
    const r = isNum(t.realized) ? Number(t.realized) : 0;
    const cls = r > 0 ? "m-value--good" : r < 0 ? "m-value--bad" : "m-value--dim";
    const hold = isNum(t.hold_s) ? formatHold(Number(t.hold_s)) : "—";
    const ts = t.ts ? esc(relTimeEpoch(t.ts / (t.ts > 1e12 ? 1000 : 1))) : "—";
    return `<tr>
      <td class="td-mono">${ts}</td>
      <td class="td-mono">${esc(t.symbol || "—")}</td>
      <td class="td-mono">${esc(t.venue || "—")}</td>
      <td class="td-mono ${cls}">${fmtSignedUsd(r)}${t.is_panic ? " <span class=\"badge badge--warn\" title=\"panic-exit\">P</span>" : ""}</td>
      <td class="td-mono">${hold}</td>
      <td>${t.is_synthetic ? '<span class="badge badge--bad" title="seeded/backfilled — does not count toward the ladder">synthetic</span>' : ""}</td>
    </tr>`;
  }).join("");
  det.innerHTML = `<td colspan="11">
    <div class="rel-detail-head">Recent closed round-trips — <b>${esc(arch)}</b> · last ${trips.length}</div>
    <table class="ledger rel-detail-table">
      <thead><tr><th>closed</th><th>symbol</th><th>venue</th><th>realized</th><th>hold</th><th>notes</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </td>`;
}

function formatHold(sec) {
  if (!isFinite(sec) || sec < 0) return "—";
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  if (sec < 86400) return `${(sec / 3600).toFixed(1)}h`;
  return `${(sec / 86400).toFixed(1)}d`;
}

/* ── config ───────────────────────────────────────────────────────── */

let configBaseline = {}; // path -> original value (numbers)

async function loadConfig() {
  let payload;
  try { payload = await api("/api/config"); }
  catch (e) { toast(`config: ${e.message}`, true); return; }
  renderFreshnessBanner("cfg-freshness", payload.freshness);
  const editable = payload.editable || {};
  configBaseline = {};
  const groups = new Map();
  for (const [path, rule] of Object.entries(editable)) {
    if (!groups.has(rule.group)) groups.set(rule.group, []);
    groups.get(rule.group).push([path, rule]);
    configBaseline[path] = rule.value;
  }
  const fields = $("#cfg-fields");
  fields.innerHTML = "";
  for (const [group, items] of groups) {
    fields.append(el("div", { class: "field-group-title" }, group));
    for (const [path, rule] of items) {
      configBaseline[path] = rule.value;
      const id = `cfg-${path.replace(/\./g, "-")}`;
      let control;
      if (rule.t === "bool") {
        // bool knobs (optimizer.enabled) carry no min/max — a number input
        // would render empty and self-flag dirty, and saving it would
        // coerce to 0. An on/off select keeps the value honest.
        control = el("select", { "data-path": path, "data-t": "bool", id },
          el("option", { value: "true" }, "on"),
          el("option", { value: "false" }, "off"));
        control.value = String(rule.value) === "true" ? "true" : "false";
        control.addEventListener("change", () => control.classList.toggle("dirty",
          control.value !== String(configBaseline[path])));
      } else {
        control = el("input", {
          type: "number", step: "any", value: rule.value ?? "",
          min: rule.min, max: rule.max, "data-path": path, id,
        });
        control.addEventListener("input", () => control.classList.toggle("dirty",
          Number(control.value) !== Number(configBaseline[path])));
      }
      const range = (rule.min != null || rule.max != null)
        ? `${rule.min ?? "—"} – ${rule.max ?? "—"}${rule.unit ? " " + rule.unit : ""}`
        : (rule.unit || "");
      fields.append(el("div", { class: "field" },
        el("div", { class: "f-name" }, rule.label,
          el("span", { class: "f-path" }, path)),
        control,
        el("div", { class: "f-range" }, range)));
    }
  }
  renderConfigReadonly(payload.config || {});
}

function renderConfigReadonly(cfg) {
  const skip = new Set(Object.keys(configBaseline));
  const rows = [];
  const flatten = (obj, prefix) => {
    for (const [k, v] of Object.entries(obj || {})) {
      const path = prefix ? `${prefix}.${k}` : k;
      if (v && typeof v === "object" && !Array.isArray(v)) { flatten(v, path); continue; }
      if (skip.has(path)) continue;
      rows.push([path, Array.isArray(v) ? JSON.stringify(v) : v]);
    }
  };
  flatten(cfg, "");
  const live = ((cfg.autonomy || {}).live_profiles) || [];
  let html = "";
  if (live.length) {
    html += `<div class="banner banner--bad" style="margin:0 0 12px;">
      <div><div class="banner-title">live_profiles is non-empty</div>
      Real-money deployment is armed in config (WT-era field). This workspace's mission is dry-run only — see state/engine.json — so live_profiles is informational here.</div></div>`;
  }
  html += rows.map(([k, v]) =>
    `<div class="kv-row"><span class="k">${esc(k)}</span><span class="v">${esc(v === null || v === undefined ? "—" : v)}</span></div>`).join("");
  $("#cfg-readonly").innerHTML = html;
}

$("#cfg-save").addEventListener("click", async () => {
  const edits = {};
  for (const control of $("#cfg-fields").querySelectorAll("[data-path]")) {
    if (!control.classList.contains("dirty")) continue;
    if (control.dataset.t === "bool") {
      edits[control.dataset.path] = control.value === "true";
    } else if (control.value !== "") {
      edits[control.dataset.path] = Number(control.value);
    }
  }
  const keys = Object.keys(edits);
  if (!keys.length) { toast("No changes to save."); return; }
  const { ok } = await confirmDialog({
    title: "Apply config changes",
    body: [el("div", {}, `Writing `, el("code", {}, "config.yaml"),
      ` — ${keys.length} value${keys.length > 1 ? "s" : ""}: `),
      el("div", { class: "mono", style: "font-size:12px;margin-top:6px;" },
        keys.map((k) => `${k} → ${edits[k]}`).join(", "))],
    label: "Write config",
  });
  if (!ok) return;
  try {
    const resp = await api("/api/config", { method: "POST", body: { edits } });
    const applied = (resp.applied || []).length;
    const rejected = resp.rejected || [];
    toast(`Wrote ${applied} value${applied === 1 ? "" : "s"} (backup kept).`);
    for (const r of rejected) toast(`rejected ${r.path}: ${r.reason}`, true);
    $("#config-banner").innerHTML = `<div class="banner banner--info">
      <div><div class="banner-title">Config written — restart required</div>
      The daemon reads config.yaml at startup. Restart it to apply.</div>
      <button class="btn btn--primary" id="cfg-restart" style="margin-left:auto">Restart daemon</button></div>`;
    $("#cfg-restart").addEventListener("click", ctlRestart);
    loadConfig();
  } catch (e) {
    toast(`config save failed: ${e.message}`, true);
  }
});

/* ── llm providers ────────────────────────────────────────────────── */

const LLM_PROVIDER_LABELS = { cf: "Cloudflare", nvidia: "NVIDIA", openrouter: "OpenRouter", mistral: "Mistral" };
const LLM_MASK = "•"; // never a real key; empty/sentinel means "keep existing"
const LLM_CLEAR = "__CLEAR__"; // explicit-delete sentinel for /api/llm

let llmState = null; // last GET /api/llm payload (providers, chain, roles)

async function loadLlm() {
  let p;
  try { p = await api("/api/llm"); }
  catch (e) { toast(`llm: ${e.message}`, true); return; }
  llmState = p;
  renderLlmLadder(p);
  renderLlmProviders(p);
  renderLlmMatrix(p);
  $("#llm-sidecar-note").textContent = p.sidecar
    ? "sidecar: state/llm.env present"
    : "sidecar: none yet — save to create state/llm.env";
}

function renderLlmLadder(p) {
  const chain = p.chain || [];
  const ladder = $("#llm-ladder");
  ladder.innerHTML = "";
  chain.forEach((name, i) => {
    const prov = p.providers[name] || {};
    const chip = el("span", { class: "llm-chip" },
      el("span", { class: "llm-chip-idx" }, String(i + 1)),
      el("span", { class: "llm-chip-name" }, LLM_PROVIDER_LABELS[name] || name),
      el("span", { class: "llm-chip-key " + (prov.key_present ? "has-key" : "no-key") },
        prov.key_present ? "key" : "no key"),
      el("button", { class: "llm-chip-btn", title: "move up", onclick: () => moveChain(i, -1) }, "↑"),
      el("button", { class: "llm-chip-btn", title: "move down", onclick: () => moveChain(i, 1) }, "↓"));
    ladder.append(chip);
  });
}

function moveChain(i, delta) {
  if (!llmState) return;
  const chain = (llmState.chain || []).slice();
  const j = i + delta;
  if (j < 0 || j >= chain.length) return;
  [chain[i], chain[j]] = [chain[j], chain[i]];
  llmState.chain = chain;
  renderLlmLadder(llmState);
}

function renderLlmProviders(p) {
  const wrap = $("#llm-providers");
  wrap.innerHTML = "";
  for (const name of ["cf", "nvidia", "openrouter", "mistral"]) {
    const prov = p.providers[name] || {};
    const enabled = (p.chain || []).includes(name);
    const modelInput = el("input", { type: "text", class: "llm-model",
      value: prov.model || "", spellcheck: "false", "data-prov": name,
      placeholder: "model id" });
    const keyInput = el("input", { type: "password", class: "llm-key",
      value: prov.key_present ? LLM_MASK : "", "data-prov": name,
      placeholder: prov.key_present ? "key set (leave to keep)" : "paste API key" });
    // typing anything (except the mask sentinel) marks the key as "will set".
    keyInput.addEventListener("input", () => {
      const v = keyInput.value;
      keyInput.dataset.dirty = (v && v !== LLM_MASK) ? "1" : "";
    });
    // Per-provider "clear key" — only visible when a key is already stored.
    // Sets the input to the __CLEAR__ sentinel and marks it dirty so the
    // next save POSTs an explicit delete; the server's apply_llm strips
    // the key from the sidecar and the provider falls out of the chain.
    const clearBtn = prov.key_present
      ? el("button", { class: "btn btn--ghost llm-key-clear", title: "remove the stored API key",
          onclick: () => {
            keyInput.value = LLM_CLEAR;
            keyInput.dataset.dirty = "1";
            keyInput.classList.add("llm-key-clearing");
            clearBtn.disabled = true;
            clearBtn.textContent = "will clear on save";
            setTimeout(() => { keyInput.classList.remove("llm-key-clearing"); }, 800);
          } }, "clear key")
      : null;
    const validateBtn = el("button", { class: "btn btn--ghost llm-validate-btn",
      onclick: () => validateProvider(name) }, "validate");
    const status = el("span", { class: "mono llm-prov-status", id: `llm-status-${name}` }, "");
    const row = el("div", { class: "llm-prov-row" + (enabled ? "" : " is-off") },
      el("div", { class: "llm-prov-head" },
        el("span", { class: "llm-prov-name" }, LLM_PROVIDER_LABELS[name]),
        el("span", { class: "badge " + (prov.key_present ? "badge--ok" : "badge--warn") },
          prov.key_present ? "key set" : "no key"),
        el("label", { class: "llm-toggle" },
          el("input", { type: "checkbox", "data-enable": name, checked: enabled,
            onchange: (e) => toggleProvider(name, e.target.checked) }),
          el("span", {}, "enabled")),
        el("span", { class: "spacer" }),
        validateBtn, status),
      el("div", { class: "llm-prov-fields" },
        el("label", { class: "llm-field" }, el("span", { class: "llm-field-l" }, "model"),
          modelInput),
        el("label", { class: "llm-field" }, el("span", { class: "llm-field-l" }, "API key"),
          keyInput, clearBtn ? clearBtn : "")));
    wrap.append(row);
  }
}

function toggleProvider(name, on) {
  if (!llmState) return;
  let chain = (llmState.chain || []).slice();
  if (on && !chain.includes(name)) chain.push(name);
  if (!on) chain = chain.filter((x) => x !== name);
  llmState.chain = chain;
  renderLlmLadder(llmState);
  // reflect enabled styling without a full re-render (keeps input values)
  document.querySelectorAll(".llm-prov-row").forEach((row) => {
    const en = row.querySelector(`[data-enable]`);
    if (en && en.dataset.enable === name) row.classList.toggle("is-off", !on);
  });
}

function renderLlmMatrix(p) {
  const roles = p.roles || {};
  const matrix = $("#llm-matrix");
  matrix.innerHTML = "";
  const opts = ["", "cf", "nvidia", "openrouter", "mistral"]; // "" = follow chain
  for (const role of (p.role_keys || [])) {
    const select = el("select", { class: "llm-role-select", "data-role": role });
    for (const o of opts) {
      const opt = el("option", { value: o }, o === "" ? "follow chain" : (LLM_PROVIDER_LABELS[o] || o));
      if ((roles[role] || "") === o) opt.selected = true;
      select.append(opt);
    }
    select.addEventListener("change", () => {
      if (!llmState) return;
      llmState.roles = llmState.roles || {};
      if (select.value) llmState.roles[role] = select.value;
      else delete llmState.roles[role];
    });
    const cell = el("div", { class: "llm-role-cell" },
      el("span", { class: "llm-role-name mono" }, role.replace(/_/g, " ")),
      select);
    matrix.append(cell);
  }
}

async function validateProvider(name) {
  const status = $(`#llm-status-${name}`);
  if (status) status.textContent = "pinging…";
  try {
    const resp = await api("/api/llm/validate", { method: "POST", body: {} });
    const r = (resp.results || []).find((x) => x.provider === name);
    if (!r) { toast(`no result for ${name}`); return; }
    setStatus(name, r.ok, r.latency_ms, r.error);
    // update all rows' statuses we received, and the validate-all note
    for (const res of resp.results || []) {
      if (res.provider !== name) setStatus(res.provider, res.ok, res.latency_ms, res.error);
    }
    const okCount = (resp.results || []).filter((x) => x.ok).length;
    $("#llm-validate-note").textContent =
      `${okCount}/${(resp.results || []).length} ok`;
  } catch (e) {
    if (status) status.textContent = "failed";
    toast(`validate ${name}: ${e.message}`, true);
  }
}

function setStatus(name, ok, latency, err) {
  const elm = $(`#llm-status-${name}`);
  if (!elm) return;
  elm.textContent = ok ? `ok ${latency}ms` : "FAIL";
  elm.className = "mono llm-prov-status " + (ok ? "ok" : "fail");
  elm.title = ok ? "" : (err || "");
}

$("#llm-validate-all").addEventListener("click", async () => {
  const note = $("#llm-validate-note");
  note.textContent = "pinging…";
  for (const name of ["cf", "nvidia", "openrouter", "mistral"]) {
    await validateProvider(name);
  }
});

$("#llm-save").addEventListener("click", async () => {
  if (!llmState) { toast("load LLM config first"); return; }
  const providers = {};
  for (const name of ["cf", "nvidia", "openrouter", "mistral"]) {
    const prov = llmState.providers[name] || {};
    // model from the DOM, not llmState — the input has no state-sync
    // listener, so prov.model is whatever was loaded from the server, not
    // what was typed. Blank falls back to the loaded value (the server
    // leaves the stored model untouched when the value is blank).
    const modelInput = document.querySelector(`.llm-model[data-prov="${name}"]`);
    const entry = { model: (modelInput && modelInput.value.trim()) || prov.model || "" };
    const keyInput = document.querySelector(`.llm-key[data-prov="${name}"]`);
    if (keyInput && keyInput.dataset.dirty === "1") entry.key = keyInput.value;
    providers[name] = entry;
  }
  const body = {
    providers,
    chain: llmState.chain,
    roles: llmState.roles || {},
  };
  try {
    const resp = await api("/api/llm", { method: "POST", body });
    toast("LLM config saved — applied at the next LLM call (no restart).");
    await loadLlm();
  } catch (e) {
    toast(`llm save failed: ${e.message}`, true);
  }
});

/* ── logs ─────────────────────────────────────────────────────────── */

let logStick = true;
// append-only diff state: the filter + last rendered line used to decide
// whether the next poll can append a delta or must re-render the window.
let logPrevGrep = "";
let logPrevLines = "";
let logPrevLast = "";

async function loadLogs(force = false) {
  if (activeView !== "logs" && !force) return;
  const follow = $("#log-follow").checked;
  const grep = ($("#log-grep").value || "").trim();
  const lines = $("#log-lines").value;
  const params = new URLSearchParams({
    lines,
    ...(grep ? { grep } : {}),
  });
  let data;
  try { data = await api(`/api/logs?${params}`); }
  catch (e) { return; }
  // label the view with the file actually being tailed — under launchd the
  // daemon log lives elsewhere and a hard-coded path would lie
  const logPath = $("#log-path");
  if (logPath) {
    if (data.source === "workspace-tail" && (data.sources || []).length) {
      logPath.textContent = data.sources.map((s) => s.label).join(" + ");
    } else {
      logPath.textContent = data.path || logPath.textContent;
    }
    if (data.source) logPath.title = `source: ${data.source}`;
  }
  // Workspace-tail (no WT-era daemon): show a banner explaining the merge +
  // a freshness block listing the per-source last-written age.
  if (data.source === "workspace-tail") {
    const sources = data.sources || [];
    const rows = sources.map((s) => {
      const age = (() => { try { return Math.round((Date.now() - s.path.__mtime)/1000); } catch(_) { return null; } })();
      return `<tr><td class="td-mono">${esc(s.label)}</td><td class="td-mono">${esc(s.path)}</td></tr>`;
    }).join("");
    renderFreshnessBanner("log-freshness", {
      at: Math.max(...sources.map((s) => { try { return new Date(s.path).getTime() / 1000; } catch(_) { return null; } }).filter(Boolean)),
      at_iso: new Date().toISOString(),
      age_s: 0,
      path: sources.map((s) => s.path).join(" + "),
      kind: "workspace-tail (live log merge)",
      note: data.note,
    });
  } else {
    renderFreshnessBanner("log-freshness", {
      at: null, at_iso: null, age_s: null,
      path: data.path, kind: "daemon.log",
      note: data.source || "launchd-supervised daemon log",
    });
  }
  const newLines = data.lines || [];
  const box = $("#logbox");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  // Append-only diff mode: while following with an unchanged filter, keep
  // the rendered window and only append the lines that appeared since the
  // last poll (highlighted), instead of replacing the whole box. Anchor is
  // the previous last line — if it still occurs in the new window, the
  // lines after it are the delta; if the tail rolled past it (too much
  // output, rotation, truncation), fall back to a full re-render.
  const sameFilter = logPrevGrep === grep && logPrevLines === lines;
  if (follow && sameFilter && logPrevLast && newLines.length) {
    const anchor = logPrevLast;
    const j = newLines.lastIndexOf(anchor);
    if (j >= 0) {
      const fresh = newLines.slice(j + 1);
      if (fresh.length) {
        const frag = document.createDocumentFragment();
        for (const ln of fresh) {
          const div = document.createElement("div");
          div.className = "log-line log-line--new";
          div.textContent = ln;
          frag.append(div);
        }
        box.append(frag);
        $("#log-count").textContent = `${data.total} line(s)`;
        if (atBottom) box.scrollTop = box.scrollHeight;
        logPrevLast = newLines[newLines.length - 1];
        return;
      }
      return; // no new lines — nothing to paint
    }
  }
  logPrevGrep = grep; logPrevLines = lines;
  logPrevLast = newLines.length ? newLines[newLines.length - 1] : "";
  box.textContent = "";
  if (!newLines.length) {
    box.textContent = "— no matching lines —";
  } else {
    const frag = document.createDocumentFragment();
    for (const ln of newLines) {
      const div = document.createElement("div");
      div.className = "log-line";
      div.textContent = ln;
      frag.append(div);
    }
    box.append(frag);
  }
  $("#log-count").textContent = `${data.total} line(s)`;
  if (follow && (logStick || atBottom)) {
    box.scrollTop = box.scrollHeight;
  }
}
$("#log-grep").addEventListener("input", () => loadLogs());
$("#log-lines").addEventListener("change", () => loadLogs());
$("#log-follow").addEventListener("change", () => loadLogs());
$("#logbox").addEventListener("scroll", () => {
  const box = $("#logbox");
  logStick = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
});

/* ── controls ─────────────────────────────────────────────────────── */

$("#ctl-halt").addEventListener("click", async () => {
  const { ok } = await confirmDialog({
    title: "Halt the daemon",
    body: [el("div", {}, "Writes the ", el("code", {}, "KILL"), " file. The daemon halts at the next loop tick; the freqtrade engine keeps running until you stop it via grid/dev.")],
    label: "Arm KILL", danger: true,
  });
  if (!ok) return;
  try {
    await api("/api/ctl/kill", { method: "POST", body: { confirm: true } });
    toast("KILL armed — daemon halts at the next tick.", true);
    loadOverview();
  } catch (e) { toast(`kill failed: ${e.message}`, true); }
});

async function ctlUnkill() {
  const { ok } = await confirmDialog({
    title: "Clear the KILL file",
    body: [el("div", {}, "Allows the daemon to keep running / start again.")],
    label: "Clear KILL",
  });
  if (!ok) return;
  try {
    await api("/api/ctl/unkill", { method: "POST", body: { confirm: true } });
    toast("KILL cleared.");
    loadOverview();
  } catch (e) { toast(`unkill failed: ${e.message}`, true); }
}
$("#ctl-unkill").addEventListener("click", ctlUnkill);

/* ── dev maintenance (the single `dev` script, run detached by the backend) ── */

async function devAction(action, body, title, lines, label) {
  const { ok } = await confirmDialog({
    title,
    body: lines.map((t) => el("div", {}, t)),
    label,
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await api(`/api/dev/${action}`, { method: "POST", body: { confirm: true, ...body } });
    toast(`${action} started — output in state/logs/dev.log; console may restart.`, true, 6500);
    setTimeout(() => location.reload(), 6000);
    return r;
  } catch (e) { toast(`${action} failed: ${e.data && e.data.error || e.message}`, true, 6500); }
}

$("#dev-clean").addEventListener("click", () => devAction("clean", {},
  "Clear logs & runtime artifacts",
  ["Run cards, market-map caches, watch specs, daemon/console/PB logs.",
   "Daemon state, decisions and the reliability ledger are kept."],
  "Clean"));

$("#dev-reset").addEventListener("click", devResetDialog);

async function devResetDialog() {
  const { ok, checked } = await confirmDialog({
    title: "Reset the system",
    body: [
      el("div", {}, "Stops the whole stack (console + freqtrade engine) and wipes runtime state:"),
      el("div", {}, "state.json, decisions journal, reliability ledger + archive, run cards, market caches, watch specs, logs, PocketBase data."),
      el("div", {}, "grid/config.yaml is NOT touched. The dry-run trades db (tradesv3.dryrun.sqlite) is preserved across reset. A backup is kept under state/backups/."),
    ],
    label: "Reset system",
    danger: true,
    checkbox: "Keep the learning journal (decisions + reliability)",
  });
  if (!ok) return;
  try {
    await api("/api/dev/reset", {
      method: "POST",
      body: { confirm: true, keep_decisions: !!checked, start: false },
    });
    toast("reset started — the console and engine are stopping; run `grid/dev start` (or wait) and reload.", true, 8000);
    setTimeout(() => location.reload(), 6000);
  } catch (e) { toast(`reset failed: ${e.data && e.data.error || e.message}`, true, 6500); }
}

async function ctlStart(livePaper) {
  const isDryRun = lastOverview && lastOverview.engine && lastOverview.engine.engine === "freqtrade";
  const { ok, checked } = await confirmDialog({
    title: isDryRun ? "Start mission (dry-run)" : "Start mission",
    body: [el("div", {},
      isDryRun
        ? "Boots the mission console (:8798) and the freqtrade dry-run engine (grid/dev). " +
          "GridStrategy on BTC/USDC:USDC 1h, Hyperliquid public data. No real orders — " +
          "the instance config hardcodes dry_run: true."
        : (livePaper
            ? "Legacy WT-era mode is retired; this workspace is dry-run only."
            : "Plans and journals everything, creates nothing."))],
    label: "Start", danger: false,
    checkbox: null,
  });
  if (!ok) return;
  try {
    const r = await api("/api/daemon/start", {
      method: "POST",
      body: { confirm: true, live_paper: !!livePaper, clear_kill: !!checked },
    });
    if (r.error) {
      toast(`start: ${r.error}`, true, 6500);
    } else {
      toast(r.starting ? "Mission starting… (console + engine via grid/dev)"
                       : "Start accepted.", false, 4000);
      setTimeout(loadOverview, 3500);
    }
  } catch (e) { toast(`start failed: ${e.data && e.data.error || e.message}`, true, 6500); }
}

async function ctlRestart(livePaper) {
  const { ok, checked } = await confirmDialog({
    title: "Restart mission",
    body: [el("div", {},
      "Stop the console + freqtrade engine, then bring them back up via grid/dev. " +
      "Unapplied config takes effect after restart. The WT-era launchd brain " +
      "is closed in this workspace — grid/dev is the supervisor.")],
    label: "Restart", danger: false,
    checkbox: null,
  });
  if (!ok) return;
  try {
    const body = { confirm: true, clear_kill: !!checked };
    if (livePaper === true) body.live_paper = true;
    if (livePaper === false) body.live_paper = false;
    const r = await api("/api/daemon/restart", { method: "POST", body });
    if (r.error) {
      toast(`restart: ${r.error}`, true, 6500);
    } else {
      toast(r.restarting ? "Mission restarting… (grid/dev stop+start in background)"
                         : "Restart accepted.", false, 4000);
      setTimeout(loadOverview, 4000);
    }
  } catch (e) { toast(`restart failed: ${e.data && e.data.error || e.message}`, true, 6500); }
}
$("#ctl-restart").addEventListener("click", () => ctlRestart());

$("#ctl-stop").addEventListener("click", async () => {
  const { ok, checked } = await confirmDialog({
    title: "Stop the mission",
    body: [el("div", {},
      "Stops the console (:8798) and the freqtrade engine via grid/dev. " +
      "Any open dry-run trades are left as-is in the tradesv3.dryrun.sqlite " +
      "so the next start resumes from the same state.")],
    label: "Stop", danger: true,
    checkbox: "Force-kill (SIGKILL) if it ignores SIGTERM",
  });
  if (!ok) return;
  try {
    const r = await api("/api/daemon/stop", {
      method: "POST", body: { confirm: true, force: checked },
    });
    toast(r.stopping ? "Mission stopping… (refresh in ~5s)" : "Stop rejected.", !r.stopping);
    if (r.stopping) setTimeout(loadOverview, 2500);
    loadOverview();
  } catch (e) { toast(`stop failed: ${e.data && e.data.error || e.message}`, true); }
});

/* ── boot + polling ───────────────────────────────────────────────── */

let tick = 0;
setInterval(() => {
  if (document.hidden) return;
  tick++;
  loadOverview(); // cheap local reads; keeps the statusbar honest everywhere
  if (tick % 6 === 0) loadPnlTimeline(); // PnL history (PB query) — every 30s
  if (activeView === "optimizer" && tick % 4 === 0) loadOptimizer();
  if (activeView === "decisions" && tick % 4 === 0) loadDecisions();
  if (activeView === "logs" && tick % 2 === 0) loadLogs();
}, 5000);

async function boot() {
  const hash = (location.hash || "#fleet").slice(1);
  selectView(VIEWS.includes(hash) ? hash : "fleet");
  loadOverview();
  loadPnlTimeline();
  try {
    const meta = await api("/api/meta");
    const modeLabel = (lastOverview && lastOverview.daemon && lastOverview.daemon.mode) || "—";
    const eng = (lastOverview && lastOverview.engine) || meta.engine || null;
    $("#mission-sub").textContent = eng
      ? `mission console · engine: ${eng.engine}${eng.mode ? ` (${eng.mode})` : ""} · daemon: ${modeLabel}`
      : `mission console · ${modeLabel} · connecting…`;
    $("#footnote").textContent =
      `grid/autonomy console · ${modeLabel} · console :${meta.console_port} · supervisor ${meta.supervisor} · pb ${meta.pocketbase.replace("http://", "")}`;
  } catch { /* header/footnote stay default */ }
}
boot();
