"""Bookly 28.8 adapter — Escape Art Zagreb (escapeart.hr), 3 rooms.

Reverse-engineered from
``wp-content/plugins/bookly-responsive-appointment-booking-tool/frontend/resources/js/bookly.min.js``.
Everything below is what the real booking widget does when you click through
it; nothing is guessed.

Endpoints
---------
Every request is a **form-encoded POST** to the URL published in the page as
``var BooklyL10n.ajaxurl``::

    POST https://escapeart.hr/wp-admin/admin-ajax.php?lang=hr
    Content-Type: application/x-www-form-urlencoded; charset=UTF-8
    X-Requested-With: XMLHttpRequest
    Referer: <room page>

with the ``action`` parameter selecting the step.  All responses are JSON.

``csrf_token`` is read from ``var BooklyL10n.csrf_token`` in the room page and
must be repeated in *every* POST body.  Responses may hand back a refreshed
``csrf_token``; it should be adopted (see ``_post``).

Flow (5 calls, then 1 call per room-month)
-----------------------------------------
1. ``GET <room page>`` — the page carries two JSON blobs::

       var BooklyL10n = {"ajaxurl": "...", "csrf_token": "77cc1c4eec", ...};
       window.bookly( {"form_id": "6af75a46d5590", "form_data": {...},
                       "skip_steps": {...}, "use_client_time_zone": 1, ...} );

   and the placeholder markup
   ``<div id="bookly-form-container-<form_id>" class="bookly-form" data-form_id="">``.
   ``<form_id>`` here is only a *container token* and is randomised on **every
   page load** — it must be parsed, never hard-coded.  There is no
   ``<form>``/``<input>`` to scrape: the widget is rendered entirely by JS.

2. ``POST action=bookly_get_form_id``
   ``form_container_id=bookly-form-container-<form_id>``, ``form_id=`` (empty),
   ``form_data=<json.dumps(cfg["form_data"])>``, ``csrf_token=...``
   -> ``{"success":true,"form_id":"83d47283df8f05feea923c10131199b6"}``

   The returned 32-hex ``form_id`` is the **session key**; all state lives
   server-side under it.  Contrary to the usual Bookly lore no cookie is
   needed and ``http.cookiejar`` is *not* used here — ``form_id`` alone is
   enough for the rest of the flow.

3. ``POST action=bookly_render_service`` ``form_id=<session>`` ``csrf_token=...``
   -> ``{"success":true,"services":{...},"staff":{...},"chain":[],"required":{"staff":1}, ...}``

   Escape Art puts **all three rooms in one form**:

   * ``services`` = *number of players* ("2 igrača" … "6 igrača"), all 90 min;
   * ``staff``    = the **rooms** ("Robin Hood", "Drakuline odaje",
     "Magična knjižnica").

   So the room is selected via ``staff_id``, and ``service_id`` only picks the
   price bracket.  Availability was measured to be byte-identical for every
   ``service_id`` of a given ``staff_id`` across whole months, so the adapter
   simply picks the lowest service id the room offers.

4. ``POST action=bookly_session_save`` — this is the "Next" button of the
   service step.  Params::

       chain[0][service_id]=3
       chain[0][staff_ids][]=6
       date_from=YYYY-MM-DD
       days[]=1 ... days[]=7

   -> ``{"success":true}``.

5. ``POST action=bookly_render_time`` ``form_id=<session>``
   ``selected_date=YYYY-MM-01`` (the *first of the month*, exactly what the
   widget fires on month-change) -> the **whole month** in one response::

       {"success":true, "has_slots":true, "has_more_slots":false,
        "show_calendar":true, "disabled_days":[...],
        "date_min":[2026,10,14], "date_max":[2027,10,13],
        "slots_data": {
          "2026-11-16": {"title":"Pon, stu 16",
                         "slots":[{"data":[[3,6,"2026-11-16 12:00:00",0]],
                                   "time_text":"12:00 pm",
                                   "status":"", "additional_text":"",
                                   "special_hour":false}, ...]}, ...}}

   Only days that have a schedule appear in ``slots_data``; a missing day means
   closed (no shift / holiday, e.g. 2026-12-24 and 2026-12-25 are absent).

   ``bookly_render_next_time`` (``last_slot=<json of the last data array>``)
   is the widget's "next page of slots" call; it is only issued when
   ``has_more_slots`` is true, which has never been observed in calendar mode,
   but it is handled defensively in ``_fetch_month``.

Traps (read before "fixing" this file)
-------------------------------------
* ``form_id`` from the page is **not** the session id and changes on every
  load; the session id comes from ``bookly_get_form_id``.  Using the page value
  as ``form_id`` yields ``{"success":false,"error":"session_error"}``.
* ``bookly_session_save`` must run **after** ``bookly_render_service``
  (otherwise ``session_error``), and its ``chain`` must contain **only**
  ``service_id`` and ``staff_ids[]``.  Adding ``units``, ``location_id``,
  ``number_of_persons`` or ``quantity`` — even as an empty string — makes the
  server answer ``{"chain":"Invalid number","success":false}``.  ``days[]``
  must list all seven week days (1 = Sunday … 7 = Saturday); the widget has
  them all checked by default, and a partial list silently filters the month
  down to those weekdays (``days[]=2`` returns Mondays only — a nastily
  plausible-looking wrong answer).
* ``date_from`` on ``bookly_session_save`` must be a **real date**.  Passing an
  empty string makes the following ``bookly_render_time`` answer with a bare
  JSON ``0`` instead of an object.  Any date returns the same slots — the
  staff schedule plus ``date_min``/``date_max`` decide what is offered — so a
  fixed placeholder is used and never refreshed per month.
* Slot times are **site-local (Europe/Zagreb)**: ``wp-json`` reports
  ``"timezone_string":"Europe/Zagreb"``.  The form has
  ``use_client_time_zone: 1`` but bookly.min.js only sends
  ``time_zone``/``time_zone_offset`` when the whole service step is skipped
  (``skip_steps.service``), which it is not here — so no conversion happens in
  the widget either and the datetimes are directly usable.
* ``status`` decides bookability: ``""`` = bookable, ``"booked"`` and
  ``"waiting-list"`` are rendered as ``disabled`` buttons.  Booked slots *are*
  returned — they must be filtered out, not treated as availability.
* Use the datetime inside ``data`` (``"YYYY-MM-DD HH:MM:SS"``, 24 h) rather
  than ``time_text`` ("12:00 pm", 12-hour) for the start time; ``time_text``
  is display-only and ambiguous for parsing.
* ``selected_date`` in the *response* is usually ``null``; the month that was
  actually rendered is in ``current_date``.  Do not validate on
  ``selected_date``.
* ``date_min``/``date_max`` are JS-style ``[year, month0, day]`` with a
  **0-based month**: ``[2026,10,14]`` is 2026-11-14.  (It is "tomorrow" — the
  site enforces a one-day booking lead time.)
* An out-of-range month (past ``date_max``) is *not* an error: it returns
  ``{"success":true,"has_slots":false,"slots_data":[]}`` — note the empty
  **array**, not an empty object — i.e. ``[]`` slots for every day.
* Quirk: ``bookly_render_time`` answers with a bare JSON integer when the
  saved chain is unusable; ``_post`` surfaces that as an ``AdapterError``
  rather than as "no availability".

Cost: 5 requests to set up a room (1 page GET + 4 POSTs), then **1 request per
room-month** — the caller asks for 31 days, i.e. 1-2 months per room.  The
month payload is cached on the client object, so 31 days x 3 rooms is ~21
requests in total.
"""
from __future__ import annotations

import html
import json
import re
import threading
import time
import unicodedata
import urllib.parse

if __package__ in (None, ""):                      # `python providers/bookly.py`
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fetch import fetch                            # noqa: E402
from providers.base import Adapter, AdapterError, Slot  # noqa: E402

AJAX_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://escapeart.hr",
}

# Bookly week-day ids: 1 = Sunday … 7 = Saturday.  The widget submits all of
# them (every checkbox in the service step is checked), and a partial list
# filters the returned days — see the module docstring.
ALL_WEEK_DAYS = ("1", "2", "3", "4", "5", "6", "7")

MAX_SLOT_PAGES = 6          # guard for the bookly_render_next_time loop
THROTTLE = 0.3              # seconds between consecutive AJAX calls

TIME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})")


def _json_var(text: str, pattern: str):
    """Parse the first ``<pattern> {...}`` blob out of a page.

    ``pattern`` is a regex tail such as ``BooklyL10n\\s*=`` (which deliberately
    does not match ``BooklyL10nGlobal =`` right above it) or
    ``window\\.bookly\\(``.
    """
    match = re.search(pattern, text)
    if not match:
        return None
    brace = text.find("{", match.end())
    if brace == -1:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(brace, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[brace:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _plain(text: str) -> str:
    """Diacritic-free lowercase alphanumeric form, for name matching."""
    text = (text or "").replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _slug(url: str) -> str:
    """Last meaningful path segment of a URL (``/drakuline-odaje/``)."""
    path = urllib.parse.urlparse(url or "").path
    parts = [p for p in path.split("/") if p]
    return parts[-1] if parts else ""


def _duration_minutes(text) -> int | None:
    """``"1 h 30 min"`` / ``"90 min"`` / ``"1:30"`` -> minutes."""
    label = str(text or "").strip().lower()
    if not label:
        return None
    m = re.fullmatch(r"(\d+):(\d{2})(?::\d{2})?", label)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    hours = re.search(r"(\d+)\s*(?:h|hour|sat)", label)
    mins = re.search(r"(\d+)\s*(?:min|minute)", label)
    if hours or mins:
        total = (int(hours.group(1)) * 60 if hours else 0) + (int(mins.group(1)) if mins else 0)
        return total or None
    m = re.search(r"(\d+)", label)
    return int(m.group(1)) if m else None


class BooklyRoom:
    """One Bookly form (one room page) — session setup + month slot cache."""

    def __init__(self, room: dict):
        self.room = room
        self.page_url = room.get("schedule_url") or room["url"]
        self._lock = threading.Lock()
        self._months: dict[str, dict[str, list[Slot]]] = {}
        self._last_call = 0.0
        self._prepared = False

        status, body = fetch(self.page_url, cache=False)
        if status != 200:
            raise AdapterError(f"{self.page_url} -> HTTP {status}")
        self.html = body

        l10n = _json_var(body, r"BooklyL10n\s*=") or {}
        self.csrf = l10n.get("csrf_token")
        self.ajax_url = l10n.get("ajaxurl") or urllib.parse.urljoin(
            self.page_url, "/wp-admin/admin-ajax.php")
        if not self.csrf:
            raise AdapterError(f"{self.page_url}: BooklyL10n.csrf_token not found")
        if not l10n.get("ajaxurl"):
            raise AdapterError(f"{self.page_url}: BooklyL10n.ajaxurl not found")

        cfg = _json_var(body, r"window\.bookly\(\s*") or {}
        container = cfg.get("form_id")
        form_data = cfg.get("form_data")
        if not container or form_data is None:
            raise AdapterError(f"{self.page_url}: window.bookly({...}) config not found")
        self.container_id = f"bookly-form-container-{container}"
        self.form_data = json.dumps(form_data)

        self.form_id: str | None = None
        self.staff_id: int | None = None
        self.service_id: int | None = None
        self.room_name: str | None = None
        self.duration_min: int | None = None

    # -- HTTP ---------------------------------------------------------------
    def _post(self, params: dict) -> dict:
        pairs = []
        for key, value in params.items():
            if isinstance(value, (list, tuple)):
                pairs.extend((key, str(v)) for v in value)
            else:
                pairs.append((key, "" if value is None else str(value)))
        pairs.append(("csrf_token", self.csrf or ""))

        wait = THROTTLE - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

        headers = dict(AJAX_HEADERS)
        headers["Referer"] = self.page_url
        try:
            status, text = fetch(self.ajax_url, method="POST",
                                 data=urllib.parse.urlencode(pairs).encode(),
                                 headers=headers, cache=False)
        except RuntimeError as exc:
            raise AdapterError(f"{self.page_url}: {exc}") from exc
        if status >= 500:
            raise AdapterError(f"{self.page_url}: {params['action']} -> HTTP {status}")
        if status not in (200, 400, 403):
            raise AdapterError(f"{self.page_url}: {params['action']} -> HTTP {status}")
        try:
            data = json.loads(text)
        except ValueError as exc:
            snippet = re.sub(r"\s+", " ", text)[:160]
            raise AdapterError(
                f"{self.page_url}: {params['action']} returned non-JSON: {snippet!r}"
            ) from exc
        if not isinstance(data, dict):
            raise AdapterError(
                f"{self.page_url}: {params['action']} returned {type(data).__name__} "
                f"{data!r} (session chain unusable)")
        if data.get("csrf_token"):
            self.csrf = data["csrf_token"]
        if not data.get("success"):
            raise AdapterError(
                f"{self.page_url}: {params['action']} failed: "
                f"{data.get('error') or data.get('chain') or data}")
        return data

    # -- setup --------------------------------------------------------------
    def _prepare(self) -> None:
        """get_form_id -> render_service -> resolve room -> save the chain."""
        data = self._post({
            "action": "bookly_get_form_id",
            "form_container_id": self.container_id,
            "form_id": "",
            "form_data": self.form_data,
        })
        form_id = str(data.get("form_id") or "")
        if not re.fullmatch(r"[0-9a-f]{16,64}", form_id):
            raise AdapterError(f"{self.page_url}: unexpected form_id {form_id!r}")
        self.form_id = form_id

        cfg = self._post({"action": "bookly_render_service", "form_id": self.form_id})
        services = cfg.get("services") or {}
        staff = cfg.get("staff") or {}
        if not services or not staff:
            raise AdapterError(f"{self.page_url}: render_service returned no services/staff")

        self.staff_id, self.room_name = self._pick_staff(staff)
        offered = (staff[str(self.staff_id)] or {}).get("services") or {}
        candidates = []
        for sid in offered:
            try:
                num = int(sid)
            except (TypeError, ValueError):
                continue
            if str(num) in services:
                candidates.append(num)
        if not candidates:
            raise AdapterError(
                f"{self.page_url}: room {self.room_name!r} offers no known service")
        # Availability does not depend on the price bracket (verified for every
        # service of every room); the lowest id keeps the pick deterministic.
        self.service_id = min(candidates)
        self.duration_min = _duration_minutes(
            (services.get(str(self.service_id)) or {}).get("duration"))

        # date_from must be a real date: an empty value makes
        # bookly_render_time answer with a bare `0`.  Any date yields the same
        # slots (the schedule and date_min/date_max decide what is offered).
        self._save_chain("2000-01-01")

    def _pick_staff(self, staff: dict) -> tuple[int, str]:
        """Map the catalog room onto a Bookly staff id (= room).

        ``data/rooms.json`` names one room "Grof Dracula" while Bookly calls the
        same room "Drakuline odaje", so matching goes through the page slug
        (``/drakuline-odaje/``), then the room name, both diacritic-folded.
        """
        wanted = [_plain(_slug(self.page_url)), _plain(self.room.get("room")),
                  _plain((self.room.get("id") or "").rsplit("/", 1)[-1])]
        wanted = [w for w in dict.fromkeys(wanted) if w]
        ordered = sorted(staff.items(), key=lambda kv: int(kv[0]))

        for mine in wanted:
            for sid, info in ordered:
                if _plain(str(info.get("name") or "")) == mine:
                    return int(sid), str(info.get("name") or "")

        for mine in wanted:                              # loose, unique-only match
            loose = [sid for sid, info in ordered
                     if mine and (mine in _plain(str(info.get("name") or ""))
                                  or _plain(str(info.get("name") or "")) in mine)]
            if len(loose) == 1:
                info = staff[loose[0]]
                return int(loose[0]), str(info.get("name") or "")

        if len(ordered) == 1:
            sid, info = ordered[0]
            return int(sid), str(info.get("name") or "")

        names = sorted(str(i.get("name")) for _, i in ordered)
        raise AdapterError(
            f"{self.page_url}: cannot match room {self.room.get('room')!r} "
            f"to a Bookly staff member (available: {names})")

    def _save_chain(self, date_from: str) -> None:
        """The service step's "Next" — must contain only service_id/staff_ids."""
        self._post({
            "action": "bookly_session_save",
            "form_id": self.form_id,
            "chain[0][service_id]": str(self.service_id),
            "chain[0][staff_ids][]": str(self.staff_id),
            "date_from": date_from,
            "days[]": list(ALL_WEEK_DAYS),
        })

    # -- slots --------------------------------------------------------------
    def _fetch_month(self, month: str) -> dict[str, list[Slot]]:
        """All days of ``YYYY-MM`` in one (usually) request."""
        data = self._post({
            "action": "bookly_render_time",
            "form_id": self.form_id,
            "selected_date": f"{month}-01",
        })
        days: dict[str, list[Slot]] = {}
        cursor, seen = self._collect(days, data, month)
        has_slots = bool(data.get("has_slots"))

        pages = 0
        while data.get("has_more_slots") and cursor and pages < MAX_SLOT_PAGES:
            data = self._post({
                "action": "bookly_render_next_time",
                "form_id": self.form_id,
                "last_slot": json.dumps(cursor, separators=(",", ":")),
            })
            if not data.get("has_slots"):
                break
            has_slots = True
            cursor, extra = self._collect(days, data, month)
            seen += extra
            pages += 1

        if has_slots and not seen:
            # Bookly said the month has slots but nothing was parseable: the
            # payload shape changed.  Never report that as "no availability".
            raise AdapterError(
                f"{self.page_url}: render_time reported slots for {month} but "
                f"none could be parsed (Bookly payload changed?)")
        return days

    def _collect(self, days: dict[str, list[Slot]], data: dict,
                 month: str) -> tuple[list | None, int]:
        """Merge one render_time payload into ``days``.

        Returns ``(cursor, entries_seen)`` where ``cursor`` is the raw ``data``
        array of the last slot — ``bookly_render_next_time`` wants ``last_slot``
        to be exactly the widget's button value, i.e. the JSON of that array.
        """
        slots_data = data.get("slots_data")
        if slots_data is None:
            raise AdapterError(f"{self.page_url}: render_time has no slots_data")
        if not isinstance(slots_data, dict):
            if not slots_data:
                # a month entirely outside date_min..date_max comes back as
                # "slots_data": [] (an empty *array*, not an object)
                return None, 0
            raise AdapterError(f"{self.page_url}: slots_data is not an object")

        seen = 0
        newest: tuple[str, str] | None = None
        cursor: list | None = None
        for day, info in slots_data.items():
            if not isinstance(day, str) or not day.startswith(month):
                continue
            found = days.setdefault(day, [])
            known = {s.time for s in found}
            for entry in (info or {}).get("slots") or []:
                if isinstance(entry, dict):
                    seen += 1
                slot = self._slot(entry)
                if slot is None:
                    continue
                if newest is None or (day, slot.time) > newest:
                    newest = (day, slot.time)
                    cursor = (entry or {}).get("data")
                if slot.time in known:
                    continue
                found.append(slot)
                known.add(slot.time)
            found.sort(key=lambda s: s.time)
            if not found:
                days.pop(day, None)
        return cursor, seen

    def _slot(self, entry: dict) -> Slot | None:
        if not isinstance(entry, dict):
            return None
        status = str(entry.get("status") or "").strip()
        if status:                       # "booked" / "waiting-list" -> disabled
            return None
        start = None
        for row in entry.get("data") or []:
            for cell in (row if isinstance(row, (list, tuple)) else [row]):
                m = TIME_RE.search(str(cell))
                if m:
                    start = m.group(2) + ":" + m.group(3)
                    break
            if start:
                break
        if not start:
            return None
        end = None
        if self.duration_min:
            total = int(start[:2]) * 60 + int(start[3:]) + self.duration_min
            if total < 24 * 60:
                end = f"{total // 60:02d}:{total % 60:02d}"
        note = html.unescape(str(entry.get("additional_text") or "")).strip()
        return Slot(time=start, end=end, note=note)

    def month(self, month: str) -> dict[str, list[Slot]]:
        """Cached ``{day: [Slot, ...]}`` for ``YYYY-MM``."""
        with self._lock:
            cached = self._months.get(month)
            if cached is not None:
                return cached
            if not self._prepared:
                self._prepare()
                self._prepared = True
            try:
                days = self._fetch_month(month)
            except AdapterError:
                # Bookly keeps the session in a transient keyed by form_id; if
                # it expired mid-run every call starts answering
                # "session_error".  Rebuild the session once, then give up.
                self._prepared = False
                self._prepare()
                self._prepared = True
                days = self._fetch_month(month)
            self._months[month] = days
            return days


class EscapeArtAdapter(Adapter):
    provider_id = "escapeart"
    provider_name = "Escape Art"

    def __init__(self) -> None:
        self._cache: dict[str, BooklyRoom] = {}
        self._lock = threading.Lock()

    def _client(self, room: dict) -> BooklyRoom:
        url = room.get("schedule_url") or room["url"]
        with self._lock:
            client = self._cache.get(url)
        if client is None:
            client = BooklyRoom(room)
            with self._lock:
                client = self._cache.setdefault(url, client)
        return client

    def slots(self, room: dict, day: str) -> list[Slot]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
            raise AdapterError(f"Escape Art: bad date {day!r}")
        found = self._client(room).month(day[:7]).get(day) or []
        return [Slot(time=s.time, end=s.end, note=s.note) for s in found]


ADAPTER = EscapeArtAdapter()


def _self_test() -> None:
    import datetime as _dt
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "data", "rooms.json"), encoding="utf-8") as fh:
        rooms = [r for r in json.load(fh) if r.get("provider_id") == "escapeart"]

    today = _dt.date.today()
    sample = [today + _dt.timedelta(days=n) for n in (1, 2, 6)]
    print(f"{ADAPTER.provider_name} — self test, today={today:%Y-%m-%d}\n")
    if not rooms:
        print("data/rooms.json has no provider_id 'escapeart' entries")
        return

    for room in rooms:
        print(f"{room['room']}  ({room['url']})")
        for when in sample:
            day = when.strftime("%Y-%m-%d")
            try:
                found = ADAPTER.slots(room, day)
            except AdapterError as exc:
                print(f"   {day} {when:%a}  ERROR: {exc}")
                continue
            stamp = ", ".join(s.time + (f"-{s.end}" if s.end else "") for s in found)
            print(f"   {day} {when:%a}  {len(found)} slot(s)  {stamp or '—'}")
        print()

    # 45-day strip: open days sit next to genuinely closed ones (the room pages
    # publish nothing for 24/25/31 Dec), so an empty day can be told apart from
    # a broken parser without leaving this script.
    span = 45
    dates = [today + _dt.timedelta(days=n) for n in range(span)]
    print(f"bookable slots per day, {dates[0]:%Y-%m-%d} .. {dates[-1]:%Y-%m-%d}")
    print(f"   {'room':<20} " + " ".join(f"{d.day:>2}" for d in dates))
    for room in rooms:
        counts = []
        for when in dates:
            try:
                counts.append(len(ADAPTER.slots(room, when.strftime("%Y-%m-%d"))))
            except AdapterError as exc:
                counts.append(None)
                print(f"   {room['room']}: {when} ERROR: {exc}")
        print(f"   {room['room']:<20} "
              + " ".join(" !" if c is None else f"{c:>2}" for c in counts))
        empty = [d for d, c in zip(dates, counts) if c == 0]
        if empty:
            print(f"   {'':<20} nothing bookable: "
                  + ", ".join(f"{d:%Y-%m-%d}" for d in empty))
    print(f"   {'':<20} column heads are day-of-month, span "
          f"{dates[0]:%b %d} - {dates[-1]:%b %d}")


if __name__ == "__main__":
    _self_test()
