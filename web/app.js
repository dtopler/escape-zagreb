/* Escape Room Zagreb — availability explorer
 *
 * Data model (from /api/state):
 *   { generated_at, window:{start,end,days}, days:[iso...],
 *     providers:[{id,name,rooms,supported,reason?}],
 *     rooms:[{id,provider_id,provider,room,url,schedule_url,
 *             slots:{iso:[{time,end?,note?}]}, total_slots, unsupported, error}] }
 *
 * Views: month heatmap, week hour-grid. Both are filtered by provider chips and
 * by an optional single-room focus. Details panel drills: day -> hour -> rooms.
 */

/* ------------------------------------------------------------- data source */
/* Local dev: the Python server answers /api/state, /api/progress, /api/refresh.
 * Static deployment: a generated config.js points window.ESCAPE_DATA_URL at a
 * published data.json (the `data` branch of this repo), and the live-only
 * controls degrade to "reload the snapshot" instead of pretending to scrape. */
const DATA_URL = (typeof window !== 'undefined' && window.ESCAPE_DATA_URL) || '/api/state';
const STATIC_SITE = DATA_URL !== '/api/state';

/* Bucket the cache-buster to 5 minutes: reuses the CDN copy, never serves a
 * snapshot older than the CDN's own max-age window. */
function dataUrl() {
  if (!STATIC_SITE) return DATA_URL;
  const sep = DATA_URL.includes('?') ? '&' : '?';
  return `${DATA_URL}${sep}v=${Math.floor(Date.now() / 300000)}`;
}

const S = {
  data: null,
  view: 'month',
  cursor: null,        // Date: first of month (month view) or Monday (week view)
  selectedDay: null,   // ISO string
  selectedHour: null,  // integer hour
  roomId: null,
  hidden: new Set(),   // provider ids switched off
  players: null,       // "I have N players" -> rooms whose range covers N
  tags: new Set(),     // genres to require (any of)
  index: new Map(),    // iso -> [{room, time, end}] for current filters
  maxPerDay: 1,
  maxPerHour: 1,
  pollTimer: null,
};

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
  'August', 'September', 'October', 'November', 'December'];
const DOW_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const DOW_LONG = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

/* ------------------------------------------------------------------ dates */
const pad = (n) => String(n).padStart(2, '0');
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parseYmd = (s) => {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(y, m - 1, d);
};
const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const startOfMonth = (d) => new Date(d.getFullYear(), d.getMonth(), 1);
const mondayOf = (d) => {
  const dow = (d.getDay() + 6) % 7; // 0 = Monday
  return addDays(d, -dow);
};
const todayISO = () => ymd(new Date());
const fmtDay = (iso) => {
  const d = parseYmd(iso);
  return `${DOW_SHORT[(d.getDay() + 6) % 7]} ${d.getDate()} ${MONTHS[d.getMonth()].slice(0, 3)}`;
};
const fmtLong = (iso) => {
  const d = parseYmd(iso);
  return `${DOW_LONG[(d.getDay() + 6) % 7]}, ${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
};
const hourLabel = (h) => `${pad(h)}:00`;

/* ------------------------------------------------------------------ utils */
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const normTime = (t) => {
  const m = /^(\d{1,2}):(\d{2})/.exec(String(t ?? ''));
  return m ? `${pad(+m[1])}:${m[2]}` : null;
};
const slotOf = (raw) => (typeof raw === 'string'
  ? { time: raw }
  : { time: raw.time, end: raw.end, note: raw.note });
const hourOf = (time) => parseInt(time.slice(0, 2), 10);
const inWindow = (iso) => !S.data || (iso >= S.data.window.start && iso <= S.data.window.end);

function toast(msg, ms = 4200) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, ms);
}

/* --------------------------------------------------------------- indexing */
/** Does the room satisfy the player-count and genre filters? */
function passesFilters(room) {
  if (S.players) {
    const { players_min: lo, players_max: hi } = room;
    if (!lo || !hi || !(lo <= S.players && S.players <= hi)) return false;
  }
  if (S.tags.size) {
    const tags = room.tags || [];
    if (!tags.some((t) => S.tags.has(t))) return false;
  }
  return true;
}

function visibleRooms() {
  if (!S.data) return [];
  return S.data.rooms.filter((r) => !r.unsupported
    && !S.hidden.has(r.provider_id)
    && (!S.roomId || r.id === S.roomId)
    && passesFilters(r));
}

function rebuildIndex() {
  S.index = new Map();
  S.maxPerDay = 1;
  S.maxPerHour = 1;
  const perDayRoom = new Map();
  const perHourRoom = new Map();
  for (const room of visibleRooms()) {
    for (const [iso, rawList] of Object.entries(room.slots || {})) {
      const list = S.index.get(iso) || [];
      const seenTimes = new Set();
      for (const raw of rawList) {
        const slot = slotOf(raw);
        const time = normTime(slot.time);
        if (!time) continue;
        list.push({ room, time, end: slot.end ? normTime(slot.end) : null, note: slot.note || '' });
        seenTimes.add(time);
      }
      if (list.length) S.index.set(iso, list);
      S.maxPerDay = Math.max(S.maxPerDay, list.length);

      const roomKey = `${iso}|${room.id}`;
      perDayRoom.set(roomKey, true);
      for (const time of seenTimes) {
        const hk = `${iso}|${hourOf(time)}`;
        if (!perHourRoom.has(hk)) perHourRoom.set(hk, new Set());
        perHourRoom.get(hk).add(room.id);
      }
    }
  }
  for (const [iso, list] of S.index) {
    list.sort((a, b) => a.time.localeCompare(b.time) || a.room.room.localeCompare(b.room.room));
    S.index.set(iso, list);
  }
  for (const set of perHourRoom.values()) S.maxPerHour = Math.max(S.maxPerHour, set.size);
  S.perHourRooms = perHourRoom;
}

const entriesFor = (iso) => S.index.get(iso) || [];
const roomsAtHour = (iso, hour) => {
  const set = S.perHourRooms.get(`${iso}|${hour}`);
  return set ? set.size : 0;
};
const hoursFor = (iso) => {
  const out = new Map();
  for (const e of entriesFor(iso)) {
    const h = hourOf(e.time);
    if (!out.has(h)) out.set(h, []);
    out.get(h).push(e);
  }
  return out;
};
const isOpen = (iso) => {
  if (!S.data) return false;
  if (!inWindow(iso)) return false;
  return entriesFor(iso).length > 0;
};
function nextOpenDay(fromIso) {
  if (!S.data) return null;
  const start = parseYmd(fromIso);
  for (let i = 0; i <= S.data.window.days; i += 1) {
    const iso = ymd(addDays(start, i));
    if (isOpen(iso)) return iso;
  }
  return null;
}

/* -------------------------------------------------------------- rendering */
function heatAlpha(ratio) {
  return 0.05 + Math.min(1, Math.max(0, ratio)) * 0.5;
}

function renderRangeLabel() {
  const el = document.getElementById('rangeLabel');
  if (!S.data) { el.textContent = '—'; return; }
  if (S.view === 'rooms') {
    el.textContent = `${S.data.rooms.length} rooms · ${S.data.providers.length} providers`;
  } else if (S.view === 'month') {
    el.textContent = `${MONTHS[S.cursor.getMonth()]} ${S.cursor.getFullYear()}`;
  } else {
    const start = mondayOf(S.cursor);
    const end = addDays(start, 6);
    const sameMonth = start.getMonth() === end.getMonth();
    el.textContent = `${start.getDate()} ${MONTHS[start.getMonth()].slice(0, 3)}`
      + ` – ${end.getDate()} ${MONTHS[end.getMonth()].slice(0, 3)} ${end.getFullYear()}`
      + (sameMonth ? '' : '');
  }
}

function renderChips() {
  const wrap = document.getElementById('providerChips');
  const providers = S.data ? S.data.providers : [];
  const supported = providers.filter((p) => p.supported);
  wrap.innerHTML = [
    `<button class="chip ${S.hidden.size === 0 ? '' : 'off'}" data-provider="__all">`
    + `<span class="dot"></span>All providers</button>`,
    ...providers.map((p) => {
      const off = S.hidden.has(p.id) || !p.supported;
      const blocked = p.supported && p.rooms_failed === p.rooms;
      const title = !p.supported
        ? `Not readable: ${p.reason || 'no public schedule endpoint'}`
        : (blocked ? `Could not read this scan: ${p.error || 'provider blocked us'}`
          : (p.slots ? `${p.rooms} room${p.rooms === 1 ? '' : 's'}, ${p.slots} open slots — click to ${S.hidden.has(p.id) ? 'show' : 'hide'}`
            : `${p.rooms} rooms, no online slots right now (booking closed or phone-only)`));
      const dotStyle = (!p.supported || blocked) ? ' style="background:var(--red)"'
        : (p.slots ? '' : ' style="background:var(--gold)"');
      return `<button class="chip ${off ? 'off' : ''}" data-provider="${esc(p.id)}" `
        + `title="${esc(title)}"><span class="dot"${dotStyle}></span>`
        + `${esc(p.name)} <span class="n">${p.rooms}</span></button>`;
    }),
  ].join('');
}

/** Player-count select, genre chips and the "what got hidden" hint. */
function renderFilters() {
  if (!S.data) return;
  const select = document.getElementById('playersFilter');
  select.value = S.players ? String(S.players) : '';

  const counts = new Map();
  for (const room of S.data.rooms) {
    for (const tag of room.tags || []) counts.set(tag, (counts.get(tag) || 0) + 1);
  }
  const order = [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([tag, n]) => `<button class="chip ${S.tags.has(tag) ? 'on' : ''}" data-tag="${esc(tag)}"`
      + ` title="${n} room${n === 1 ? '' : 's'}">${esc(tag)} <span class="n">${n}</span></button>`)
    .join('');
  document.getElementById('genreFilter').innerHTML = order;

  const active = Boolean(S.players) || S.tags.size > 0;
  document.getElementById('clearFilters').hidden = !active;

  const bits = [];
  if (S.players) {
    const unknown = S.data.rooms.filter((r) => !r.unsupported && !r.players_min).length;
    if (unknown) bits.push(`${unknown} room${unknown === 1 ? '' : 's'} hidden — player count not published`);
  }
  const shown = visibleRooms().length;
  if (active) bits.push(`${shown} room${shown === 1 ? '' : 's'} match`);
  document.getElementById('filterNote').textContent = bits.join(' · ');
}

function renderFocus() {
  const el = document.getElementById('roomFocus');
  if (!S.roomId || !S.data) { el.hidden = true; el.innerHTML = ''; return; }
  const room = S.data.rooms.find((r) => r.id === S.roomId);
  if (!room) { el.hidden = true; return; }
  const days = Object.keys(room.slots || {}).length;
  const trouble = room.status === 'error'
    ? `· <span style="color:var(--red)">scrape failed</span>`
    : room.status === 'stale'
      ? `· <span style="color:var(--gold)">${room.stale_days} day(s) from cache</span>`
      : (room.status === 'partial'
        ? `· <span style="color:var(--gold)">${room.failed_days} day(s) not read</span>` : '');
  el.hidden = false;
  el.innerHTML = `
    <div>
      <div class="fname">${esc(room.room)}</div>
      <div class="fprovider">${esc(room.provider)} · ${room.total_slots} slots on ${days} days ${trouble}</div>
    </div>
    <div style="display:flex;gap:14px;align-items:center">
      <a href="${esc(room.schedule_url || room.url)}" target="_blank" rel="noopener">open booking page ↗</a>
      <button class="clear" id="clearFocus">Show all rooms</button>
    </div>`;
  document.getElementById('clearFocus').onclick = () => setRoom(null);
}

function renderCalendar() {
  const wrap = document.getElementById('calendar');
  if (!S.data) {
    wrap.innerHTML = '<div class="skeleton">Loading availability…</div>';
    return;
  }
  wrap.classList.toggle('rooms-mode', S.view === 'rooms');
  document.body.classList.toggle('rooms-view', S.view === 'rooms');
  if (S.view === 'rooms') wrap.innerHTML = roomsViewHtml();
  else wrap.innerHTML = S.view === 'month' ? monthHtml() : weekHtml();
  wireCalendar();
  syncUrl();          // every interactive path re-renders the calendar
}

/* -------------------------------------------------------------- coverage */
function coverageBanner(firstIso, lastIso, label) {
  if (!S.data) return '';
  const w = S.data.window;
  if (lastIso < w.start) {
    return `<div class="banner">${esc(label)} is before the scanned window
      (data starts ${esc(w.start)}). Use ‹ to go back or Refresh to rescan.</div>`;
  }
  if (firstIso > w.end) {
    return `<div class="banner"><span>${esc(label)} is not scanned yet.</span>
      <button class="chip" data-scanend="${lastIso}">Scan ${esc(label)}</button></div>`;
  }
  if (lastIso > w.end) {
    return `<div class="banner"><span>Scanned up to ${esc(w.end)}.</span>
      <button class="chip" data-scanend="${lastIso}">Extend scan to ${esc(label)}</button></div>`;
  }
  return '';
}

async function scanTo(endIso) {
  if (!S.data) return;
  if (STATIC_SITE) {
    toast('This is a published snapshot — it refreshes every 15 minutes.');
    return;
  }
  const start = S.data.window.start;
  const ms = parseYmd(endIso) - parseYmd(start);
  const days = Math.max(1, Math.ceil(ms / 86400000));
  if (days > 120) {
    toast('That is more than ~4 months ahead — pick a nearer month.');
    return;
  }
  const res = await fetch('/api/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ days, start, force: false }),
  });
  const out = await res.json();
  if (out.started) {
    S._wasScanning = true;
    toast(`Scanning ${days} days from ${start}…`);
    pollProgress();
  } else {
    toast(out.error || 'scan already running');
  }
}

function monthHtml() {
  const first = startOfMonth(S.cursor);
  const gridStart = mondayOf(first);
  const daysInMonth = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
  const offset = (first.getDay() + 6) % 7;
  const cells = Math.ceil((offset + daysInMonth) / 7) * 7;
  const today = todayISO();
  const monthLast = ymd(new Date(first.getFullYear(), first.getMonth(), daysInMonth));
  const parts = [
    coverageBanner(ymd(first), monthLast, `${MONTHS[first.getMonth()]} ${first.getFullYear()}`),
    `<div class="month"><div class="weekday-row">${DOW_SHORT.map((d) => `<div>${d}</div>`).join('')}</div>`,
    '<div class="month-grid">',
  ];
  for (let i = 0; i < cells; i += 1) {
    const date = addDays(gridStart, i);
    const iso = ymd(date);
    const outsideMonth = date.getMonth() !== first.getMonth();
    const inData = inWindow(iso);
    const entries = inData ? entriesFor(iso) : [];
    const slotCount = entries.length;
    const roomCount = new Set(entries.map((e) => e.room.id)).size;
    const ratio = slotCount / S.maxPerDay;
    const cls = ['day'];
    if (!inData) cls.push('outside');
    else if (outsideMonth) cls.push('edge');
    if (iso === today) cls.push('today');
    if (iso === S.selectedDay) cls.push('selected');
    if (!slotCount) cls.push('none');
    const badge = slotCount ? String(slotCount) : '–';
    const stat = !inData ? 'outside data'
      : (slotCount ? `<b>${roomCount}</b> room${roomCount === 1 ? '' : 's'}` : 'nothing free');
    parts.push(`
      <div class="${cls.join(' ')}" data-day="${iso}" data-outside="${inData ? 0 : 1}"
           title="${esc(fmtLong(iso))} — ${slotCount ? `${slotCount} slots, ${roomCount} rooms` : (inData ? 'nothing bookable' : 'outside the scanned window')}">
        <div class="dnum">${date.getDate()}</div>
        <div class="badge">${badge}</div>
        <div class="heatbar"><i style="width:${slotCount ? Math.max(8, ratio * 100) : 0}%"></i></div>
        <div class="dstat">${stat}</div>
      </div>`);
  }
  parts.push('</div>');
  parts.push(`<div class="legend">fewer
    <span class="scale">${[0.12, 0.3, 0.5, 0.7, 0.9].map((a) => `<i style="background:rgba(253,183,19,${a})"></i>`).join('')}</span>
    more slots per day</div></div>`);
  return parts.join('');
}

function weekHtml() {
  const start = mondayOf(S.cursor);
  const days = Array.from({ length: 7 }, (_, i) => ymd(addDays(start, i)));
  const today = todayISO();

  let lo = 24; let hi = -1;
  for (const iso of days) {
    for (const e of entriesFor(iso)) {
      const h = hourOf(e.time);
      lo = Math.min(lo, h); hi = Math.max(hi, h);
    }
  }
  if (hi < 0) { lo = 9; hi = 22; }
  lo = Math.max(7, Math.min(lo - 1, 12));
  hi = Math.min(23, Math.max(hi + 1, 20));

  const parts = [
    coverageBanner(days[0], days[6],
      `${parseYmd(days[0]).getDate()} ${MONTHS[parseYmd(days[0]).getMonth()].slice(0, 3)}`
      + ` – ${parseYmd(days[6]).getDate()} ${MONTHS[parseYmd(days[6]).getMonth()].slice(0, 3)}`),
    '<div class="week">', '<div></div>'];
  days.forEach((iso) => {
    const d = parseYmd(iso);
    const cls = ['wk-head'];
    if (iso === today) cls.push('today');
    if (iso === S.selectedDay) cls.push('selected');
    parts.push(`<div class="${cls.join(' ')}" data-day="${iso}">
        <b>${DOW_SHORT[(d.getDay() + 6) % 7]} ${d.getDate()}</b>
        <span>${MONTHS[d.getMonth()].slice(0, 3)} · ${entriesFor(iso).length || '–'}</span>
      </div>`);
  });
  for (let h = lo; h <= hi; h += 1) {
    parts.push(`<div class="hour-lbl">${hourLabel(h)}</div>`);
    for (const iso of days) {
      const count = inWindow(iso) ? roomsAtHour(iso, h) : 0;
      const entries = count ? entriesFor(iso).filter((e) => hourOf(e.time) === h) : [];
      const times = [...new Set(entries.map((e) => e.time))].slice(0, 3).join(' · ');
      const sel = (iso === S.selectedDay && S.selectedHour === h) ? ' selected' : '';
      const alpha = count ? heatAlpha(count / S.maxPerHour) : 0;
      const style = count ? `background:rgba(253,183,19,${alpha.toFixed(3)});border-color:rgba(253,183,19,.35)` : '';
      parts.push(`<div class="slot ${count ? '' : 'empty'}${sel}" data-day="${iso}" data-hour="${h}"
          style="${style}" title="${count ? `${count} rooms at ${hourLabel(h)} — ${times}` : 'nothing at this hour'}">
          ${count ? `<span class="cnt">${count}</span><span class="times">${esc(times)}</span>`
    : '<span class="times">·</span>'}
        </div>`);
    }
  }
  parts.push('</div>');
  parts.push(`<div class="legend">cell = how many rooms have a slot starting in that hour
    <span class="scale">${[0.12, 0.3, 0.5, 0.7, 0.9].map((a) => `<i style="background:rgba(253,183,19,${a})"></i>`).join('')}</span></div>`);
  return parts.join('');
}

/* ----------------------------------------------------------- rooms view */
function roomRowHtml(room) {
  const players = room.players_min
    ? `${room.players_min}–${room.players_max} players` : 'players unknown';
  const dur = room.duration_min ? `${room.duration_min} min` : '';
  const next = Object.keys(room.slots || {}).sort()[0];
  const tags = (room.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join('');
  const dead = room.unsupported || room.status === 'error';
  return `
    <div class="rv-room ${dead ? 'dead' : ''}" data-roompick="${esc(room.id)}"
         title="${esc(room.room)} — ${room.total_slots} open slots">
      <div class="rv-main">
        <div class="rv-name">${esc(room.room)} ${tags}</div>
        <div class="rv-meta">${esc(players)}${dur ? ` · ${dur}` : ''}${next ? ` · next ${esc(fmtDay(next))}` : ''}</div>
      </div>
      <div class="rv-count">${room.unsupported ? '—' : room.total_slots}</div>
      <div class="rv-go">${room.unsupported ? '' : '›'}</div>
    </div>`;
}

function roomsViewHtml() {
  const filtered = Boolean(S.players) || S.tags.size > 0;
  const byProvider = new Map();
  for (const room of S.data.rooms) {
    if (S.hidden.has(room.provider_id)) continue;
    if (!byProvider.has(room.provider_id)) byProvider.set(room.provider_id, []);
    byProvider.get(room.provider_id).push(room);
  }
  const order = S.data.providers
    .filter((p) => byProvider.has(p.id))
    .sort((a, b) => (a.supported === b.supported ? a.name.localeCompare(b.name) : (a.supported ? -1 : 1)));

  const shownTotal = [...byProvider.values()]
    .flat()
    .filter((r) => (r.unsupported ? !filtered : passesFilters(r))).length;
  const liveTotal = S.data.rooms.filter((r) => !r.unsupported && r.total_slots > 0).length;
  const emptyProviders = [];

  const parts = [`<div class="rooms-view">
    <div class="rv-summary">${shownTotal} of ${S.data.rooms.length} rooms
      ${filtered ? 'match the current filters' : 'in the catalogue'}
      · ${liveTotal} with live slots · click a room to see all its slots</div>`];

  for (const prov of order) {
    const rooms = byProvider.get(prov.id);
    const shown = rooms.filter((r) => (r.unsupported ? !filtered : passesFilters(r)));
    if (!shown.length) { emptyProviders.push(prov.name); continue; }
    const slots = shown.reduce((a, r) => a + (r.total_slots || 0), 0);
    const dot = prov.supported ? (prov.slots ? 'var(--green)' : 'var(--gold)') : 'var(--red)';
    parts.push(`
      <section class="rv-provider">
        <header class="rv-head">
          <span class="dot" style="background:${dot}"></span>
          <b>${esc(prov.name)}</b>
          <span class="rv-sub">${shown.length}/${rooms.length} rooms · ${slots} slots</span>
          ${prov.supported ? '' : `<span class="rv-sub">not readable — ${esc(prov.reason || '')}</span>`}
        </header>
        ${shown.sort((a, b) => a.room.localeCompare(b.room)).map(roomRowHtml).join('')}
      </section>`);
  }
  if (emptyProviders.length) {
    parts.push(`<div class="rv-summary">no matching room at: ${esc(emptyProviders.join(', '))}</div>`);
  }
  parts.push('</div>');
  return parts.join('');
}

function wireCalendar() {
  document.querySelectorAll('[data-scanend]').forEach((btn) => {
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      scanTo(btn.dataset.scanend);
    });
  });
  document.querySelectorAll('[data-roompick]').forEach((row) => {
    row.addEventListener('click', () => setRoom(row.dataset.roompick));
  });
  document.querySelectorAll('[data-day]').forEach((el) => {
    el.addEventListener('click', () => {
      const iso = el.dataset.day;
      const hour = el.dataset.hour;
      if (!inWindow(iso)) return;
      S.selectedDay = iso;
      S.selectedHour = hour === undefined ? null : Number(hour);
      if (S.view === 'month' && parseYmd(iso).getMonth() !== S.cursor.getMonth()) {
        S.cursor = startOfMonth(parseYmd(iso));
        renderRangeLabel();
      }
      if (S.view === 'week') S.cursor = mondayOf(parseYmd(iso));
      renderCalendar();
      renderDetails();
    });
  });
}

/* --------------------------------------------------------------- details */
function renderDetails() {
  const el = document.getElementById('details');
  if (!S.data) { el.innerHTML = '<div class="skeleton">Waiting for first scan…</div>'; return; }
  if (S.roomId) el.innerHTML = roomPanelHtml();
  else if (S.selectedDay && S.selectedHour !== null) el.innerHTML = hourPanelHtml();
  else if (S.selectedDay) el.innerHTML = dayPanelHtml();
  else el.innerHTML = overviewHtml();
  wireDetails();   // every panel has its own interactive rows
}

function overviewHtml() {
  const rooms = visibleRooms();
  const totalSlots = [...S.index.values()].reduce((a, list) => a + list.length, 0);
  const daysWith = [...S.index.keys()].filter((iso) => (S.index.get(iso) || []).length).length;
  const next = nextOpenDay(S.data.window.start);
  const busiest = [...S.index.entries()]
    .sort((a, b) => b[1].length - a[1].length)
    .slice(0, 6);

  return `
    <h2>At a glance</h2>
    <div class="sub">${visibleRooms().length} rooms · ${S.data.window.start} → ${S.data.window.end}</div>
    <div class="stat-grid">
      <div class="stat"><div class="v">${totalSlots}</div><div class="k">open slots</div></div>
      <div class="stat"><div class="v">${daysWith}</div><div class="k">days with slots</div></div>
      <div class="stat"><div class="v">${rooms.filter((r) => r.total_slots > 0).length}</div><div class="k">bookable rooms</div></div>
      <div class="stat"><div class="v">${next ? esc(fmtDay(next)) : '—'}</div><div class="k">next free day</div></div>
    </div>

    <h3>Busiest days coming up</h3>
    ${busiest.length ? busiest.map(([iso, list]) => `
      <div class="room-row" data-jump="${iso}">
        <div><div class="rn">${esc(fmtDay(iso))}</div>
          <div class="rp">${new Set(list.map((e) => e.room.id)).size} rooms · ${list.length} slots</div></div>
        <div class="rt">${list[0].time} →</div>
      </div>`).join('') : '<div class="empty-note">No slots in this window.</div>'}

    <h3>Sources</h3>
    ${S.data.providers.map((p) => {
    const live = p.supported;
    const blocked = live && p.rooms_failed && p.rooms_failed === p.rooms;
    const idle = live && !p.slots && !p.rooms_failed;
    const note = !live ? (p.reason || 'not readable')
      : (blocked ? `could not read (${(p.error || '').slice(0, 46)}…)`
        : (idle ? 'online booking closed' : `${p.slots} slots`));
    const dot = !live || blocked ? 'background:var(--red)'
      : (idle ? 'background:var(--gold)' : '');
    return `
      <div class="provider-line ${live && !blocked ? '' : 'dead'}" title="${esc(p.reason || p.error || '')}">
        <span class="dot" style="${dot}"></span>
        <b>${esc(p.name)}</b> · ${p.rooms} rooms · ${esc(note)}
      </div>`;
  }).join('')}

    <h3>How to read this</h3>
    <div class="empty-note" style="padding:0">
      Click a day for its one-hour slots, then an hour for every room free in it.
      Search a room above to follow a single room across the whole month.
    </div>`;
}

function dayPanelHtml() {
  const iso = S.selectedDay;
  const entries = entriesFor(iso);
  const hours = hoursFor(iso);
  const roomCount = new Set(entries.map((e) => e.room.id)).size;
  const head = `
    <h2>${esc(fmtLong(iso))}</h2>
    <div class="sub">${entries.length ? `${entries.length} slots · ${roomCount} rooms` : 'nothing bookable'}</div>`;

  if (!entries.length) {
    const next = nextOpenDay(iso);
    return `${head}
      <div class="empty-note">No room has a free slot on this day.</div>
      ${next ? `<button class="chip" data-jump="${next}">Jump to ${esc(fmtDay(next))} →</button>` : ''}`;
  }

  const blocks = [...hours.keys()].sort((a, b) => a - b).map((h) => {
    const list = hours.get(h);
    const uniqueRooms = new Set(list.map((e) => e.room.id)).size;
    const open = (S.selectedHour === h) || (S.selectedHour === null && h === [...hours.keys()].sort((a, b) => a - b)[0]);
    return `
      <div class="hour-block ${open ? 'open' : ''}" data-hourblock="${h}">
        <div class="hour-head" data-hour="${h}">
          <span class="hh">${hourLabel(h)}</span>
          <span class="hc">${uniqueRooms} room${uniqueRooms === 1 ? '' : 's'}</span>
          <span class="chev">▸</span>
        </div>
        <div class="hour-rooms">
          ${list.map((e) => `
            <div class="room-row" data-room="${esc(e.room.id)}" data-day="${iso}" data-hour="${h}">
              <div><div class="rn">${esc(e.room.room)}</div>
                <div class="rp">${esc(e.room.provider)}${e.end ? ` · until ${e.end}` : ''}</div></div>
              <div class="rt">${e.time}
                <a class="book" href="${esc(e.room.schedule_url || e.room.url)}" target="_blank"
                   rel="noopener" title="open ${esc(e.room.provider)} booking page"
                   onclick="event.stopPropagation()">↗</a></div>
            </div>`).join('')}
        </div>
      </div>`;
  }).join('');

  return `${head}${blocks}`;
}

function hourPanelHtml() {
  const iso = S.selectedDay;
  const hour = S.selectedHour;
  const list = entriesFor(iso).filter((e) => hourOf(e.time) === hour);
  const uniqueRooms = new Set(list.map((e) => e.room.id)).size;
  if (!list.length) {
    return `<h2>${esc(fmtDay(iso))} · ${hourLabel(hour)}</h2>
      <div class="sub">no rooms</div>
      <div class="empty-note">Nothing starts in this hour.</div>
      <button class="chip" data-backday="1">← back to the day</button>`;
  }
  return `
    <h2>${esc(fmtDay(iso))} · ${hourLabel(hour)}–${hourLabel((hour + 1) % 24)}</h2>
    <div class="sub">${uniqueRooms} room${uniqueRooms === 1 ? '' : 's'} available in this hour</div>
    ${list.map((e) => `
      <div class="room-row" data-room="${esc(e.room.id)}">
        <div><div class="rn">${esc(e.room.room)}</div>
          <div class="rp">${esc(e.room.provider)}${e.end ? ` · until ${e.end}` : ''}</div></div>
        <div class="rt">${e.time}
          <a class="book" href="${esc(e.room.schedule_url || e.room.url)}" target="_blank"
             rel="noopener" title="open ${esc(e.room.provider)} booking page"
             onclick="event.stopPropagation()">↗</a></div>
      </div>`).join('')}
    <div style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap">
      <button class="chip" data-backday="1">← all hours that day</button>
    </div>`;
}

function roomPanelHtml() {
  const room = S.data.rooms.find((r) => r.id === S.roomId);
  if (!room) return overviewHtml();
  const days = Object.entries(room.slots || {})
    .filter(([, list]) => list && list.length)
    .sort((a, b) => a[0].localeCompare(b[0]));
  const total = days.reduce((a, [, list]) => a + list.length, 0);

  const groups = days.map(([iso, list]) => {
    const times = [...new Set(list.map((raw) => normTime(slotOf(raw).time)).filter(Boolean))].sort();
    return `
      <div class="day-group">
        <div class="dg-head"><b>${esc(fmtDay(iso))}</b><span>${times.length} slot${times.length === 1 ? '' : 's'}</span></div>
        <div class="slot-list">
          ${times.map((t) => `<button class="slot-pill" data-day="${iso}" data-hour="${hourOf(t)}">${t}</button>`).join('')}
        </div>
      </div>`;
  }).join('');

  return `
    <h2>${esc(room.room)}</h2>
    <div class="sub">${esc(room.provider)} · ${total} slots on ${days.length} days</div>
    ${room.status === 'error' ? `<div class="banner" style="margin:0 0 12px">
        <span>Could not read this room's calendar: ${esc(room.error || '')}</span>
        <a class="book" href="${esc(room.schedule_url || room.url)}" target="_blank" rel="noopener">check manually ↗</a>
      </div>` : (room.status === 'ok' ? '' : `<div class="banner" style="margin:0 0 12px">
        <span>${esc(room.error || `${room.failed_days} day(s) could not be read`)}</span>
        <a class="book" href="${esc(room.schedule_url || room.url)}" target="_blank" rel="noopener">book page ↗</a>
      </div>`)}
    <div class="stat-grid">
      <div class="stat"><div class="v">${total}</div><div class="k">open slots</div></div>
      <div class="stat"><div class="v">${days.length}</div><div class="k">bookable days</div></div>
    </div>
    <h3>All bookable slots</h3>
    ${days.length ? groups : '<div class="empty-note">No slots in the scanned window.</div>'}
    <div style="margin-top:14px">
      <a class="book" href="${esc(room.schedule_url || room.url)}" target="_blank" rel="noopener">Book on ${esc(room.provider)} ↗</a>
    </div>`;
}

function wireDetails() {
  const el = document.getElementById('details');
  el.querySelectorAll('[data-hourblock]').forEach((block) => {
    block.querySelector('.hour-head').addEventListener('click', () => {
      const h = Number(block.dataset.hourblock);
      const isOpen = block.classList.contains('open');
      el.querySelectorAll('[data-hourblock]').forEach((b) => b.classList.remove('open'));
      if (!isOpen) {
        block.classList.add('open');
        S.selectedHour = h;
        renderCalendar();
      } else {
        S.selectedHour = null;
      }
    });
  });
  el.querySelectorAll('[data-room]').forEach((row) => {
    row.addEventListener('click', () => {
      const iso = row.dataset.day;
      const hour = row.dataset.hour;
      if (iso && hour !== undefined) { S.selectedDay = iso; S.selectedHour = Number(hour); }
      setRoom(row.dataset.room);
    });
  });
  el.querySelectorAll('[data-backday]').forEach((btn) => {
    btn.addEventListener('click', () => { S.selectedHour = null; renderCalendar(); renderDetails(); });
  });
  el.querySelectorAll('[data-jump]').forEach((btn) => {
    btn.addEventListener('click', () => {
      S.selectedDay = btn.dataset.jump;
      S.selectedHour = null;
      S.cursor = S.view === 'month' ? startOfMonth(parseYmd(S.selectedDay)) : mondayOf(parseYmd(S.selectedDay));
      renderRangeLabel(); renderCalendar(); renderDetails();
    });
  });
  el.querySelectorAll('.slot-pill[data-day]').forEach((pill) => {
    pill.addEventListener('click', () => {
      S.selectedDay = pill.dataset.day;
      S.selectedHour = Number(pill.dataset.hour);
      S.cursor = S.view === 'month' ? startOfMonth(parseYmd(S.selectedDay)) : mondayOf(parseYmd(S.selectedDay));
      renderRangeLabel(); renderCalendar(); renderDetails();
    });
  });
}

/* ------------------------------------------------------------------ state */
function syncUrl() {
  if (!S.data || typeof history === 'undefined') return;
  const params = new URLSearchParams();
  params.set('view', S.view);
  if (S.selectedDay) params.set('day', S.selectedDay);
  if (S.roomId) params.set('room', S.roomId);
  if (S.players) params.set('players', String(S.players));
  if (S.tags.size) params.set('tags', [...S.tags].join(','));
  history.replaceState(null, '', `?${params.toString()}`);
}

function applyDeepLink() {
  if (typeof location === 'undefined') return;
  const params = new URLSearchParams(location.search || '');
  const view = params.get('view');
  if (view === 'week' || view === 'month' || view === 'rooms') {
    S.view = view;
    document.querySelectorAll('[data-view]').forEach((b) => {
      b.classList.toggle('active', b.dataset.view === view);
    });
  }
  const day = params.get('day');
  if (day && /^\d{4}-\d{2}-\d{2}$/.test(day)) {
    S.selectedDay = day;
    S.cursor = S.view === 'month' ? startOfMonth(parseYmd(day)) : mondayOf(parseYmd(day));
  }
  const room = params.get('room');
  if (room && S.data.rooms.some((r) => r.id === room)) S.roomId = room;
  const players = Number(params.get('players'));
  if (players >= 1 && players <= 12) S.players = players;
  const tags = params.get('tags');
  if (tags) tags.split(',').filter(Boolean).forEach((t) => S.tags.add(t));
}

function setRoom(id) {
  S.roomId = id;
  rebuildIndex();
  renderFocus();
  renderCalendar();
  renderDetails();
  const box = document.getElementById('searchResults');
  box.hidden = true;
  document.getElementById('roomSearch').value = id
    ? (S.data.rooms.find((r) => r.id === id) || {}).room || '' : '';
}

function renderAll() {
  renderRangeLabel();
  renderChips();
  renderFilters();
  renderFocus();
  renderCalendar();
  renderDetails();
  syncUrl();
  const stamp = document.getElementById('dataStamp');
  if (S.data && S.data.generated_at) {
    const when = new Date(S.data.generated_at);
    stamp.textContent = `data ${when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} · ${S.data.window.days} days · ${S.data.took_seconds}s scan`;
  }
  const supported = S.data ? S.data.providers.filter((p) => p.supported) : [];
  const readable = S.data ? S.data.rooms.filter((r) => !r.unsupported).length : 0;
  const totalRooms = S.data ? S.data.rooms.length : 0;
  document.getElementById('coverage').textContent = S.data
    ? `${readable}/${totalRooms} rooms readable · ${supported.length}/${S.data.providers.length} providers live` : '';
}

/* ------------------------------------------------------------------- load */
async function loadState() {
  let data;
  try {
    const res = await fetch(dataUrl(), { cache: 'no-cache' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch (err) {
    document.getElementById('calendar').innerHTML =
      `<div class="skeleton">Could not load the snapshot (${esc(String(err.message || err))}).<br />`
      + 'It is republished every 15 minutes — reload in a moment.</div>';
    return;
  }
  if (data.status === 'empty') {
    S.data = null;
    document.getElementById('calendar').innerHTML = STATIC_SITE
      ? '<div class="skeleton">No snapshot published yet — the first scrape runs within 15 minutes.</div>'
      : '<div class="skeleton">First scan running… this takes ~1 minute. The page reloads itself.</div>';
    if (!STATIC_SITE) pollProgress();
    return;
  }
  S.data = data;
  if (!S.cursor) {
    const anchor = data.window.start;
    S.view === 'month' ? (S.cursor = startOfMonth(parseYmd(anchor))) : (S.cursor = mondayOf(parseYmd(anchor)));
  }
  if (!S.selectedDay) {
    const next = nextOpenDay(todayISO()) || data.window.start;
    S.selectedDay = next;
  }
  applyDeepLink();
  rebuildIndex();
  renderAll();
}

async function pollProgress() {
  clearTimeout(S.pollTimer);
  if (STATIC_SITE) return;   // the static deployment has no scan endpoint
  const res = await fetch('/api/progress');
  const p = await res.json();
  const el = document.getElementById('scanProgress');
  if (p.running) {
    el.hidden = false;
    const pct = p.total ? Math.min(100, (p.done / p.total) * 100) : 0;
    el.querySelector('.bar i').style.width = `${pct.toFixed(1)}%`;
    el.querySelector('.scan-text').textContent = `scanning ${p.done}/${p.total}`;
    S.pollTimer = setTimeout(pollProgress, 900);
  } else {
    el.hidden = true;
    if (p.phase === 'done' && (S._wasScanning || !S.data)) {
      S._wasScanning = false;
      await loadState();
      toast(`Scan finished — ${p.message || 'data refreshed'}`);
      return;
    }
    S.pollTimer = setTimeout(pollProgress, 4000);
  }
  if (p.running) S._wasScanning = true;
}

/* ----------------------------------------------------------------- events */
function wireEvents() {
  document.querySelectorAll('[data-view]').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('[data-view]').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      S.view = btn.dataset.view;
      if (S.view !== 'rooms') {
        S.cursor = S.view === 'month'
          ? startOfMonth(parseYmd(S.selectedDay || todayISO()))
          : mondayOf(parseYmd(S.selectedDay || todayISO()));
      }
      S.selectedHour = null;
      renderRangeLabel(); renderCalendar(); renderDetails();
    });
  });

  document.getElementById('prev').onclick = () => {
    S.cursor = S.view === 'month'
      ? new Date(S.cursor.getFullYear(), S.cursor.getMonth() - 1, 1)
      : addDays(S.cursor, -7);
    S.selectedHour = null;
    renderRangeLabel(); renderCalendar();
  };
  document.getElementById('next').onclick = () => {
    S.cursor = S.view === 'month'
      ? new Date(S.cursor.getFullYear(), S.cursor.getMonth() + 1, 1)
      : addDays(S.cursor, 7);
    S.selectedHour = null;
    renderRangeLabel(); renderCalendar();
  };
  document.getElementById('today').onclick = () => {
    const iso = todayISO();
    S.cursor = S.view === 'month' ? startOfMonth(parseYmd(iso)) : mondayOf(parseYmd(iso));
    S.selectedDay = iso; S.selectedHour = null;
    renderRangeLabel(); renderCalendar(); renderDetails();
  };

  document.getElementById('providerChips').addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-provider]');
    if (!btn) return;
    const id = btn.dataset.provider;
    if (id === '__all') S.hidden.clear();
    else if (S.hidden.has(id)) S.hidden.delete(id);
    else S.hidden.add(id);
    rebuildIndex(); renderAll();
  });

  /* ---- player / genre filters ------------------------------------------ */
  document.getElementById('playersFilter').addEventListener('change', (ev) => {
    S.players = ev.target.value ? Number(ev.target.value) : null;
    rebuildIndex(); renderAll();
  });
  document.getElementById('genreFilter').addEventListener('click', (ev) => {
    const chip = ev.target.closest('[data-tag]');
    if (!chip) return;
    const tag = chip.dataset.tag;
    S.tags.has(tag) ? S.tags.delete(tag) : S.tags.add(tag);
    rebuildIndex(); renderAll();
  });
  document.getElementById('clearFilters').addEventListener('click', () => {
    S.players = null;
    S.tags.clear();
    rebuildIndex(); renderAll();
  });

  const search = document.getElementById('roomSearch');
  const results = document.getElementById('searchResults');
  search.addEventListener('input', () => {
    const q = search.value.trim().toLowerCase();
    if (!S.data || !q) { results.hidden = true; return; }
    const hits = S.data.rooms
      .filter((r) => !r.unsupported)
      .filter((r) => r.room.toLowerCase().includes(q) || r.provider.toLowerCase().includes(q))
      .slice(0, 12);
    if (!hits.length) { results.innerHTML = '<div class="empty-note">no match</div>'; results.hidden = false; return; }
    results.innerHTML = hits.map((r) => `<button data-pick="${esc(r.id)}">
        <span>${esc(r.room)}</span><span class="rp">${esc(r.provider)} · ${r.total_slots}</span></button>`).join('');
    results.hidden = false;
  });
  results.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-pick]');
    if (!btn) return;
    const room = S.data.rooms.find((r) => r.id === btn.dataset.pick);
    setRoom(btn.dataset.pick);
    const firstDay = Object.keys(room.slots || {}).sort()[0];
    if (firstDay) {
      S.selectedDay = firstDay;
      S.cursor = S.view === 'month' ? startOfMonth(parseYmd(firstDay)) : mondayOf(parseYmd(firstDay));
      renderRangeLabel(); renderCalendar(); renderDetails();
    }
  });
  document.addEventListener('click', (ev) => {
    if (!ev.target.closest('.search')) results.hidden = true;
  });

  document.getElementById('refresh').onclick = async () => {
    if (STATIC_SITE) {
      toast('Reloading the latest snapshot…');
      await loadState();
      return;
    }
    if (!S.data) { toast('still scanning…'); return; }
    const body = {
      days: S.data.window.days - 1,
      start: S.data.window.start,
      force: true,
    };
    const res = await fetch('/api/refresh', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const out = await res.json();
    if (out.started) {
      toast('Re-scanning every provider…');
      pollProgress();
    } else {
      toast(out.error || 'refresh refused');
    }
  };

  document.addEventListener('keydown', (ev) => {
    if (ev.target.tagName === 'INPUT') return;
    if (ev.key === 'ArrowLeft') document.getElementById('prev').click();
    if (ev.key === 'ArrowRight') document.getElementById('next').click();
    if (ev.key === 'Escape') setRoom(null);
  });
}

/* ------------------------------------------------------------------- boot */
(async function boot() {
  wireEvents();
  await loadState();
  if (STATIC_SITE) {
    const btn = document.getElementById('refresh');
    if (btn) btn.title = 'Reload the latest published snapshot';
  } else {
    pollProgress();
  }
})();

/* test hook: lets selftest_ui.mjs drive the view state without a real browser */
if (typeof window !== 'undefined') {
  window.__APP = { S, rebuildIndex, renderAll, renderCalendar, renderDetails, setRoom, loadState };
}
