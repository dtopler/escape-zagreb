# Escape Room Zagreb — availability explorer

A tiny local web app that reads the live booking calendars of the escape rooms
listed on [escaperoomzagreb.com/escape-room-rezervacije](https://www.escaperoomzagreb.com/escape-room-rezervacije/)
(plus rooms that site forgets to link) and answers three questions:

1. **Which rooms have anything free this week / this month?** — month heatmap and
   a week grid of hour cells.
2. **What is free at a given hour?** — click a day, then an hour, and get the list
   of every room that has a slot starting in that hour.
3. **When can I play *this* room?** — pick a room and every one of its bookable
   slots across the whole scanned window is listed.
4. **Which rooms fit my group?** — a *Rooms* view lists every room grouped by
   provider, with player range, duration and genres, and filters for the number
   of players and for type (Horror, Heist, Detective, Historic, Outdoor, ...).

Times are **Europe/Zagreb**. Nothing is invented: a slot only appears if the
provider's own booking widget currently offers it.

Current coverage: **39 rooms / 9 providers**, of which **7 providers (35 rooms)**
are scraped live — ~4,000 open slots over a 31-day window.

---

## Quick start

```powershell
cd escape-zagreb
.\run.cmd                  # scans if needed, then serves http://127.0.0.1:8765/
```

`run.cmd` works under the default PowerShell execution policy (which is
`Restricted` on this machine and blocks `.ps1` files). If you prefer PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File run.ps1
```

or step by step with plain Python:

```powershell
python catalog.py         # 1. read the directory page -> data/rooms.json
python aggregate.py 30    # 2. scrape every room x 31 days -> data/availability.json
python server.py          # 3. serve the UI on http://127.0.0.1:8765/
```

`python aggregate.py 30 --providers=cluego,lockup --force` scans a subset and
ignores the cache. The **Refresh** button in the UI does the same for the window
you are looking at; navigating past the scanned window shows a *Scan this month*
button instead.

---

## How the schedules are actually obtained

The directory page is only a catalogue of links. Every provider runs a different
booking engine, so each one gets an adapter in `providers/` that talks to the same
endpoint its own widget talks to:

| Provider | Rooms | Engine | What the adapter does | Requests / 30-day scan |
|---|---|---|---|---|
| **ClueGo** | 5 | WooCommerce Bookings | `POST /wp-admin/admin-ajax.php` `action=wc_bookings_get_blocks&form=<serialized booking form + date>` → HTML time-picker `<li><a data-value="2026-11-16T13:30:00+0100">` | ~31 per room |
| **Fox in a Box** | 3 | WooCommerce Bookings | same engine, same call | ~31 per room |
| **Escape Art** | 3 | Bookly 28.8 | 5 form POSTs to `admin-ajax.php` (`bookly_get_form_id` → `render_service` → `session_save` → `render_time`); `render_time` returns **a whole month** of `slots_data` per room. The room is the Bookly **staff** id, the player count is the *service* | ~6 per room |
| **The Old Lock Up** | 11 | classic ASP | `GET /booking-formb.asp?lang=EN&room_id=N&daysahead=90` renders the **entire 91-day calendar server-side**; a `<button disabled>` means taken. `lang` is mandatory — without it you only get a language chooser | 1 per room |
| **Enigmarium** | 4 | SimplyBook.me | `GET /v2/booking/time-slots/?from&to&provider&service` on `zagrebenigmarium.simplybook.it`; provider id (room) comes from the embed page's `SimplybookWidget` config. One **31-day range** call answers the whole window (the `busy` grid rows it also returns are dropped by `BOOKABLE_TYPES`) | ~1 per room |
| **Escape Room Arena** | 5 | DOP Booking System PRO 6.2.3 | `POST admin-ajax.php action=dopbsp_calendar_schedule_get&id=<calendar>&year=<Y>&firstYear="false"` → **double-encoded** JSON of the year's days/hours | ~1–2 per room |
| **Portal Escape** | 4 | Bookeo | `widget.js?a=<account>&type=<product>` → shard host → 3 GETs sharing an `AXIOMID` cookie → month slots come from Bookeo's DWR `POST /bookeo/srvrq` (RSA+AES wrapped body) | ~5 per room |
| **Elephant in the Escape Room** | 1 | Wix Bookings | `GET /_api/bookings/v1/availability` → **HTTP 401 `FAILED_TO_RESOLVE_INSTANCE_ID`**: Wix only accepts a signed instance JWT minted in the browser. Not readable without a headless browser | — |
| **Exit Games** | 3 | — | `www.exitgames.hr` no longer resolves (DNS NXDOMAIN); the rooms are gone | — |

The exact request/response shapes, nonces, cookie flows and traps are documented
in the top docstring of each `providers/*.py` file — that is the place to look
when a site changes.

### Rooms the directory page misses

`catalog.py` merges the scraped directory with an explicit `EXTRA_ROOMS` list.
Each entry was verified by asking the provider's own engine for slots, never by
guessing a URL:

| Room | Provider | How it was found |
|---|---|---|
| 403 FORBIDDEN | ClueGo | the site nav and `product-sitemap.xml` list a fifth booking product (`/reservation/403-forbidden-kalendar/`, product 67776) that the directory never links |
| Laboratorij crne kraljice | Enigmarium | SimplyBook `GET /v2/provider/` lists provider `7` / service `4`, which the directory omits → [room page](https://enigmarium.hr/laboratorij-crne-kraljice/) |
| Circus (room_id 10) | The Old Lock Up | the ASP calendar accepts `room_id=1..11`; the directory links only 9 → [room page](https://www.theoldlockup.com/room-detail.asp?roomid=10) |
| Missing (room_id 11) | The Old Lock Up | same calendar, `room_id=11` → [room page](https://www.theoldlockup.com/room-detail.asp?roomid=11) |

One room was also **renamed**: the directory still calls Fox in a Box's third room
"Pljačka banke", but the booking product is now *Golden Nugget Saloon* (a Wild West
adventure), so the catalogue uses the provider's current name.

Deliberately **not** added: ClueGo's `/reservation/sipsketch/` (a paint-and-sip
event, not an escape room) and its `-hr` / `-2` product duplicates; Exit Games
(site gone); Another World Zagreb in Ilica 33 (a VR arena whose own listing says
it sells VR games, not classic escape rooms) and World City Trail (an outdoor
self-guided city game).

### Room metadata: players, duration, genres

`build_meta.py` produces `data/room_meta.json`, which `catalog.py` merges into
every room. It populates:

* **players_min / players_max** and **duration_min** — from the World of Escapes
  city page, which publishes a structured range per room (`2 – 6`, `60 min`).
* **tags** — from World of Escapes' genre pages (`/zagreb/tags/scary` lists the
  horror rooms, `/zagreb/tags/heist` the heists, ...), normalised to
  Horror, Vampires, Detective, Heist, Historic, Ancient, Family, Adventure,
  Mystical, Sci-Fi, Prison, Hospital, Outdoor, VR, Challenging, Long.
* For the handful of rooms no directory tags, a small curated `MANUAL_TAGS` map
  covers only the unambiguous ones (e.g. "Pustolovina Divljeg Zapada" →
  Adventure, "na otvorenom" → Outdoor, "1202." → Historic).

```powershell
python build_meta.py       # refresh metadata (network), then: python catalog.py && python aggregate.py 30
```

**Unknown stays unknown.** Only 26 of 39 rooms publish a player range and 25 have
a genre. When a player count is selected, rooms without a published range are
*excluded* (and the UI says how many), never assumed to fit. Genre filters work
the same way.

### Filters and the Rooms view

* **Players** — "I have N people" keeps rooms whose published range covers N.
* **Type** — any number of genre chips, OR-ed together.
* Both filters apply to the month/week heatmaps too, so "4 players + Horror"
  shows which *hours* have something suitable. The state is deep-linkable:
  `?view=rooms&players=4&tags=Horror`.
* **Rooms view** lists every room grouped by provider with players, duration,
  next free day and open-slot count; clicking a room focuses it and shows all its
  slots in the side panel.



### Two findings worth knowing

* **Escape Room Arena is closed for online booking**, not broken. Every one of its
  calendars ships `"available": [false,false,false,false,false,false,false]` and
  all hours come back `status:"unavailable"`, while `/rezervacije/` tells visitors
  to call +385 1 6024 470. The adapter therefore returns `[]` — and would start
  returning times as soon as the provider re-enables weekdays.
* **Bookeo and SimplyBook throttle.** Opening many Bookeo sessions in a row gets
  a reCAPTCHA interstitial; SimplyBook starts answering with a "please wait"
  queue page. The scanner limits those providers to 1–2 parallel requests and
  falls back to the last known values (see below), so a throttle degrades the
  freshness of the data instead of blanking the page.

---

## How it works

```
                    ┌──────────────┐
escaperoomzagreb.com│  catalog.py  │  directory + EXTRA_ROOMS -> data/rooms.json
                    └──────┬───────┘
                           │ 38 rooms, 9 providers
                    ┌──────▼───────┐        providers/
                    │ aggregate.py │◄────── wc.py         (ClueGo, Fox in a Box)
                    │  threads,    │        bookly.py     (Escape Art)
                    │  per-host    │        lockup.py     (The Old Lock Up)
                    │  throttles,  │        simplybook.py (Enigmarium)
                    │  caches      │        dopbsp.py     (Escape Room Arena)
                    └──────┬───────┘        bookeo.py     (Portal Escape)
                           │                wixbookings.py(Elephant, unsupported)
                  data/availability.json
                           │
                    ┌──────▼───────┐        GET /api/state
                    │  server.py   │◄────── POST /api/refresh   ┌────────────┐
                    │ stdlib HTTP  │        GET /api/progress    │ web/ (HTML │
                    └──────────────┘                            │  + JS UI)  │
                                                                └────────────┘
```

### Data model (`data/availability.json`)

```jsonc
{
  "generated_at": "2026-11-13T20:50:00+01:00",
  "window": { "start": "2026-11-13", "end": "2026-12-13", "days": 31 },
  "providers": [ { "id": "cluego", "name": "ClueGo", "rooms": 4,
                   "supported": true, "slots": 541, "rooms_failed": 0 } ],
  "days": ["2026-11-13", "..."],
  "rooms": [ {
      "id": "cluego/krada-mona-lise", "provider_id": "cluego", "provider": "ClueGo",
      "room": "Krađa Mona Lise", "url": "…", "schedule_url": "…",
      "slots": { "2026-11-14": [{ "time": "13:30" }] },
      "total_slots": 135, "status": "ok", "error": null, "unsupported": false
  } ]
}
```

`slots[day]` holds only *bookable* start times; a missing day means closed or
fully booked. Some providers also publish an end time (`"end": "15:00"`).

### Refresh, caching and failure handling

* `data/slot_cache.json` — 20-minute TTL (override with `ESCAPE_CACHE_TTL`), so
  Refresh only re-reads what actually changed.
* `data/stale_cache.json` — last successful answer per room/day, kept for ~4
  months. If a provider blocks or times out, the old value is reused and the room
  is flagged `stale` instead of silently showing "nothing free".
* A scan is parallel across rooms but throttled per host (3) and per
  rate-limit-prone provider (`portal` 1, `enigmarium` 2).
* Provider crashes never kill a scan: failures are counted per room, which ends
  up `ok`, `partial`, `stale`, `error`, or `unsupported`.

---

## Verification

```powershell
python check_data.py                 # data integrity: times, windows, duplicates, totals
node   selftest_ui.mjs               # drives web/app.js headlessly against the API
python providers\wc.py               # each adapter has its own live self-test
python providers\lockup.py
```

`selftest_ui.mjs` runs the real `app.js` in a `vm` with a minimal DOM shim and
asserts the month grid, the week grid, the day → hour → room drill-down, the room
panel and the provider filter all render from live data.

---

## Files

| File | Purpose |
|---|---|
| `catalog.py` | scrapes the directory page (+ `EXTRA_ROOMS`) and merges `data/room_meta.json` → `data/rooms.json` |
| `build_meta.py` | builds players / duration / genres for every room → `data/room_meta.json` |
| `aggregate.py` | thin orchestrator: adapters × days → `data/availability.json` |
| `providers/base.py` | `Adapter`, `Slot`, `AdapterError` contract |
| `providers/*.py` | one module per booking engine, self-tested, heavily documented |
| `server.py` | stdlib HTTP server + JSON API (`/api/state`, `/api/refresh`, `/api/progress`) |
| `web/` | the UI: `index.html`, `style.css`, `app.js` (no build step, no dependencies) |
| `fetch.py` | cached HTTP helper used by all adapters |
| `check_data.py` | data integrity checker |
| `show_catalog.py` | prints the room catalogue (`id / room / provider / schedule url`) |
| `selftest_ui.mjs` | headless UI smoke test |
| `run.cmd` / `run.ps1` | launchers |

## Adding a provider

1. Add the room URLs to `data/rooms.json` (or fix `catalog.py` overrides if the
   directory links are stale — this was needed for ClueGo and The Old Lock Up,
   and `EXTRA_ROOMS` covers rooms the directory does not list at all).
2. Copy `providers/wc.py` as a template, implement
   `slots(room, day) -> list[Slot]`, cache the expensive per-room setup behind a
   `threading.Lock`.
3. Register the module in `providers/__init__.py` (`MODULES` + `PROVIDER_TO_MODULE`).
4. Verify with `python providers/yourmodule.py`, then `python aggregate.py 7 --providers=<id> --force`.

## Known limitations

* **Elephant in the Escape Room** cannot be read without a headless browser
  (Wix-minted instance token). The room is listed as *not readable*.
* **Exit Games** is offline — the domain no longer resolves.
* **Escape Room Arena** publishes no bookable day while its weekdays stay disabled.
* **Bookeo / SimplyBook rate limits** can leave individual days unread; those
  rooms show as `partial` and reuse cached values where possible.
* **Capacity is not modelled.** Adapters use the guest count each widget defaults
  to (2 for ClueGo, 6 for Fox in a Box); for the fixed-capacity escape rooms this
  does not change which start times exist, but a provider that prices per person
  could theoretically hide a slot.
* **Player ranges / genres are only known where a directory publishes them**
  (26 and 25 of 39 rooms). The filters exclude the rest instead of guessing, and
  the UI reports how many rooms that hid.
* Start times are exact; end times only exist where the provider publishes them.
