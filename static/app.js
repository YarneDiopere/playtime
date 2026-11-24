"use strict";

// ------------------------------------------------------------------ helpers

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const SERIES_VARS = ["--series-1", "--series-2", "--series-3", "--series-4", "--series-5", "--series-6"];
const MAX_SERIES = SERIES_VARS.length;
const SYMBOL_TO_CODE = { "$": "USD", "US$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₽": "RUB", "zł": "PLN", "R$": "BRL", "CDN$": "CAD", "A$": "AUD", "kr": "SEK", "CHF": "CHF" };

let CONFIG = { regions: ["us"], default_region: "us", me: null };
let state = null; // current profile view state

// Static mode (GitHub Pages build): read-only, data comes from pre-built JSON files.
const STATIC = !!window.PLAYTIME_STATIC;

function staticPath(path) {
  if (path === "/api/config") return "data/config.json";
  let m = path.match(/^\/api\/profile\/(\d{17})\/timeline/);
  if (m) return `data/${m[1]}-timeline.json`;
  m = path.match(/^\/api\/profile\/(\d{17})(\?|$)/);
  if (m) return `data/${m[1]}.json`;
  return null;
}

async function api(path, opts = {}) {
  if (STATIC) {
    const file = staticPath(path);
    if (!file || (opts.method && opts.method !== "GET")) throw new Error("Not available on this read-only page.");
    const res = await fetch(file, { cache: "no-cache" });
    if (!res.ok) throw new Error("This profile isn't published here.");
    return res.json();
  }
  const res = await fetch(path, { ...opts, headers: { "X-Requested-With": "fetch", ...(opts.headers || {}) } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `Request failed (${res.status})`);
  return body;
}

function hours(min, digits) {
  const h = min / 60;
  if (digits === undefined) digits = h >= 100 ? 0 : 1;
  return h.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}
function duration(min) {
  min = Math.round(min || 0);
  if (!min) return "0 min";
  if (min < 60) return `${min} min`;
  return `${hours(min)} h`;
}
function money(cents, currency) {
  if (cents == null) return "—";
  const code = currency && currency.length === 3 && /^[A-Z]{3}$/.test(currency) ? currency : SYMBOL_TO_CODE[currency];
  if (code) {
    try { return new Intl.NumberFormat(undefined, { style: "currency", currency: code }).format(cents / 100); } catch {}
  }
  return `${currency || ""}${(cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
function ago(ts) {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400 * 2) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
const fmtDate = (ts) => (ts ? new Date(ts * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "—");
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const iconUrl = (g) => (g.img_icon ? `https://media.steampowered.com/steamcommunity/public/images/apps/${g.appid}/${g.img_icon}.jpg` : "");

// ------------------------------------------------------------------ tooltip

const tip = $("#tooltip");
function showTip(html, ev) {
  tip.innerHTML = html;
  tip.hidden = false;
  moveTip(ev);
}
function moveTip(ev) {
  const pad = 14;
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + r.width > window.innerWidth - 8) x = ev.clientX - r.width - pad;
  if (y + r.height > window.innerHeight - 8) y = ev.clientY - r.height - pad;
  tip.style.left = `${Math.max(8, x)}px`;
  tip.style.top = `${Math.max(8, y)}px`;
}
const hideTip = () => { tip.hidden = true; };

// ------------------------------------------------------------------ stacked bar chart

/** Round tick step (1/2/2.5/5 × 10^k) in display units, e.g. unit=60 for minutes shown as hours. */
function niceStep(max, unit, ticks) {
  const raw = Math.max(max / unit / ticks, 1e-9);
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= raw) return m * p * unit;
  return 10 * p * unit;
}

/**
 * buckets: [{ label, title, values: {seriesKey: number} }]
 * series:  [{ key, name, color }]  (stacked bottom-up in this order)
 */
function renderBars(el, { buckets, series, fmt, axisFmt, height = 240, unit = 1 }) {
  const draw = () => {
    const W = el.clientWidth || 600;
    const m = { l: 48, r: 8, t: 10, b: 26 };
    const iw = W - m.l - m.r, ih = height - m.t - m.b;
    const totals = buckets.map((b) => series.reduce((s, se) => s + (b.values[se.key] || 0), 0));
    const step = niceStep(Math.max(...totals, 0), unit, 4);
    const nTicks = Math.max(1, Math.ceil(Math.max(...totals, 0) / step));
    const max = step * nTicks;
    const band = iw / Math.max(buckets.length, 1);
    const bw = Math.max(1, Math.min(36, band * 0.72));
    const y = (v) => m.t + ih - (v / max) * ih;
    const surface = cssVar("--surface-1");

    let svg = `<svg viewBox="0 0 ${W} ${height}" height="${height}" role="img" aria-label="Bar chart">`;
    // grid + y axis
    svg += `<g class="axis">`;
    for (let i = 0; i <= nTicks; i++) {
      const v = step * i, yy = y(v);
      if (i > 0) svg += `<line class="gridline" x1="${m.l}" x2="${W - m.r}" y1="${yy}" y2="${yy}"/>`;
      svg += `<text x="${m.l - 8}" y="${yy + 4}" text-anchor="end">${esc(axisFmt(v))}</text>`;
    }
    // x labels: ~6 evenly spaced
    const every = Math.max(1, Math.ceil(buckets.length / Math.max(2, Math.floor(iw / 90))));
    buckets.forEach((b, i) => {
      if (i % every !== 0) return;
      const cx = m.l + band * i + band / 2;
      svg += `<text x="${cx}" y="${height - 6}" text-anchor="middle">${esc(b.label)}</text>`;
    });
    svg += `</g>`;

    // bars
    buckets.forEach((b, i) => {
      const x = m.l + band * i + (band - bw) / 2;
      const segs = series.map((se) => ({ se, v: b.values[se.key] || 0 })).filter((s) => s.v > 0);
      let acc = 0;
      svg += `<g${b.faded ? ' opacity="0.42"' : ""}>`;
      segs.forEach((s, j) => {
        const y0 = y(acc), y1 = y(acc + s.v);
        acc += s.v;
        const isTop = j === segs.length - 1;
        // 2px surface gap between stacked segments (visible spacer, not a stroke on the mark)
        const top = isTop ? y1 : Math.min(y1 + 2, y0);
        const h = y0 - top;
        if (h <= 0.2) return;
        if (isTop) {
          const r = Math.min(4, bw / 2, h);
          svg += `<path fill="${s.se.color}" d="M${x},${y0} V${top + r} Q${x},${top} ${x + r},${top} H${x + bw - r} Q${x + bw},${top} ${x + bw},${top + r} V${y0} Z"/>`;
        } else {
          svg += `<rect fill="${s.se.color}" x="${x}" y="${top}" width="${bw}" height="${h}"/>`;
        }
      });
      svg += `</g>`;
    });
    svg += `<line class="baseline" x1="${m.l}" x2="${W - m.r}" y1="${y(0)}" y2="${y(0)}"/>`;
    // hit targets: full column, wider than the mark
    buckets.forEach((b, i) => {
      svg += `<rect class="hit" data-i="${i}" x="${m.l + band * i}" y="${m.t}" width="${band}" height="${ih}"/>`;
    });
    svg += `</svg>`;
    el.innerHTML = svg;

    el.querySelectorAll(".hit").forEach((r) => {
      const b = buckets[+r.dataset.i];
      const total = totals[+r.dataset.i];
      const rows = series
        .map((se) => ({ se, v: b.values[se.key] || 0 }))
        .filter((s) => s.v > 0)
        .sort((a, c) => c.v - a.v)
        .map((s) => `<div class="t-row"><span><i style="background:${s.se.color}"></i>${esc(s.se.name)}</span><span class="t-val">${esc(fmt(s.v))}</span></div>`)
        .join("");
      const totalRow = series.length > 1 ? `<div class="t-row" style="margin-top:4px"><span class="secondary">Total</span><span class="t-val"><b>${esc(fmt(total))}</b></span></div>` : "";
      const footer = b.footer ? `<div class="t-src">${b.footer}</div>` : "";
      const html = `<div class="t-title">${esc(b.title)}</div>${rows || '<div class="muted">Nothing recorded</div>'}${totalRow}${footer}`;
      r.addEventListener("mouseenter", (ev) => showTip(html, ev));
      r.addEventListener("mousemove", moveTip);
      r.addEventListener("mouseleave", hideTip);
    });
  };
  draw();
  if (el._ro) el._ro.disconnect();
  el._ro = new ResizeObserver(() => draw());
  el._ro.observe(el);
}

function legendHtml(series) {
  return `<div class="legend">${series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>`;
}

// ------------------------------------------------------------------ account bar

function renderAccount() {
  const el = $("#account");
  const me = CONFIG.me;
  if (STATIC) {
    const accounts = CONFIG.accounts || [];
    el.innerHTML = accounts.length > 1
      ? `<div class="seg" role="group" aria-label="Account">${accounts.map((a) => `<button data-account="${a.steamid}" aria-pressed="false">${esc(a.persona || a.steamid)}</button>`).join("")}</div>`
      : "";
    el.querySelectorAll("[data-account]").forEach((b) => (b.onclick = () => { location.hash = b.dataset.account; }));
    return;
  }
  if (me) {
    el.innerHTML = `
      <a class="btn small" href="/u/${me.steamid}" data-link>${me.avatar ? `<img src="${esc(me.avatar)}" alt="">` : ""}${esc(me.persona || "My profile")}</a>
      <button class="btn small" id="logout">Sign out</button>`;
    $("#logout").onclick = async () => { await api("/auth/logout", { method: "POST" }); location.href = "/"; };
  } else {
    el.innerHTML = `<a class="btn small primary" href="/auth/login">Sign in through Steam</a>`;
  }
}

// ------------------------------------------------------------------ home

function renderHome() {
  document.title = "Playtime Ledger";
  const recent = JSON.parse(safeGet("recent") || "[]");
  $("#view").innerHTML = `
    <section class="hero">
      <h1>Where does your game time go?</h1>
      <p>Enter a Steam profile and we'll track its playtime from today on, building a day-by-day timeline of what was played and what it cost.</p>
      <form class="search" id="search">
        <input id="q" placeholder="SteamID64, profile URL or custom URL name" autocomplete="off" aria-label="Steam profile" required>
        <button class="btn primary" type="submit">Track</button>
      </form>
      <p id="search-msg" class="small" style="margin-top:12px"></p>
      ${recent.length ? `<div class="recent muted">Recently viewed: ${recent.map((r) => `<a href="/u/${r.id}" data-link>${esc(r.name || r.id)}</a>`).join(" · ")}</div>` : ""}
    </section>
    <section class="how">
      <div class="card"><h3>1. Snapshots</h3><p>Steam only reports total playtime per game. We save it every ${CONFIG.snapshot_interval_minutes || 60} minutes, and the differences become your daily timeline.</p></div>
      <div class="card"><h3>2. Library value</h3><p>Every owned game is priced from the Steam store in your chosen region, so you see what the library is worth and the cost per hour.</p></div>
      <div class="card"><h3>3. What you really paid</h3><p>Sign in through Steam and import your purchase history to replace estimates with real amounts, sales and refunds included. Private unless you share it.</p></div>
    </section>`;
  $("#search").onsubmit = async (e) => {
    e.preventDefault();
    const msg = $("#search-msg");
    msg.innerHTML = `<span class="spinner"></span> Looking up profile…`;
    try {
      const { steamid } = await api(`/api/resolve?q=${encodeURIComponent($("#q").value)}`);
      navigate(`/u/${steamid}`);
    } catch (err) {
      msg.textContent = err.message;
    }
  };
}

function safeGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function safeSet(k, v) { try { localStorage.setItem(k, v); } catch {} }
function rememberRecent(id, name) {
  const list = JSON.parse(safeGet("recent") || "[]").filter((r) => r.id !== id);
  list.unshift({ id, name });
  safeSet("recent", JSON.stringify(list.slice(0, 6)));
}

// ------------------------------------------------------------------ profile

const SOURCE_LABEL = {
  tracked: "Tracked by this site",
  log: "Steam log (exact sessions)",
  replay: "Steam Replay (monthly total, spread over the month)",
  estimate: "Estimated",
};

const RANGES = [
  { key: "7", label: "7D", days: 7 },
  { key: "30", label: "30D", days: 30 },
  { key: "90", label: "90D", days: 90 },
  { key: "365", label: "1Y", days: 365 },
  { key: "all", label: "All", days: 3650 },
];

async function renderProfile(steamid) {
  hideTip();
  const region = STATIC ? CONFIG.default_region : safeGet("region") || CONFIG.default_region;
  state = { steamid, region, range: safeGet("range") || "30", sort: { key: "playtime_forever", dir: -1 }, filter: "", showTable: false, pollTimer: null };
  $("#view").innerHTML = `<div class="empty"><span class="spinner"></span> Loading profile… the first visit fetches the whole library from Steam.</div>`;
  try {
    const tz = new Date().getTimezoneOffset();
    const [profile, timeline] = await Promise.all([
      api(`/api/profile/${steamid}?region=${region}`),
      api(`/api/profile/${steamid}/timeline?days=3650&tz=${tz}`),
    ]);
    if (state?.steamid !== steamid) return;
    state.profile = profile;
    state.timeline = timeline;
    rememberRecent(steamid, profile.persona);
    drawProfile();
    schedulePricePoll();
  } catch (err) {
    $("#view").innerHTML = `<div class="banner"><strong>Couldn't load this profile.</strong> ${esc(err.message)}</div><p><a href="/" data-link>Back</a></p>`;
  }
}

function schedulePricePoll() {
  clearTimeout(state.pollTimer);
  if (state.profile.stats.prices_pending > 0) {
    state.pollTimer = setTimeout(async () => {
      const id = state.steamid;
      const p = await api(`/api/profile/${id}?region=${state.region}`).catch(() => null);
      if (p && state.steamid === id) { state.profile = p; drawProfile(); schedulePricePoll(); }
    }, 8000);
  }
}

const TOP_N = 4; // coloured games before "Other": stacks past ~4 segments stop being readable

function rangeSlice() {
  const range = RANGES.find((r) => r.key === state.range) || RANGES[1];
  const all = state.timeline.days;
  const days = all.slice(-range.days);
  const prev = range.key === "all" ? [] : all.slice(Math.max(0, all.length - 2 * range.days), all.length - range.days);
  return { range, days, prev: prev.length === range.days ? prev : [] };
}

const gameName = (appid) => state.timeline.games[appid] || state.profile.games.find((g) => String(g.appid) === String(appid))?.name || `App ${appid}`;
const capsuleUrl = (appid) =>
  state.profile.games.find((g) => String(g.appid) === String(appid))?.capsule ||
  `https://cdn.cloudflare.steamstatic.com/steam/apps/${appid}/capsule_184x69.jpg`;

/** Games played in these days, most played first. */
function rankGames(days) {
  const totals = new Map();
  for (const d of days) for (const [a, v] of Object.entries(d.games)) if (v > 0) totals.set(a, (totals.get(a) || 0) + v);
  return [...totals.entries()].sort((x, y) => y[1] - x[1]).map(([appid, minutes]) => ({ appid, minutes }));
}

/** Colour slots for the visible top games. A game keeps its slot when the range changes. */
function assignColors(topIds) {
  const reg = state.colorSlots || (state.colorSlots = new Map());
  const used = new Set();
  const out = new Map();
  for (const id of topIds) {
    const s = reg.get(id);
    if (s !== undefined && !used.has(s)) { out.set(id, s); used.add(s); }
  }
  for (const id of topIds) {
    if (out.has(id)) continue;
    let s = 0;
    while (used.has(s)) s++;
    out.set(id, s); used.add(s); reg.set(id, s);
  }
  return out;
}

function timelineBuckets(days, keyOf) {
  const unit = days.length > 400 ? "month" : days.length > 120 ? "week" : "day";
  const buckets = [];
  const keyOfDate = (date) => {
    const d = new Date(date + "T00:00:00");
    if (unit === "day") return date;
    if (unit === "month") return date.slice(0, 7);
    const monday = new Date(d); monday.setDate(d.getDate() - ((d.getDay() + 6) % 7));
    return `${monday.getFullYear()}-${String(monday.getMonth() + 1).padStart(2, "0")}-${String(monday.getDate()).padStart(2, "0")}`;
  };
  const byKey = new Map();
  for (const day of days) {
    const k = keyOfDate(day.date);
    let b = byKey.get(k);
    if (!b) {
      const d = new Date((unit === "month" ? k + "-01" : k) + "T00:00:00");
      b = {
        label: unit === "month" ? d.toLocaleDateString(undefined, { month: "short", year: "2-digit" }) : d.toLocaleDateString(undefined, { month: "short", day: "numeric" }),
        title: unit === "month" ? d.toLocaleDateString(undefined, { month: "long", year: "numeric" })
          : unit === "week" ? `Week of ${d.toLocaleDateString(undefined, { month: "long", day: "numeric", year: "numeric" })}`
          : d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" }),
        values: {},
        total: 0,
        src: {},
      };
      byKey.set(k, b);
      buckets.push(b);
    }
    for (const [a, v] of Object.entries(day.games)) {
      const key = keyOf(a);
      if (key == null) continue;
      b.values[key] = (b.values[key] || 0) + v;
      b.total += v;
    }
    for (const [s, v] of Object.entries(day.src || {})) b.src[s] = (b.src[s] || 0) + v;
  }
  for (const b of buckets) {
    // Exact-day data (tracked snapshots, Steam log) draws solid. Replay totals are exact per
    // month, so they only count as "spread" when shown per day or week. Estimates are faded.
    const all = Object.values(b.src).reduce((s, v) => s + v, 0);
    const exact = (b.src.tracked || 0) + (b.src.log || 0) + (unit === "month" ? b.src.replay || 0 : 0);
    b.faded = all > 0 && exact / all < 0.5;
    const parts = Object.entries(b.src).filter(([, v]) => v >= 1).sort((x, y) => y[1] - x[1]);
    const label = (s) => (s === "replay" && unit === "month" ? "Steam Replay (exact monthly total)" : SOURCE_LABEL[s] || s);
    b.footer = parts.length && !state.focus ? parts.map(([s, v]) => `${label(s)}: ${duration(v)}`).join("<br>") : "";
  }
  return { buckets, unit };
}

function drawProfile() {
  const p = state.profile;
  const s = p.stats;
  document.title = `${p.persona || p.steamid} · Playtime Ledger`;
  const spent = p.spending?.imported ? p.spending.totals[0] : null;
  const playedMin = s.total_minutes;
  const costPerHour = spent
    ? { v: playedMin ? Math.round(spent.net / (playedMin / 60)) : null, cur: spent.currency, sub: "from your real purchases" }
    : { v: playedMin && s.value_regular ? Math.round(s.value_regular / (playedMin / 60)) : null, cur: s.value_currency, sub: "estimated from store prices" };

  const regionOpts = CONFIG.regions.map((r) => `<option value="${r}" ${r === state.region ? "selected" : ""}>${r.toUpperCase()} prices</option>`).join("");
  const tracking = p.last_snapshot_at
    ? `Tracking since ${fmtDate(p.tracking_since)} · updated ${ago(p.last_snapshot_at)}`
    : `Waiting for the first snapshot…`;

  $("#view").innerHTML = `
    <section class="profile-head">
      ${p.avatar ? `<img src="${esc(p.avatar)}" alt="">` : `<div class="avatar-blank"></div>`}
      <div class="meta">
        <h1>${esc(p.persona || p.steamid)}</h1>
        <div class="secondary small">${tracking}${p.profile_url ? ` · <a href="${esc(p.profile_url)}" target="_blank" rel="noopener">Steam profile ↗</a>` : ""}</div>
      </div>
      ${STATIC ? "" : `<div class="actions">
        <select id="region" aria-label="Store region for prices">${regionOpts}</select>
        <button class="btn small" id="refresh">Refresh</button>
      </div>`}
    </section>
    ${p.last_error ? `<div class="banner"><strong>Steam said:</strong> ${esc(p.last_error)}</div>` : ""}

    <section class="tiles">
      ${tile("Lifetime playtime", `${hours(s.total_minutes, 0)} h`, `${s.games_played} of ${s.games_owned} games played`)}
      ${tile("Last 2 weeks", `${hours(s.minutes_2weeks)} h`, "as reported by Steam")}
      ${tile("Library value", s.value_currency ? money(s.value_regular, s.value_currency) : "—", s.prices_pending ? `pricing ${s.prices_pending} games…` : `${money(s.value_current, s.value_currency)} at today's prices`)}
      ${spent ? tile("Actually spent", money(spent.net, spent.currency), spent.refunded ? `${money(spent.refunded, spent.currency)} refunded` : `${spent.n} transactions`) : ""}
      ${tile("Cost per hour", costPerHour.v == null ? "—" : money(costPerHour.v, costPerHour.cur), costPerHour.sub)}
    </section>

    <section class="card">
      <div class="card-head">
        <h2>Playtime</h2>
        <div class="table-tools">
          <div class="seg" role="group" aria-label="Time range">${RANGES.map((r) => `<button data-range="${r.key}" aria-pressed="${r.key === state.range}">${r.label}</button>`).join("")}</div>
          <button class="btn small" id="tl-table">${state.showTable ? "Chart" : "Table"}</button>
        </div>
      </div>
      <div id="timeline"></div>
    </section>

    <section class="card">
      <div class="card-head"><h2>When you play</h2><span class="secondary small" id="heat-summary"></span></div>
      <div id="heatmap"></div>
    </section>

    ${STATIC ? "" : spendingCard(p)}

    <section class="card">
      <div class="card-head">
        <h2>Library</h2>
        <div class="table-tools"><input id="filter" type="search" placeholder="Search games" value="${esc(state.filter)}" aria-label="Search games"></div>
      </div>
      <div class="table-wrap" id="games"></div>
    </section>`;

  if ($("#region")) $("#region").onchange = (e) => { safeSet("region", e.target.value); renderProfile(p.steamid); };
  if ($("#refresh")) $("#refresh").onclick = async (e) => {
    e.target.disabled = true;
    try { await api(`/api/profile/${p.steamid}/refresh`, { method: "POST" }); renderProfile(p.steamid); }
    catch (err) { e.target.textContent = err.message; }
  };
  document.querySelectorAll("[data-range]").forEach((b) => (b.onclick = () => {
    state.range = b.dataset.range; safeSet("range", state.range);
    document.querySelectorAll("[data-range]").forEach((x) => x.setAttribute("aria-pressed", x === b));
    drawTimeline();
  }));
  $("#tl-table").onclick = (e) => { state.showTable = !state.showTable; e.target.textContent = state.showTable ? "Chart" : "Table"; drawTimeline(); };
  $("#filter").oninput = (e) => { state.filter = e.target.value; drawGames(); };
  bindSpending(p);
  drawTimeline();
  drawHeatmap();
  drawSpendingChart(p);
  drawGames();
}

function tile(label, value, sub) {
  return `<div class="tile"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="sub">${esc(sub || "")}</div></div>`;
}

function rangePhrase(range, days) {
  if (range.key === "all") {
    const first = days.find((d) => d.total > 0)?.date || days[0]?.date;
    return first ? `since ${new Date(first + "T00:00:00").toLocaleDateString(undefined, { month: "long", year: "numeric" })}` : "";
  }
  return range.days === 365 ? "in the last year" : `in the last ${range.days} days`;
}

function drawTimeline() {
  const el = $("#timeline");
  const { range, days, prev } = rangeSlice();
  const ranked = rankGames(days);
  if (state.focus && !ranked.some((g) => g.appid === state.focus)) state.focus = null;
  const focus = state.focus;

  const top = ranked.slice(0, TOP_N).map((g) => g.appid);
  const slots = assignColors(top);
  const colorOf = (id) => (slots.has(id) ? cssVar(SERIES_VARS[slots.get(id)]) : cssVar("--series-other"));
  let series, keyOf;
  if (focus) {
    series = [{ key: focus, name: gameName(focus), color: colorOf(focus) }];
    keyOf = (a) => (a === focus ? a : null);
  } else {
    series = top.map((a) => ({ key: a, name: gameName(a), color: colorOf(a) }));
    if (ranked.length > top.length) series.push({ key: "other", name: `${ranked.length - top.length} other games`, color: cssVar("--series-other") });
    keyOf = (a) => (slots.has(a) ? a : "other");
  }
  const { buckets, unit } = timelineBuckets(days, keyOf);
  const note = historyNote() + logImportUi();

  // Headline numbers for the selected range (and the game in focus, if any).
  const sum = (ds) => ds.reduce((s, d) => s + (focus ? d.games[focus] || 0 : d.total), 0);
  const total = sum(days);
  const prevTotal = prev.length ? sum(prev) : 0;
  const busiest = buckets.reduce((m, b) => (b.total > (m?.total || 0) ? b : m), null);
  const change = prevTotal >= 60 ? Math.round(((total - prevTotal) / prevTotal) * 100) : null;
  const kpis = [
    `<div><b>${duration(total / Math.max(days.length, 1))}</b><span>per day on average</span></div>`,
    busiest ? `<div><b>${duration(busiest.total)}</b><span>busiest ${unit}: ${esc(busiest.label)}</span></div>` : "",
    change != null ? `<div><b>${change > 0 ? "▲" : change < 0 ? "▼" : ""} ${Math.abs(change)}%</b><span>vs the ${range.days} days before</span></div>` : "",
  ].join("");
  const summary = `
    <div class="tl-summary">
      <div class="tl-headline">
        <div class="big">${hours(total)} h</div>
        <div class="secondary">${focus ? `<b>${esc(gameName(focus))}</b> ` : ""}${rangePhrase(range, days)}</div>
      </div>
      <div class="kpis">${kpis}</div>
    </div>
    ${focus ? `<button class="chip" id="clear-focus">Showing one game · show all ✕</button>` : ""}`;

  let main;
  if (!ranked.length) {
    main = `<div class="empty">No playtime in this range yet.<br><span class="small">New sessions appear after the next snapshot (every ${CONFIG.snapshot_interval_minutes} min).</span></div>`;
  } else if (state.showTable) {
    const rows = buckets.filter((b) => b.total > 0).reverse();
    main = `<div class="table-wrap"><table><thead><tr><th>${unit[0].toUpperCase() + unit.slice(1)}</th><th class="num">Total</th>${series.map((s) => `<th class="num">${esc(s.name)}</th>`).join("")}</tr></thead><tbody>
      ${rows.map((b) => `<tr><td>${esc(b.title)}</td><td class="num"><b>${duration(b.total)}</b></td>${series.map((s) => `<td class="num">${b.values[s.key] ? duration(b.values[s.key]) : ""}</td>`).join("")}</tr>`).join("")}
      </tbody></table></div>`;
  } else {
    const anyFaded = buckets.some((b) => b.faded);
    const legend = legendHtml(series).replace(/<\/div>$/, anyFaded ? `<span class="faded"><i style="background:var(--text-muted)"></i>Faded = ${unit === "month" ? "estimated" : "monthly total spread over the month, or estimated"}</span></div>` : "</div>");
    main = `<div class="chart" id="tl-chart"></div>${legend}`;
  }

  el.innerHTML = `
    ${summary}
    <div class="tl-grid">
      <div class="tl-main">${main}</div>
      ${ranked.length ? `<aside class="tl-top">${topGamesHtml(ranked, slots, total)}</aside>` : ""}
    </div>
    ${note}`;

  if ($("#tl-chart")) {
    renderBars($("#tl-chart"), {
      buckets, series,
      fmt: duration,
      axisFmt: (v) => (v === 0 ? "0" : `${hours(v, 1)}h`),
      unit: 60,
    });
  }
  el.querySelectorAll("[data-focus]").forEach((b) => (b.onclick = () => {
    state.focus = state.focus === b.dataset.focus ? null : b.dataset.focus;
    drawTimeline();
  }));
  const clear = $("#clear-focus");
  if (clear) clear.onclick = () => { state.focus = null; drawTimeline(); };
  const more = $("#top-more");
  if (more) more.onclick = () => { state.showAllTop = !state.showAllTop; drawTimeline(); };
  bindLogImport();
}

function topGamesHtml(ranked, slots, total) {
  const rangeTotal = ranked.reduce((s, g) => s + g.minutes, 0);
  const max = ranked[0].minutes;
  const shown = state.showAllTop ? ranked : ranked.slice(0, 8);
  const rows = shown.map((g, i) => {
    const color = slots.has(g.appid) ? `var(${SERIES_VARS[slots.get(g.appid)]})` : "var(--series-other)";
    const pct = Math.round((g.minutes / rangeTotal) * 100);
    return `<li>
      <button class="top-row" data-focus="${esc(g.appid)}" aria-pressed="${state.focus === g.appid}" title="Show only this game in the chart">
        <span class="rank">${i + 1}</span>
        <img class="cap" src="${capsuleUrl(g.appid)}" alt="" loading="lazy" onerror="this.classList.add('missing')">
        <span class="top-info">
          <span class="top-name">${esc(gameName(g.appid))}</span>
          <span class="share"><i style="width:${Math.max(2, (g.minutes / max) * 100)}%;background:${color}"></i></span>
        </span>
        <span class="top-val"><b>${duration(g.minutes)}</b><small>${pct}%</small></span>
      </button>
    </li>`;
  }).join("");
  return `<h3>Top games <span class="muted small">· click to focus</span></h3><ol class="toplist">${rows}</ol>
    ${ranked.length > 8 ? `<button class="btn small" id="top-more">${state.showAllTop ? "Show fewer" : `Show all ${ranked.length}`}</button>` : ""}`;
}

/** Calendar heatmap of the last year, using only days with exact data. */
function drawHeatmap() {
  const el = $("#heatmap");
  const tl = state.timeline;
  const byDate = new Map(tl.days.map((d) => [d.date, d]));
  const startTracked = new Date(tl.tracking_since * 1000);
  const trackedFrom = `${startTracked.getFullYear()}-${String(startTracked.getMonth() + 1).padStart(2, "0")}-${String(startTracked.getDate()).padStart(2, "0")}`;
  const log = tl.history?.log_range;
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const isCovered = (ds) => ds >= trackedFrom || (log && ds >= log[0] && ds <= log[1]);
  const exactMin = (ds) => { const d = byDate.get(ds); return d ? (d.src?.tracked || 0) + (d.src?.log || 0) : 0; };

  const draw = () => {
    const width = el.clientWidth || 700;
    const cell = 12, gap = 3, left = 30, topPad = 18;
    const weeks = Math.max(8, Math.min(53, Math.floor((width - left) / (cell + gap))));
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const start = new Date(today);
    start.setDate(today.getDate() - ((today.getDay() + 6) % 7) - (weeks - 1) * 7); // a Monday
    const levels = [0, 60, 120, 240];
    const level = (m) => (m <= 0 ? 0 : m < 60 ? 1 : m < 120 ? 2 : m < 240 ? 3 : 4);

    let covered = 0, played = 0, streak = 0, best = 0;
    const weekday = Array.from({ length: 7 }, () => ({ min: 0, n: 0 }));
    let svg = `<svg viewBox="0 0 ${left + weeks * (cell + gap)} ${topPad + 7 * (cell + gap)}" width="${left + weeks * (cell + gap)}" height="${topPad + 7 * (cell + gap)}" role="img" aria-label="Calendar of daily playtime">`;
    ["Mon", "", "Wed", "", "Fri", "", ""].forEach((t, i) => {
      if (t) svg += `<text class="hm-label" x="0" y="${topPad + i * (cell + gap) + cell - 2}">${t}</text>`;
    });
    let lastMonth = -1;
    const cells = [];
    for (let w = 0; w < weeks; w++) {
      for (let r = 0; r < 7; r++) {
        const d = new Date(start); d.setDate(start.getDate() + w * 7 + r);
        if (d > today) continue;
        if (r === 0 && d.getMonth() !== lastMonth) {
          lastMonth = d.getMonth();
          if (w < weeks - 2) svg += `<text class="hm-label" x="${left + w * (cell + gap)}" y="11">${d.toLocaleDateString(undefined, { month: "short" })}</text>`;
        }
        const ds = iso(d);
        const x = left + w * (cell + gap), y = topPad + r * (cell + gap);
        if (!isCovered(ds)) {
          svg += `<rect class="hm-none" x="${x + 0.5}" y="${y + 0.5}" width="${cell - 1}" height="${cell - 1}" rx="2.5"/>`;
          continue;
        }
        const m = exactMin(ds);
        covered++; if (m > 0) { played++; streak++; best = Math.max(best, streak); } else streak = 0;
        weekday[r].min += m; weekday[r].n++;
        cells.push({ ds, m, i: cells.length });
        svg += `<rect class="hm-cell" data-i="${cells.length - 1}" x="${x}" y="${y}" width="${cell}" height="${cell}" rx="3" fill="var(--heat-${level(m)})"/>`;
      }
    }
    svg += `</svg>`;
    const steps = ["didn't play", "under 1 h", "1–2 h", "2–4 h", "4 h or more"];
    const legend = `<div class="hm-legend"><span>Less</span>${steps.map((t, l) => `<i title="${t}" style="background:var(--heat-${l})"></i>`).join("")}<span>More</span><span class="hm-gap"></span><i class="none"></i><span>no exact data for that day</span></div>`;
    el.innerHTML = covered
      ? `<div class="heatmap">${svg}</div>${legend}`
      : `<div class="empty">Fills in as tracking runs, or import your Steam log to see past days.</div>`;

    const names = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"];
    const fav = weekday.map((w, i) => ({ i, avg: w.n ? w.min / w.n : 0 })).sort((a, b) => b.avg - a.avg)[0];
    $("#heat-summary").textContent = covered
      ? `Played ${played} of ${covered} days · longest streak ${best} days${fav.avg > 0 ? ` · most on ${names[fav.i]} (${duration(fav.avg)} avg)` : ""}`
      : "";

    el.querySelectorAll(".hm-cell").forEach((r) => {
      const c = cells[+r.dataset.i];
      const day = byDate.get(c.ds);
      const topGame = day && Object.entries(day.games).sort((a, b) => b[1] - a[1])[0];
      const title = new Date(c.ds + "T00:00:00").toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric", year: "numeric" });
      const html = `<div class="t-title">${esc(title)}</div>${c.m > 0
        ? `<div class="t-row"><span>Played</span><span class="t-val"><b>${duration(c.m)}</b></span></div>${topGame ? `<div class="t-row"><span>Mostly</span><span class="t-val">${esc(gameName(topGame[0]))}</span></div>` : ""}`
        : `<div class="muted">Didn't play</div>`}`;
      r.addEventListener("mouseenter", (ev) => showTip(html, ev));
      r.addEventListener("mousemove", moveTip);
      r.addEventListener("mouseleave", hideTip);
    });
  };
  draw();
  if (el._ro) el._ro.disconnect();
  let lastW = el.clientWidth;
  el._ro = new ResizeObserver(() => { if (Math.abs(el.clientWidth - lastW) > 10) { lastW = el.clientWidth; draw(); } });
  el._ro.observe(el);
}

function historyNote() {
  const h = state.timeline.history || {};
  const started = fmtDate(state.timeline.tracking_since);
  const fmtD = (iso) => new Date(iso + "T00:00:00").toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  const items = [];
  if (h.replay_years?.length) {
    const ys = h.replay_years;
    items.push(`<li><b>Steam Replay ${ys[0]}${ys.length > 1 ? `–${ys[ys.length - 1]}` : ""}</b>: exact playtime per game per month, spread evenly over each month.</li>`);
  }
  if (h.log_range) items.push(`<li><b>Steam log ${fmtD(h.log_range[0])} – ${fmtD(h.log_range[1])}</b>: exact sessions from the owner's PC.</li>`);
  if (h.estimate_range) items.push(`<li><b>Estimated ${fmtD(h.estimate_range[0])} – ${fmtD(h.estimate_range[1])}</b>: ${duration(h.estimated_minutes)} not covered by any source, spread between each game's first and last play.</li>`);
  items.push(`<li><b>Tracked since ${started}</b>: hourly snapshots. Steam updates totals when a session ends, so a session counts on the day it finished.</li>`);
  if (h.undated_minutes >= 60) items.push(`<li><b>${duration(h.undated_minutes)} undated</b>: counted in the totals but can't be placed on the timeline (mostly play before 2022, which Steam Replay doesn't cover).</li>`);
  if (!h.replay_years?.length) items.push(`<li class="muted">No Steam Replay data for this profile, so older playtime can't be placed. Replay appears once the owner's profile and game details are public.</li>`);
  return `<details class="about"><summary>Where this data comes from</summary><ul class="sources">${items.join("")}</ul></details>`;
}

function logImportUi() {
  if (!state.profile.is_owner) return "";
  const h = state.timeline.history || {};
  return `<details style="margin-top:12px">
    <summary class="small"><b>${h.log_range ? "Add newer Steam logs" : "Add exact session history from your PC"}</b></summary>
    <ol class="steps">
      <li>Open your Steam folder's <code>logs</code> folder, usually <code>C:\\Program Files (x86)\\Steam\\logs</code>.</li>
      <li>Select <code>gameprocess_log.txt</code> and <code>connection_log.txt</code> (plus any <code>.previous.txt</code> versions). The connection log tells which Steam account was signed in, so sessions from other accounts on the same PC are left out.</li>
      <li>Steam rotates these logs, so they only reach back a few months. Upload again later to add more; duplicates are ignored.</li>
    </ol>
    <div class="import-row">
      <input type="file" id="log-files" multiple accept=".txt,text/plain">
      <button class="btn primary small" id="log-upload">Import logs</button>
      ${h.log_range ? `<button class="btn small danger" id="log-delete">Delete imported logs</button>` : ""}
      <span id="log-msg" class="small secondary"></span>
    </div>
  </details>`;
}

function bindLogImport() {
  const up = $("#log-upload");
  if (!up) return;
  const id = state.steamid;
  up.onclick = async () => {
    const files = $("#log-files").files;
    const msg = $("#log-msg");
    if (!files.length) { msg.textContent = "Choose the log files first."; return; }
    const fd = new FormData();
    for (const f of files) fd.append("files", f);
    fd.append("tz", new Date().getTimezoneOffset());
    msg.innerHTML = `<span class="spinner"></span> Importing…`;
    try {
      const r = await api("/api/me/playlog", { method: "POST", body: fd });
      msg.textContent = `Added ${r.sessions_added} sessions` +
        (r.account_check ? ` (${r.sessions_other_account} from other accounts left out).` : ". Add connection_log.txt to leave out other accounts' sessions.");
      setTimeout(() => renderProfile(id), 1200);
    } catch (err) { msg.textContent = err.message; }
  };
  const del = $("#log-delete");
  if (del) del.onclick = async () => {
    if (!confirm("Delete your imported Steam log sessions from this site?")) return;
    await api("/api/me/playlog", { method: "DELETE" });
    renderProfile(id);
  };
}

// ------------------------------------------------------------------ spending

function spendingCard(p) {
  if (!p.can_see_spending) {
    return `<section class="card"><div class="card-head"><h2>Spending</h2></div>
      <p class="secondary" style="margin:0">Real purchase amounts are private for this profile. The library value above is estimated from current Steam store prices.</p></section>`;
  }
  const sp = p.spending;
  const owner = p.is_owner;
  const importUi = owner ? `
    <details ${sp.imported ? "" : "open"}>
      <summary><b>${sp.imported ? "Re-import purchase history" : "Import your purchase history"}</b></summary>
      <ol class="steps">
        <li>Open <a href="https://store.steampowered.com/account/history/" target="_blank" rel="noopener">store.steampowered.com/account/history</a> while signed in. Set Steam's language to English for the most reliable import.</li>
        <li>Scroll down and click <i>Load More Transactions</i> until it disappears.</li>
        <li>Press <kbd>Ctrl</kbd>+<kbd>S</kbd> (<kbd>⌘</kbd>+<kbd>S</kbd> on Mac) and save the page as HTML.</li>
        <li>Upload that file here. It replaces any previous import.</li>
      </ol>
      <div class="import-row">
        <input type="file" id="hist-file" accept=".html,.htm,text/html">
        <button class="btn primary small" id="hist-upload">Import</button>
        ${sp.imported ? `<button class="btn small danger" id="hist-delete">Delete imported data</button>` : ""}
        <span id="hist-msg" class="small secondary"></span>
      </div>
    </details>
    <p><label class="switch"><input type="checkbox" id="share" ${p.share_spending ? "checked" : ""}> Show my real spending to everyone who views this profile</label></p>` : "";

  if (!sp.imported) {
    return `<section class="card"><div class="card-head"><h2>Spending</h2></div>
      ${owner ? `<p class="secondary" style="margin-top:0">Steam has no API for what you paid. Import your purchase history to see real totals, refunds and cost per hour.</p>${importUi}` : `<p class="secondary">No purchase history imported yet.</p>`}</section>`;
  }
  const main = sp.totals[0];
  const foreign = (sp.originals || []).filter((o) => o.currency !== main.currency);
  return `<section class="card">
    <div class="card-head"><h2>Spending over time</h2><span class="secondary small">${money(main.gross, main.currency)} spent · ${money(main.refunded, main.currency)} refunded · ${main.n} transactions</span></div>
    ${foreign.length ? `<p class="note">Purchases in ${foreign.map((o) => `${esc(o.currency)} (${money(o.net, o.currency)} over ${o.n})`).join(", ")} are converted to ${esc(main.currency)} at the exchange rate of each purchase date. Change the currency with the prices menu at the top.${sp.unconverted ? ` ${sp.unconverted} purchases in an unknown currency are left out.` : ""}</p>` : ""}
    <div class="chart" id="spend-chart"></div>
    ${sp.unmatched.length ? `<details style="margin-top:12px"><summary class="secondary small">${sp.unmatched.length} ${sp.unmatched.length === 1 ? "purchase that isn't" : "purchases that aren't"} a game in this library (bundles, DLC, items)</summary>
      <div class="table-wrap"><table><tbody>${sp.unmatched.map((u) => `<tr><td>${esc(u.name)}</td><td class="num">${money(u.amount, main.currency)}</td></tr>`).join("")}</tbody></table></div></details>` : ""}
    ${importUi ? `<hr style="border:0;border-top:1px solid var(--border);margin:16px 0">${importUi}` : ""}
  </section>`;
}

function drawSpendingChart(p) {
  const el = $("#spend-chart");
  if (!el || !p.spending?.monthly?.length) return;
  const months = p.spending.monthly;
  // fill gaps so empty months show as empty
  const first = months[0].month, last = months[months.length - 1].month;
  const map = Object.fromEntries(months.map((m) => [m.month, m.net]));
  const buckets = [];
  let [y, mo] = first.split("-").map(Number);
  const [ly, lm] = last.split("-").map(Number);
  while (y < ly || (y === ly && mo <= lm)) {
    const key = `${y}-${String(mo).padStart(2, "0")}`;
    const d = new Date(y, mo - 1, 1);
    buckets.push({
      label: d.toLocaleDateString(undefined, { month: "short", year: "2-digit" }),
      title: d.toLocaleDateString(undefined, { month: "long", year: "numeric" }),
      values: { spent: Math.max(0, map[key] || 0) },
    });
    if (++mo > 12) { mo = 1; y++; }
  }
  const cur = p.spending.totals[0].currency;
  renderBars(el, {
    buckets,
    series: [{ key: "spent", name: "Spent (after refunds)", color: cssVar("--series-1") }],
    fmt: (v) => money(v, cur),
    axisFmt: (v) => money(Math.round(v), cur).replace(/[.,]00(?!\d)/, ""),
    height: 200,
    unit: 100,
  });
}

function bindSpending(p) {
  const up = $("#hist-upload");
  if (up) up.onclick = async () => {
    const f = $("#hist-file").files[0];
    const msg = $("#hist-msg");
    if (!f) { msg.textContent = "Choose the saved HTML file first."; return; }
    const fd = new FormData();
    fd.append("file", f);
    fd.append("region", state.region);
    msg.innerHTML = `<span class="spinner"></span> Importing…`;
    try {
      const r = await api("/api/me/purchases", { method: "POST", body: fd });
      msg.textContent = `Imported ${r.transactions} transactions (${r.items_matched} items matched to games).`;
      setTimeout(() => renderProfile(p.steamid), 900);
    } catch (err) { msg.textContent = err.message; }
  };
  const del = $("#hist-delete");
  if (del) del.onclick = async () => {
    if (!confirm("Delete your imported purchase history from this site?")) return;
    await api("/api/me/purchases", { method: "DELETE" });
    renderProfile(p.steamid);
  };
  const share = $("#share");
  if (share) share.onchange = async () => {
    await api("/api/me/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ share_spending: share.checked }) });
  };
}

// ------------------------------------------------------------------ games table

const COLUMNS = [
  { key: "name", label: "Game", sortable: true },
  { key: "playtime_forever", label: "Total", num: true },
  { key: "playtime_2weeks", label: "2 weeks", num: true },
  { key: "rtime_last_played", label: "Last played", num: true },
  { key: "price", label: "Store price", num: true },
  { key: "paid", label: "You paid", num: true, spendingOnly: true },
  { key: "cph", label: "Cost / hour", num: true },
];

function gameValue(g, key) {
  if (key === "price") return g.is_free ? 0 : g.initial ?? -1;
  if (key === "cph") return costPerHour(g) ?? Infinity;
  if (key === "name") return (g.name || "").toLowerCase();
  return g[key] ?? -1;
}
function costPerHour(g) {
  const cost = g.paid ?? (g.is_free ? 0 : g.initial);
  if (cost == null || !g.playtime_forever) return null;
  return Math.round(cost / (g.playtime_forever / 60));
}

function drawGames() {
  const p = state.profile;
  const showPaid = p.can_see_spending && p.spending?.imported;
  const paidCur = showPaid ? p.spending.totals[0].currency : null;
  const cols = COLUMNS.filter((c) => !c.spendingOnly || showPaid);
  const q = state.filter.trim().toLowerCase();
  const { key, dir } = state.sort;
  const games = p.games
    .filter((g) => !q || (g.name || "").toLowerCase().includes(q))
    .sort((a, b) => {
      const va = gameValue(a, key), vb = gameValue(b, key);
      return (va < vb ? -1 : va > vb ? 1 : 0) * dir;
    });

  const priceCell = (g) => {
    if (!g.price_known) return `<span class="muted">…</span>`;
    if (!g.available) return `<span class="pill">not sold</span>`;
    if (g.is_free) return `<span class="pill">free</span>`;
    return g.final < g.initial
      ? `${money(g.initial, g.currency)} <span class="muted small">(${money(g.final, g.currency)} now)</span>`
      : money(g.initial, g.currency);
  };
  const cphCell = (g) => {
    const v = costPerHour(g);
    if (v == null) return `<span class="muted">—</span>`;
    return money(v, g.paid != null ? paidCur : g.currency);
  };

  const shown = games.slice(0, 500);
  $("#games").innerHTML = `<table>
    <thead><tr>${cols.map((c) => `<th class="sortable ${c.num ? "num" : ""}" data-sort="${c.key}" aria-sort="${c.key === key ? (dir > 0 ? "ascending" : "descending") : "none"}">${c.label}${c.key === key ? (dir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr></thead>
    <tbody>${shown.map((g) => `<tr>
      <td class="game">${iconUrl(g) ? `<img src="${iconUrl(g)}" alt="" loading="lazy">` : ""}<a href="https://store.steampowered.com/app/${g.appid}" target="_blank" rel="noopener">${esc(g.name)}</a></td>
      <td class="num">${g.playtime_forever ? duration(g.playtime_forever) : '<span class="muted">never</span>'}</td>
      <td class="num">${g.playtime_2weeks ? duration(g.playtime_2weeks) : ""}</td>
      <td class="num">${g.rtime_last_played ? fmtDate(g.rtime_last_played) : ""}</td>
      <td class="num">${priceCell(g)}</td>
      ${showPaid ? `<td class="num">${g.paid != null ? money(g.paid, paidCur) : '<span class="muted">—</span>'}</td>` : ""}
      <td class="num">${cphCell(g)}</td>
    </tr>`).join("")}</tbody></table>
    ${games.length > shown.length ? `<p class="muted small">Showing 500 of ${games.length}. Use the filter to find more.</p>` : ""}
    ${!games.length ? `<div class="empty">No games${q ? " match that filter" : ""}.</div>` : ""}`;
  document.querySelectorAll("#games th[data-sort]").forEach((th) => (th.onclick = () => {
    const k = th.dataset.sort;
    state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : k === "name" ? 1 : -1 };
    drawGames();
  }));
}

// ------------------------------------------------------------------ routing

function navigate(path) {
  history.pushState({}, "", path);
  route();
}
function route() {
  if (state) clearTimeout(state.pollTimer);
  state = null;
  hideTip();
  if (STATIC) {
    const ids = (CONFIG.accounts || []).map((a) => a.steamid);
    const id = ids.includes(location.hash.slice(1)) ? location.hash.slice(1) : ids[0];
    document.querySelectorAll("[data-account]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.account === id));
    if (id) renderProfile(id);
    else $("#view").innerHTML = `<div class="empty">No profiles published yet.</div>`;
    return;
  }
  const m = location.pathname.match(/^\/u\/(\d{17})/);
  if (m) renderProfile(m[1]);
  else renderHome();
}
window.addEventListener("hashchange", () => { if (STATIC) route(); });
document.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-link]");
  if (a && !e.metaKey && !e.ctrlKey && !e.shiftKey) { e.preventDefault(); navigate(a.getAttribute("href")); }
});
window.addEventListener("popstate", route);
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (state?.profile) drawProfile(); });

(async () => {
  try { CONFIG = await api("/api/config"); } catch {}
  renderAccount();
  if (new URLSearchParams(location.search).get("login") === "failed") alert("Steam sign-in failed. Please try again.");
  route();
})();
