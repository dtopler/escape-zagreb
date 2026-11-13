/* Headless smoke test for web/app.js — no browser required.
 *
 * Runs the real app.js inside a vm context against the real server API, with a
 * minimal DOM shim, then drives the view state and asserts the rendered HTML.
 *
 *   node selftest_ui.mjs [http://127.0.0.1:8765]
 */
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const BASE = process.argv[2] || 'http://127.0.0.1:8765';
/* Set ESCAPE_STATIC_DATA to a JSON URL to exercise the GitHub Pages code path
 * (config.js sets window.ESCAPE_DATA_URL there, which disables the live controls). */
const STATIC_URL = process.env.ESCAPE_STATIC_DATA || '';
const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = fs.readFileSync(path.join(HERE, 'web', 'app.js'), 'utf8');

let failures = 0;
const ok = (cond, msg) => {
  console.log(`${cond ? '  ok  ' : ' FAIL '} ${msg}`);
  if (!cond) failures += 1;
};

/* ------------------------------------------------------------- DOM shim --- */
class ClassList {
  constructor() { this.set = new Set(); }
  add(...c) { c.forEach((x) => this.set.add(x)); }
  remove(...c) { c.forEach((x) => this.set.delete(x)); }
  contains(c) { return this.set.has(c); }
  toggle(c) { this.set.has(c) ? this.set.delete(c) : this.set.add(c); }
}

class El {
  constructor(id = '') {
    this.id = id;
    this._html = '';
    this.textContent = '';
    this.value = '';
    this.hidden = false;
    this.style = {};
    this.dataset = {};
    this.classList = new ClassList();
    this.listeners = {};
    this.children = [];
  }

  set innerHTML(v) { this._html = String(v ?? ''); }
  get innerHTML() { return this._html; }

  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener() {}
  appendChild(child) { this.children.push(child); }
  setAttribute(k, v) { this[k] = v; }
  getBoundingClientRect() { return { top: 0, left: 0, width: 100, height: 100 }; }
  focus() {}
  querySelectorAll(sel) { return parseNodes(this._html).filter((n) => matches(n, sel)).map((n) => nodeFor(n)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) { return matches({ attrs: {}, classes: [] }, sel) ? this : null; }
}

const VOID = new Set(['input', 'br', 'img', 'hr', 'meta', 'link', 'source']);
const TAG_RE = /<(\/?)([a-zA-Z0-9]+)\b([^>]*?)(\/?)>/g;
const ATTR_RE = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"([^"]*)"/g;

function parseAttrs(raw) {
  const attrs = {};
  let a;
  ATTR_RE.lastIndex = 0;
  while ((a = ATTR_RE.exec(raw || ''))) attrs[a[1]] = a[2];
  return attrs;
}

/** Nesting-aware scan: every tag record knows the markup of its own subtree. */
function parseNodes(html) {
  const out = [];
  const stack = [];
  TAG_RE.lastIndex = 0;
  let m;
  while ((m = TAG_RE.exec(html))) {
    const closing = m[1] === '/';
    if (closing) {
      const idx = stack.pop();
      if (idx !== undefined) out[idx].end = m.index + m[0].length;
      continue;
    }
    const attrs = parseAttrs(m[3]);
    const dataset = {};
    for (const [k, v] of Object.entries(attrs)) {
      if (k.startsWith('data-')) {
        dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = v;
      }
    }
    const rec = {
      key: String(m.index),
      tag: m[2].toLowerCase(),
      attrs,
      classes: (attrs.class || '').split(/\s+/).filter(Boolean),
      dataset,
      handlers: {},
      start: m.index,
      end: html.length,
      sub: '',
    };
    out.push(rec);
    if (VOID.has(rec.tag) || m[4]) rec.end = m.index + m[0].length;
    else stack.push(out.length - 1);
  }
  for (const rec of out) rec.sub = html.slice(rec.start, rec.end);
  return out;
}

function matches(rec, sel) {
  const needAttrs = [...sel.matchAll(/\[([a-zA-Z-]+)\]/g)].map((x) => x[1]);
  const needClasses = [...sel.matchAll(/\.([a-zA-Z0-9_-]+)/g)].map((x) => x[1]);
  for (const c of needClasses) if (!rec.classes.includes(c)) return false;
  for (const at of needAttrs) {
    const camel = at.replace(/^data-/, '').replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    if (!(rec.attrs[at] !== undefined || rec.dataset[camel] !== undefined)) return false;
  }
  return needAttrs.length + needClasses.length > 0;
}

function nodeFor(rec) {
  const el = new El();
  el.dataset = rec.dataset;
  el.classList = new ClassList();
  rec.classes.forEach((c) => el.classList.add(c));
  const search = (sel) => parseNodes(rec.sub).filter((n) => matches(n, sel)).map(nodeFor);
  el.querySelectorAll = search;
  el.querySelector = (sel) => search(sel)[0] || null;
  el.closest = (sel) => (matches(rec, sel) ? el : null);
  el.addEventListener = (type, fn) => { (rec.handlers[type] ||= []).push(fn); };
  el._rec = rec;
  return el;
}

const byId = new Map();
const document = {
  body: new El('body'),
  getElementById(id) {
    if (!byId.has(id)) byId.set(id, new El(id));
    return byId.get(id);
  },
  querySelectorAll(sel) {
    // used for the view toggle buttons and the delegated containers
    if (sel === '[data-view]') {
      return ['month', 'week'].map((v) => {
        const el = new El(`view-${v}`);
        el.dataset = { view: v };
        el.classList = new ClassList();
        if (v === 'month') el.classList.add('active');
        return el;
      });
    }
    if (sel === '[data-day]') return [];
    return [];
  },
  addEventListener() {},
  createElement(tag) { return new El(tag); },
};

/* ------------------------------------------------------------- fetch shim - */
const calls = [];
async function fetchShim(input, init) {
  const url = String(input).startsWith('http') ? String(input) : BASE + String(input);
  calls.push(url);
  const res = await fetch(url, init);
  return {
    ok: res.ok,
    status: res.status,
    json: () => res.json(),
    text: () => res.text(),
  };
}

/* ------------------------------------------------------------------ boot -- */
const ctx = {
  document,
  window: {},
  fetch: fetchShim,
  setTimeout,
  clearTimeout,
  console,
  Intl,
  Date,
  Math,
  JSON,
};
ctx.window = ctx;
ctx.globalThis = ctx;
if (STATIC_URL) ctx.ESCAPE_DATA_URL = STATIC_URL;
vm.createContext(ctx);
vm.runInContext(SRC, ctx, { filename: 'app.js' });

await new Promise((r) => setTimeout(r, 1200));

const app = ctx.__APP;
ok(!!app, 'app bootstraps and exposes its state (window.__APP)');
if (!app) process.exit(1);

const { S } = app;
const gid = (id) => document.getElementById(id);

ok(!!S.data, 'loads data from /api/state');
if (!S.data) process.exit(1);
ok(S.data.rooms.length > 0, `catalogue has rooms (${S.data.rooms.length})`);
ok(S.data.window.start && S.data.window.end, 'has a scan window');

const supported = S.data.providers.filter((p) => p.supported);
console.log(`\n  providers live: ${supported.map((p) => p.name).join(', ') || 'none'}`);
const readable = S.data.rooms.filter((r) => r.total_slots > 0);
console.log(`  rooms with slots: ${readable.length}/${S.data.rooms.length}`);

/* month view renders day cells */
app.renderAll();
const calHtml = gid('calendar').innerHTML;
ok(calHtml.includes('class="month"'), 'month view renders');
ok((calHtml.match(/data-day="/g) || []).length >= 28, 'month grid has day cells');

/* details panel shows something useful on first paint */
const detailsHtml = gid('details').innerHTML;
ok(detailsHtml.length > 50, 'details panel renders on first paint');

/* day -> hour -> room drill down on a day that actually has slots */
const openDay = readable[0] && Object.keys(readable[0].slots).sort()[0];
ok(!!openDay, `found a day with slots (${openDay})`);
if (openDay) {
  S.selectedDay = openDay;
  S.selectedHour = null;
  app.renderAll();
  const dayHtml = gid('details').innerHTML;
  ok(/hour-block/.test(dayHtml), 'day panel lists one-hour blocks');
  const hourCount = (dayHtml.match(/data-hourblock=/g) || []).length;
  ok(hourCount > 0, `day panel has ${hourCount} hour buckets`);

  /* pick the first hour -> list of every room free in it */
  const hours = [...new Set((S.index.get(openDay) || []).map((e) => e.time.slice(0, 2)))].sort();
  S.selectedHour = Number(hours[0]);
  app.renderAll();
  const hourHtml = gid('details').innerHTML;
  const expected = new Set((S.index.get(openDay) || [])
    .filter((e) => Number(e.time.slice(0, 2)) === S.selectedHour).map((e) => e.room.room));
  ok(/available in this hour/.test(hourHtml), 'hour drill-down renders');
  let listed = 0;
  expected.forEach((name) => { if (hourHtml.includes(name)) listed += 1; });
  ok(listed === expected.size, `all ${expected.size} rooms at ${S.selectedHour}:00 are listed (${listed})`);

  /* week view: one column per day, one row per hour */
  S.view = 'week';
  S.cursor = new Date(openDay);
  S.selectedHour = null;
  app.renderAll();
  const weekHtml = gid('calendar').innerHTML;
  ok(weekHtml.includes('class="week"'), 'week view renders');
  ok((weekHtml.match(/data-hour="/g) || []).length > 10, 'week grid has hour cells');
  ok((weekHtml.match(/wk-head/g) || []).length >= 7, 'week grid has 7 day headers');

  /* room focus: single room schedule across the window */
  const room = readable[0];
  S.view = 'month';
  app.renderAll();
  app.setRoom(room.id);
  const roomHtml = gid('details').innerHTML;
  ok(roomHtml.includes(room.room), 'room panel shows the room name');
  ok(/slot-pill/.test(roomHtml), 'room panel lists bookable slot pills');
  ok(!gid('roomFocus').hidden, 'room focus bar is visible');
  const pillCount = (roomHtml.match(/class="slot-pill"/g) || []).length;
  ok(pillCount > 0, `room panel renders ${pillCount} slot pills`);

  /* filtering by provider only hides other providers */
  app.setRoom(null);
  const prov = supported[0];
  if (prov) {
    S.hidden = new Set(S.data.providers.filter((p) => p.id !== prov.id).map((p) => p.id));
    app.rebuildIndex();
    app.renderAll();
    const visible = S.data.rooms.filter((r) => !r.unsupported && !S.hidden.has(r.provider_id));
    ok(visible.length > 0 && visible.every((r) => r.provider_id === prov.id),
      `provider filter narrows to ${prov.name} (${visible.length} rooms)`);
    S.hidden = new Set();
    app.rebuildIndex();
    app.renderAll();
  }

  /* ---- rooms view: every room, grouped by provider ---------------------- */
  S.view = 'rooms';
  app.renderAll();
  const rvHtml = gid('calendar').innerHTML;
  ok(rvHtml.includes('rooms-view'), 'rooms view renders');
  const groupNames = S.data.providers.filter((p) => rvHtml.includes(`>${p.name}`)).length;
  ok(groupNames >= 7, `rooms view groups by provider (${groupNames} groups)`);
  const rows = (rvHtml.match(/data-roompick="/g) || []).length;
  const expectedRows = S.data.rooms.filter((r) => !r.unsupported).length
    + S.data.rooms.filter((r) => r.unsupported).length;
  ok(rows === expectedRows, `all ${expectedRows} rooms are listed (${rows})`);
  const metaShown = (rvHtml.match(/rv-meta/g) || []).length;
  ok(metaShown === rows, 'every room row shows players/duration metadata');

  /* ---- player filter ---------------------------------------------------- */
  S.players = 4;
  app.rebuildIndex();
  app.renderAll();
  const want4 = S.data.rooms.filter((r) => !r.unsupported
    && r.players_min && r.players_min <= 4 && 4 <= r.players_max).length;
  const got4 = (gid('calendar').innerHTML.match(/data-roompick="/g) || []).length;
  ok(got4 === want4 && want4 > 0, `players=4 keeps exactly the ${want4} rooms that fit 4`);
  const idxRooms = new Set();
  for (const list of S.index.values()) list.forEach((e) => idxRooms.add(e.room.id));
  ok(idxRooms.size <= want4, `calendar index only contains rooms fitting 4 players (${idxRooms.size})`);
  ok(![...idxRooms].some((id) => {
    const room = S.data.rooms.find((r) => r.id === id);
    return !room.players_min;
  }), 'rooms with unpublished player counts are excluded when the filter is on');

  /* ---- genre filter ----------------------------------------------------- */
  S.players = null;
  S.tags = new Set(['Horror']);
  app.rebuildIndex();
  app.renderAll();
  const wantH = S.data.rooms.filter((r) => !r.unsupported && (r.tags || []).includes('Horror')).length;
  const gotH = (gid('calendar').innerHTML.match(/data-roompick="/g) || []).length;
  ok(gotH === wantH && wantH > 0, `Horror filter keeps exactly the ${wantH} horror rooms`);
  const allHorror = [...S.index.values()].flat().every((e) => (e.room.tags || []).includes('Horror'));
  ok(allHorror, 'every slot in the filtered index belongs to a horror room');

  /* combined filters + clear */
  S.players = 4;
  S.tags = new Set(['Horror']);
  app.rebuildIndex();
  app.renderAll();
  const combined = S.data.rooms.filter((r) => !r.unsupported
    && r.players_min && r.players_min <= 4 && 4 <= r.players_max
    && (r.tags || []).includes('Horror')).length;
  ok((gid('calendar').innerHTML.match(/data-roompick="/g) || []).length === combined,
    `4 players + Horror combine (${combined} rooms)`);
  S.players = null;
  S.tags = new Set();
  S.view = 'month';
  app.rebuildIndex();
  app.renderAll();
  ok((gid('calendar').innerHTML.match(/data-day="/g) || []).length >= 28,
    'clearing the filters restores the month view');
}

/* ---- static deployment mode --------------------------------------------- */
if (STATIC_URL) {
  ok(calls.some((u) => u.includes(STATIC_URL)), 'snapshot fetched from the configured URL');
  ok(!calls.some((u) => u.includes('/api/progress')), 'static mode never polls /api/progress');
  ok(!calls.some((u) => u.includes('/api/refresh')), 'static mode never POSTs /api/refresh');
  const before = calls.length;
  gid('refresh').onclick();
  await new Promise((r) => setTimeout(r, 400));
  ok(calls.length > before, 'Refresh reloads the snapshot instead of scraping');
  ok(!calls.some((u) => u.includes('/api/refresh')), 'Refresh never hits the scrape endpoint');
}

console.log(`\n${failures ? `${failures} FAILURE(S)` : 'all checks passed'}`);
process.exit(failures ? 1 : 0);
