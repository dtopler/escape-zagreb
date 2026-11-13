"""The Old Lock Up (Zagreb) — classic-ASP availability calendar.

Mechanism (verified 2026-11-13 against www.theoldlockup.com)
-----------------------------------------------------------

1. Room pages (``room-detail.asp?roomid=N`` and ``room-single_haunted.html``)
   each link ``booking-formb.asp?room_id=N``.  That numeric ``room_id`` is the
   only key needed, and it is also the ``story`` id used inside the calendar
   (1 Kidnapped, 2 Haunted Hotel, 3 The Crypt, 4 Morgue, 5 The Bailiff,
   6 Narcos, 7 Panic Room, 8 The Ritual, 9 The Watchers, 10 Circus,
   11 Missing).  It is derived from ``room["url"]`` (``...?roomid=N``); the
   haunted-hotel HTML page carries no id, so it maps to ``room_id=2``.

2. ``booking-formb.asp`` with **no** ``lang`` renders a ~12 kB "choose your
   language" page (two flag links) and *no* calendar.  With ``lang`` set it
   renders the complete availability calendar **server side** in one GET::

       GET https://www.theoldlockup.com/booking-formb.asp
           ?lang=EN&room_id=4&daysahead=90

   No AJAX, no JSON, no iframe, no third-party booking host, no cookie
   session, no nonce — the whole answer is in the HTML, so one request per
   room covers the entire booking horizon (91 dates: today .. today+90).

   Query parameters that actually matter:
     ``lang=EN|HR``   required (see above).
     ``room_id=N``    room filter; the ``story=N`` inside the page equals it
                      and the page then contains that room's slots only.
     ``daysahead=90`` the page's own radio (14/30/60/90).  90 is the ceiling:
                      ``daysahead=365`` makes the ASP return **HTTP 500**
                      (a partial body still renders) — never exceed 90.
     ``show_available=1`` hides taken slots.  Deliberately NOT sent: the
                      disabled flags are exactly what we need to read.
   ``date`` / ``month`` / ``startdate`` are ignored by the server (verified),
   the window always starts today.

3. Markup per day (a "Booked" plugin look-alike)::

       <td colspan="7" data-date="2026-12-1" class="active">Tuesday,...</td>
       ...
       <div class="timeslot bookedClearFix has-title">
         <span class="timeslot-range">
           <i class="booked-icon booked-icon-clock"></i>Morgue&nbsp;10:30</span>
         <button class="new-appt button" [disabled]
                 onclick="window.location.href='booking-page.asp?ts=330&story=4
                          &ds=2026-11-14&lang=EN&lastmin_id=54875';">
           <span class="button-timeslot">10:30</span>
           <span class="button-text">Book Room</span>
         </button>
       </div>

   * A button **without** ``disabled`` is bookable; with ``disabled`` (plus a
     red ``background:#F00``) the slot is taken or the day is closed.
     Cross-checked: ``booking-page.asp?ts=…&story=…&ds=…`` returns the bare
     12 kB shell with no ``<form id="bookingForm">`` for a disabled slot, but
     the ~20 kB booking form for a free one — so ``disabled`` really means
     "not bookable", not just a styling hint.
   * ``data-date`` (and the ``ds`` parameter) are **not zero-padded**
     (``2026-12-1``, ``2027-2-9``), so they must be normalised before being
     compared with the ISO ``day`` argument.
   * A day with no ``<div class="timeslot">`` is closed → ``[]``.

4. Days outside today .. today+90 are not published at all (the window is
   server-controlled) and return ``[]``.

No end time is published anywhere in the calendar, so ``Slot.end`` stays
``None``.

Session flow: none — a plain GET with query parameters is enough.  With
``cache=False`` every call goes to the live site; the parsed calendar is kept
per room for ``CACHE_TTL`` seconds so a room x day sweep costs one request per
room instead of one per day.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time

try:
    from fetch import fetch
    from providers.base import Adapter, AdapterError, Slot, parse_hhmm
except ModuleNotFoundError:  # script mode: the project root is not on sys.path
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from fetch import fetch
    from providers.base import Adapter, AdapterError, Slot, parse_hhmm

SITE = "https://www.theoldlockup.com/"
CALENDAR_PATH = "booking-formb.asp"
DAYS_AHEAD = 90            # 90 is the maximum the ASP page accepts
POLITE_DELAY = 0.4         # seconds between room calendar fetches
CACHE_TTL = 600.0          # a fetched calendar is reused for 10 minutes

_DATE_RE = re.compile(r'data-date="(\d{4})-(\d{1,2})-(\d{1,2})"')
_TIMESLOT_RE = re.compile(
    r'<div\b[^>]*\bclass="[^"]*\btimeslot\b[^"]*"[^>]*>(.*?)</div>', re.S | re.I)
_BUTTON_RE = re.compile(r"<button\b[^>]*>", re.I)
_DISABLED_RE = re.compile(r"(?<![\w-])disabled(?![\w-])", re.I)
_TIME_SPAN_RE = re.compile(r'class="button-timeslot"[^>]*>\s*(\d{1,2}[:.]\d{2})', re.I)
_RANGE_RE = re.compile(r'class="timeslot-range"[^>]*>(.*?)</span>', re.S | re.I)


def _iso(year: str, month: str, dom: str) -> str:
    return f"{int(year):04d}-{int(month):02d}-{int(dom):02d}"


def _room_id(room: dict) -> int | None:
    """Numeric ``room_id`` for a rooms.json dict, or None if not deducible."""
    for key in ("url", "id", "schedule_url"):
        blob = str(room.get(key) or "")
        m = re.search(r"room_?id[=_\-](\d+)", blob, re.I)
        if m:
            return int(m.group(1))
        if re.search(r"haunted", blob, re.I):
            # room-single_haunted.html -> booking-formb.asp?room_id=2
            return 2
    return None


def _room_id_from_page(room: dict) -> int | None:
    """Last-resort fallback: read the ``Book Now!`` link off the room page."""
    for key in ("url", "schedule_url"):
        url = room.get(key)
        if not url:
            continue
        status, body = fetch(url, cache=False)
        if status != 200:
            continue
        ids = re.findall(r"booking-formb\.asp\?[^\"'<>]*room_id=(\d+)", body)
        if ids:
            return int(ids[-1])
    return None


def _check_day(day: str) -> str:
    """Normalise the ``YYYY-MM-DD`` argument (tolerates unpadded/padded input)."""
    m = re.match(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})", str(day or ""))
    if not m:
        raise AdapterError(f"lockup: bad day {day!r}")
    return _iso(m.group(1), m.group(2), m.group(3))


def _slot_from_block(block: str) -> Slot | None:
    """One ``<div class="timeslot">`` -> Slot, or None when not bookable."""
    tag = _BUTTON_RE.search(block)
    if tag is None or _DISABLED_RE.search(tag.group(0)):
        return None
    m = _TIME_SPAN_RE.search(block)
    start = parse_hhmm(m.group(1)) if m else None
    if start is None:
        label = _RANGE_RE.search(block)
        if label:
            start = parse_hhmm(re.sub(r"<[^>]+>", " ", label.group(1)))
    if start is None:
        return None
    return Slot(time=start)


def _parse_calendar(page: str) -> dict[str, list[Slot]]:
    """Split the calendar into ``{"YYYY-MM-DD": [Slot, ...]}``."""
    raw: dict[str, dict[str, Slot]] = {}
    marks = list(_DATE_RE.finditer(page))
    for i, mark in enumerate(marks):
        day = _iso(mark.group(1), mark.group(2), mark.group(3))
        end = marks[i + 1].start() if i + 1 < len(marks) else len(page)
        chunk = page[mark.end():end]
        bucket = raw.setdefault(day, {})
        for block in _TIMESLOT_RE.finditer(chunk):
            slot = _slot_from_block(block.group(1))
            if slot is not None:
                bucket[slot.time] = slot
    return {day: [slots[t] for t in sorted(slots)] for day, slots in raw.items()}


class _RoomCalendar:
    """Availability calendar for one ``room_id``, cached for ``CACHE_TTL``."""

    def __init__(self, room_id: int, room_name: str = "") -> None:
        self.room_id = room_id
        self.room_name = room_name
        self._days: dict[str, list[Slot]] = {}
        self._stamp = 0.0
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        return (f"{SITE}{CALENDAR_PATH}?lang=EN&room_id={self.room_id}"
                f"&daysahead={DAYS_AHEAD}")

    def _fresh(self) -> bool:
        return bool(self._days) and (time.monotonic() - self._stamp) < CACHE_TTL

    def days(self) -> dict[str, list[Slot]]:
        with self._lock:
            if not self._fresh():
                self._days = _parse_calendar(self._download())
                self._stamp = time.monotonic()
            return self._days

    def _download(self) -> str:
        status, body = fetch(self.url, cache=False,
                             headers={"Referer": f"{SITE}index.asp"})
        if status != 200:
            raise AdapterError(
                f"lockup: room {self.room_id} calendar -> HTTP {status}")
        if not _DATE_RE.search(body) or "timeslot" not in body:
            raise AdapterError(
                f"lockup: room {self.room_id} calendar markup changed "
                f"(no dates/timeslots in {len(body)} bytes)")
        time.sleep(POLITE_DELAY)
        return body

    def slots(self, day: str) -> list[Slot]:
        return list(self.days().get(day, ()))


class LockUpAdapter(Adapter):
    provider_id = "lockup"
    provider_name = "The Old Lock Up"

    def __init__(self) -> None:
        self._cache: dict[int, _RoomCalendar] = {}
        self._lock = threading.Lock()

    def _calendar(self, room: dict) -> _RoomCalendar:
        room_id = _room_id(room) or _room_id_from_page(room)
        if room_id is None:
            raise AdapterError(
                f"lockup: cannot derive room_id from {room.get('url')!r}")
        with self._lock:
            cal = self._cache.get(room_id)
        if cal is None:
            cal = _RoomCalendar(room_id, str(room.get("room") or ""))
            with self._lock:
                self._cache.setdefault(room_id, cal)
                cal = self._cache[room_id]
        return cal

    def slots(self, room: dict, day: str) -> list[Slot]:
        return self._calendar(room).slots(_check_day(day))


ADAPTER = LockUpAdapter()


if __name__ == "__main__":
    import datetime as dt
    import json

    sys.stdout.reconfigure(encoding="utf-8")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "data", "rooms.json"), encoding="utf-8") as fh:
        all_rooms = [r for r in json.load(fh) if r.get("provider_id") == "lockup"]

    wanted = ["Mrtvačnica", "Ukleti hotel", "Kripta"]
    picks = [r for name in wanted for r in all_rooms if r["room"] == name]
    for room in all_rooms:
        if len(picks) >= 3:
            break
        if room not in picks:
            picks.append(room)
    picks = picks[:3]

    today = dt.date.today()
    days = [(today + dt.timedelta(days=n)).isoformat() for n in (1, 2, 3)]
    print(f"machine today = {today.isoformat()}  adapter = {ADAPTER!r}")
    print(f"rooms in data/rooms.json for provider 'lockup': {len(all_rooms)}")
    for room in picks:
        rid = _room_id(room)
        print(f"\n{room['room']}  [{room['id']}]  room_id={rid}")
        print(f"  {SITE}{CALENDAR_PATH}?lang=EN&room_id={rid}&daysahead={DAYS_AHEAD}")
        for day in days:
            try:
                slots = ADAPTER.slots(room, day)
            except AdapterError as exc:
                print(f"  {day}: AdapterError: {exc}")
                continue
            if slots:
                shown = ", ".join(s.time for s in slots)
            else:
                shown = "(closed / nothing available)"
            print(f"  {day}: {len(slots)} slot(s) {shown}")
