"""DOP Booking System PRO 6.2.3 (``dopbsp``) adapter — Escape Room Arena Zagreb.

Reverse-engineered from the plugin files the site actually serves
(``assets/js/jquery.dop.frontend.BSPCalendar.js`` and the
``application/assets/js/calendars/*`` modules, ``?ver=6.2.3``).

Endpoints and parameters
========================

**1. Calendar bootstrap (room page, HTML).**
Every room page embeds exactly one calendar::

    DOT.calendars[4] = jQuery("#DOPBSPCalendar4").DOPBSPCalendar({ ...huge JSON... });

The JSON object carries everything needed:

``ID``
    The *calendar id* used by every AJAX call (the index in ``DOT.calendars[n]``
    is just the DOM order — they are not the same number).
``calendar.data``
    Settings: ``bookingStop`` (minutes of lead time, here 180), ``maxYear``
    (here 2026), ``dateType``, ``language``, ``minimumNoAvailable`` …
``days.data.available``
    Seven booleans, index 0 = Sunday … 6 = Saturday.  A ``false`` entry makes
    *every* date on that weekday unavailable — ``calendar-day.js`` →
    ``calendar_day.get()`` returns the plugin's ``unavailable()`` day object and
    ``methods_hours.display()`` stops with "no services".
``default_schedule``
    The fallback day object used for dates that have no explicit row: its
    ``hours`` maps ``"HH:MM"`` → ``{status, available, price, promo, bind, …}``
    and ``hours_definitions`` lists the hour keys.
``hours.data.definitions``
    The calendar-wide hour list (``10:00 … 22:00`` / ``10:30 … 22:30`` here).

The empty calendar wrapper also carries the AJAX URL as an anchor::

    <div class="DOPBSPCalendar-wrapper" id="DOPBSPCalendar4">
        <a href="https://www.escape-arena.com/wp-admin/admin-ajax.php"></a>

``DOPPrototypes.acaoBuster($('a', Container).attr('href'))`` only rewrites that
URL to be same-origin — i.e. plain ``/wp-admin/admin-ajax.php``.

**2. Nonce flow: there is none.**  DOPBSP 6.2.3 guards its public actions with
``wp_ajax_nopriv_*`` plus a single static flag:

    POST {ajax_url}   (application/x-www-form-urlencoded, X-Requested-With)
    action=dopbsp_calendar_schedule_get
    dopbsp_frontend_ajax_request=true
    id=<calendar id>            # NOTE: "id", not "calendar_id"
    year=<YYYY>
    firstYear="false"           # the widget sends bare quotes: '"'+bool+'"'

No ``nonce``, ``_wpnonce`` or ``security`` field appears anywhere in the
shipped JS or in the page HTML, and the call works without cookies — so the
bootstrap/config can be cached for the whole run.

Response: a JSON object mapping ``"YYYY-MM-DD"`` to **a JSON string** (double
encoded!) describing that day::

    {"2026-01-11": "{\"available\":\"\",\"status\":\"unavailable\",
                    \"hours_definitions\":[{\"value\":\"10:00\"}],
                    \"hours\":{\"10:00\":{\"status\":\"unavailable\",
                                        \"available\":0,\"price\":0}}}"}

An HTTP 200 with an **empty body** is legal: "this calendar has no explicit day
rows for that year", in which case the widget falls back to
``default_schedule``.  (Calendars 2 and 4 on this site always answer empty.)

A second public action exists — ``action=pbs_user_calendars_data`` with
``calendar_id=<id>`` → ``{"availability":[]}`` — but it only carries rate
rules, so this adapter never calls it.

Day resolution (what DOPBSP offers for one date)
================================================

Mirrors ``calendar-day.js → calendar_day.get()`` and
``methods_hours.display()``::

    weekday = date.getDay()                       # 0 = Sunday
    if not days.available[weekday]:                      -> no hours (closed)
    day = schedule_rows.get(date) or default_schedule
    if int(day["available"] or 0) <= 0:                   -> no hours (closed)
    hours = day["hours_definitions"] or default["hours_definitions"]
    for hh in hours:
        row = day["hours"].get(hh) or default["hours"].get(hh)
        bookable <=> row["status"] in {"available", "special"}
                     and int(row["available"] or 0) > 0

``status`` values: ``available`` (bookable), ``special`` (bookable, promo),
``booked`` (sold out), ``unavailable`` (closed by the owner) and ``none`` (no
schedule at all — the ``{"00:00": … "none"}`` stub DOPBSP emits for a calendar
that has no default availability configured).  The ``available`` counter is the
remaining capacity, so an ``available`` hour with ``0`` left is not bookable —
same rule the plugin's own server-side availability model applies.

Traps
=====

* ``id`` vs ``calendar_id`` — the schedule action takes ``id``; sending
  ``calendar_id`` (or nothing) still returns HTTP 200 with an empty body, which
  is indistinguishable from "no schedule rows".  Do not "fix" that by guessing.
* Values are double-encoded: ``json.loads`` the outer object, then each value.
* The schedule is fetched **per year**; ``year`` above ``maxYear`` yields ``""``.
* Calendar ids are page-specific: ``the-office-of-john-monroe`` → 1,
  ``mind-crime-2`` → 2, ``lev-pasted`` → 3, ``amazing-heist`` → 4,
  ``secret-agent-room`` → 5.  The booking page mixes all of them (plus 14), so
  the id must always come from the room page's own bootstrap.
* Only start hours are published (``hours_definitions``); the plugin exposes no
  end time, so ``Slot.end`` stays ``None``.  Slots are 2 h apart by config.
* The widget hides hours that fall before *now + bookingStop* on the current
  day ("Please call"), so those are skipped for today only.

Live state of the site (checked 2026-11-13)
===========================================

All six calendars answer ``days.data.available = [false,false,false,false,
false,false,false]`` (the plugin default is ``true,true,true,true,true,true,
true``) — no weekday is bookable — and the booking page tells visitors to book
by phone (+385 1 6024 470) or e-mail instead.  ``default_schedule`` is
populated for calendars 1/3/5/14 while calendars 2 and 4 additionally return an
empty yearly schedule.  So DOPBSP's own answer for every Arena room/date right
now is ``[]`` (closed) — that is not a parse failure.  ``diagnostics()`` and
``_preview_hours()`` show what the same schedule would offer if the weekdays
were enabled; if the site reopens and the mask turns out to be stale, set
``ADAPTER.respect_weekday_mask = False`` to ignore it.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

# ``python providers/dopbsp.py`` puts providers/ on sys.path; the project root
# (fetch.py, providers.base) must be importable too.  Import-time only, no
# network/config access.
if __package__ in (None, ""):  # pragma: no cover - script mode
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fetch import fetch
from providers.base import Adapter, AdapterError, Slot, parse_hhmm

DEFAULT_AJAX_URL = "https://www.escape-arena.com/wp-admin/admin-ajax.php"

BOOTSTRAP_RE = re.compile(
    r"DOT\.calendars\[\d+\]\s*=\s*jQuery\(\s*[\"']#(DOPBSPCalendar\d+)[\"']\s*\)"
    r"\s*\.DOPBSPCalendar\(\s*\{"
)

BOOKABLE_STATUSES = ("available", "special")
WEEKDAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _as_int(value, default: int = 0) -> int:
    """``int()`` that tolerates DOPBSP's ``""``/``None``/``"3"`` values."""
    parsed = _as_opt_int(value)
    return default if parsed is None else parsed


def _as_opt_int(value) -> int | None:
    """``int()`` or ``None`` when the value is empty/absent/unparseable."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _json_object(text: str, start: int) -> str | None:
    """Return the ``{...}`` blob starting at ``start`` (string/escape aware)."""
    if start < 0 or start >= len(text) or text[start] != "{":
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
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
                return text[start:i + 1]
    return None


def _weekday_index(day: str) -> int:
    """0 = Sunday … 6 = Saturday, like JavaScript's ``Date.getDay()``."""
    return (datetime.strptime(day, "%Y-%m-%d").weekday() + 1) % 7


def _zagreb_now() -> datetime:
    """Current Europe/Zagreb time (tzdata via zoneinfo, manual DST fallback)."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Europe/Zagreb"))
    except Exception:  # pragma: no cover - only when tzdata is unavailable
        utc = datetime.now(timezone.utc).replace(tzinfo=None)

        def last_sunday(year: int, month: int) -> datetime:
            last = datetime(year, month + 1, 1) - timedelta(days=1)
            return last - timedelta(days=(last.weekday() + 1) % 7)

        dst = last_sunday(utc.year, 3).replace(hour=1) <= utc < last_sunday(
            utc.year, 10).replace(hour=1)
        offset = timezone(timedelta(hours=2 if dst else 1))
        return utc.replace(tzinfo=timezone.utc).astimezone(offset)


def _request(*args, **kwargs) -> tuple[int, str]:
    """``fetch()`` with any transport failure normalised to AdapterError."""
    try:
        return fetch(*args, **kwargs)
    except AdapterError:
        raise
    except Exception as exc:  # noqa: BLE001 - fetch raises RuntimeError etc.
        target = args[0] if args else kwargs.get("url")
        raise AdapterError(f"{target}: request failed ({exc})") from exc


# --------------------------------------------------------------------------- #
# one room page / one DOPBSP calendar
# --------------------------------------------------------------------------- #
class DOPBSPRoom:
    """The DOPBSP calendar embedded in a single Arena room page."""

    def __init__(self, page_url: str):
        self.page_url = page_url
        self.calendar_id = 0
        self.calendar_dom_id = ""
        self.ajax_url = DEFAULT_AJAX_URL
        self.data: dict = {}
        self.settings: dict = {}
        self.days_settings: dict = {}
        self.default_schedule: dict = {}
        self.hours_data: dict = {}
        self._rows: dict[int, dict[str, dict]] = {}
        self._lock = threading.Lock()
        self._load()

    # -- setup ------------------------------------------------------------- #
    def _load(self) -> None:
        status, body = _request(self.page_url, cache=False)
        if status >= 500:
            raise AdapterError(f"{self.page_url}: HTTP {status}")
        if status != 200:
            raise AdapterError(f"{self.page_url}: HTTP {status}")
        if not body:
            raise AdapterError(f"{self.page_url}: empty response body")

        match = BOOTSTRAP_RE.search(body)
        if not match:
            raise AdapterError(
                f"{self.page_url}: DOPBSP calendar bootstrap not found "
                "(markup changed?)")
        self.calendar_dom_id = match.group(1)
        blob = _json_object(body, match.end() - 1)
        if not blob:
            raise AdapterError(
                f"{self.page_url}: DOPBSP calendar config is truncated")
        try:
            data = json.loads(blob)
        except json.JSONDecodeError as exc:
            raise AdapterError(
                f"{self.page_url}: DOPBSP calendar config is not JSON ({exc})"
            ) from exc
        if not isinstance(data, dict) or "ID" not in data:
            raise AdapterError(
                f"{self.page_url}: unexpected DOPBSP calendar config")

        self.data = data
        self.calendar_id = _as_int(data.get("ID"))
        if self.calendar_id <= 0:
            raise AdapterError(
                f"{self.page_url}: DOPBSP calendar id missing from config")
        self.ajax_url = self._ajax_url(body)
        self.settings = (data.get("calendar") or {}).get("data") or {}
        self.days_settings = (data.get("days") or {}).get("data") or {}
        self.default_schedule = data.get("default_schedule") or {}
        self.hours_data = (data.get("hours") or {}).get("data") or {}

    def _ajax_url(self, body: str) -> str:
        pos = body.find(f'id="{self.calendar_dom_id}"')
        if pos != -1:
            anchor = re.search(r"<a\s[^>]*href=[\"']([^\"']+)[\"']",
                               body[pos:pos + 4000], re.I)
            if anchor:
                url = html.unescape(anchor.group(1)).strip()
                if url:
                    return urllib.parse.urljoin(self.page_url, url)
        return DEFAULT_AJAX_URL

    # -- schedule ---------------------------------------------------------- #
    def year_rows(self, year: int) -> dict[str, dict]:
        """``{"YYYY-MM-DD": {day object}}`` for one year (cached, one request)."""
        with self._lock:
            rows = self._rows.get(year)
            if rows is None:
                # Held while fetching on purpose: the aggregator calls slots()
                # from several threads, and one request per room-year is enough.
                rows = self._fetch_year(year)
                self._rows[year] = rows
            return rows

    def _fetch_year(self, year: int) -> dict[str, dict]:
        body = urllib.parse.urlencode({
            "action": "dopbsp_calendar_schedule_get",
            "dopbsp_frontend_ajax_request": "true",
            "id": str(self.calendar_id),
            "year": str(year),
            "firstYear": '"false"',
        }).encode()
        status, text = _request(
            self.ajax_url, method="POST", data=body, cache=False,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": self.page_url,
                "Origin": "https://www.escape-arena.com",
            },
        )
        time.sleep(0.3)  # be gentle with admin-ajax.php
        if status >= 500:
            raise AdapterError(
                f"{self.page_url}: schedule HTTP {status} (calendar "
                f"{self.calendar_id}, {year})")
        if status != 200:
            raise AdapterError(
                f"{self.page_url}: schedule HTTP {status} (calendar "
                f"{self.calendar_id}, {year})")

        raw = text.strip()
        if not raw:
            return {}  # legal: no explicit day rows for this year
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AdapterError(
                f"{self.page_url}: schedule response is not JSON "
                f"(calendar {self.calendar_id}, {year}): {raw[:120]!r}"
            ) from exc
        if not isinstance(payload, dict):
            raise AdapterError(
                f"{self.page_url}: unexpected schedule payload "
                f"(calendar {self.calendar_id}, {year})")

        rows: dict[str, dict] = {}
        for day, value in payload.items():
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    continue
            if isinstance(value, dict) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                rows[day] = value
        return rows

    # -- day resolution ---------------------------------------------------- #
    def weekday_allowed(self, day: str) -> bool:
        mask = self.days_settings.get("available")
        if not isinstance(mask, list) or len(mask) < 7:
            return True  # no usable mask -> do not gate
        value = mask[_weekday_index(day)]
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes")
        return bool(value)

    def weekday_mask(self) -> str:
        mask = self.days_settings.get("available")
        if not isinstance(mask, list) or not mask:
            return "?"
        out = []
        for value in mask:
            if isinstance(value, str):
                out.append("1" if value.strip().lower() in ("1", "true", "yes")
                           else "0")
            else:
                out.append("1" if bool(value) else "0")
        return "".join(out)

    def _day_object(self, day: str) -> dict:
        row = self.year_rows(int(day[:4])).get(day)
        if row is not None:
            return row
        return self.default_schedule or {}

    def _hours(self, day: str, respect_mask: bool = True) -> list[tuple[str, dict]]:
        """Resolved ``[(HH:MM, hour dict)]`` that DOPBSP would offer."""
        if respect_mask and not self.weekday_allowed(day):
            return []
        default = self.default_schedule or {}
        day_obj = self._day_object(day)
        # Day-level capacity gate.  DOPBSP writes "" for "not applicable", so
        # only an explicit numeric 0..n-1 closes the day (an hour with 0 left
        # is caught per hour below).
        day_available = _as_opt_int(day_obj.get("available"))
        if day_available is not None and day_available <= 0:
            return []

        definitions = (day_obj.get("hours_definitions")
                       or default.get("hours_definitions")
                       or self.hours_data.get("definitions")
                       or [])
        day_hours = day_obj.get("hours") or {}
        default_hours = default.get("hours") or {}

        out: list[tuple[str, dict]] = []
        seen: set[str] = set()
        for definition in definitions:
            if not isinstance(definition, dict):
                continue
            hhmm = parse_hhmm(str(definition.get("value") or ""))
            if not hhmm or hhmm in seen:
                continue
            hour = day_hours.get(hhmm) or default_hours.get(hhmm)
            if not isinstance(hour, dict):
                continue
            if hour.get("status") not in BOOKABLE_STATUSES:
                continue
            if _as_int(hour.get("available")) <= 0:
                continue
            seen.add(hhmm)
            out.append((hhmm, hour))
        out.sort(key=lambda item: item[0])
        return out

    # -- public ------------------------------------------------------------ #
    def slots(self, day: str, respect_mask: bool = True) -> list[Slot]:
        try:
            target = datetime.strptime(str(day), "%Y-%m-%d")
        except ValueError as exc:
            raise AdapterError(f"bad day {day!r}, expected YYYY-MM-DD") from exc
        if target.date() < _zagreb_now().date():
            return []  # past dates are never bookable

        max_year = _as_int(self.settings.get("maxYear"))
        if max_year and target.year > max_year:
            return []

        earliest = ""
        now = _zagreb_now()
        if target.date() == now.date():
            stop = _as_int(self.settings.get("bookingStop"))
            earliest = (now + timedelta(minutes=stop)).strftime("%H:%M")

        out: list[Slot] = []
        for hhmm, hour in self._hours(day, respect_mask=respect_mask):
            if earliest and hhmm < earliest:
                continue  # too close to now; the widget says "Please call"
            note = str(hour.get("info") or hour.get("notes") or "").strip()
            out.append(Slot(time=hhmm, end=None, note=note))
        return out

    def diagnostics(self, day: str) -> dict:
        """Everything the parser saw for ``day`` (for debugging / self-test)."""
        row = self.year_rows(int(day[:4])).get(day)
        preview = [hhmm for hhmm, _ in self._hours(day, respect_mask=False)]
        return {
            "calendar_id": self.calendar_id,
            "ajax_url": self.ajax_url,
            "weekday": WEEKDAY_NAMES[_weekday_index(day)],
            "weekday_mask": self.weekday_mask(),
            "weekday_allowed": self.weekday_allowed(day),
            "schedule_rows": len(self.year_rows(int(day[:4]))),
            "day_row": row is not None,
            "day_status": (row or {}).get("status"),
            "day_available": (row or {}).get("available"),
            "default_status": (self.default_schedule or {}).get("status"),
            "default_hours": sorted((self.default_schedule or {}).get("hours") or {}),
            "hours_if_weekdays_enabled": preview,
        }


# --------------------------------------------------------------------------- #
# adapter
# --------------------------------------------------------------------------- #
class DOPBSPAdapter(Adapter):
    """Escape Room Arena Zagreb (DOP Booking System PRO 6.2.3)."""

    provider_id = "arena"
    provider_name = "Escape Room Arena"

    #: Honour ``days.data.available`` exactly like the plugin's calendar does.
    #: Set to ``False`` only if the site is demonstrably bookable while the mask
    #: still says "no weekday available" (stale config).
    respect_weekday_mask = True

    def __init__(self) -> None:
        self._cache: dict[str, DOPBSPRoom] = {}
        self._lock = threading.Lock()

    def _client(self, room: dict) -> DOPBSPRoom:
        url = (room.get("url") or room.get("schedule_url") or "").strip()
        if not url:
            raise AdapterError(f"room {room.get('id')!r}: no url in rooms.json")
        with self._lock:
            client = self._cache.get(url)
        if client is None:
            client = DOPBSPRoom(url)
            with self._lock:
                self._cache[url] = client
        return client

    def slots(self, room: dict, day: str) -> list[Slot]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(day)):
            raise AdapterError(f"bad day {day!r}, expected YYYY-MM-DD")
        return self._client(room).slots(
            day, respect_mask=self.respect_weekday_mask)

    def diagnostics(self, room: dict, day: str) -> dict:
        """Debug view of one date; used by the self-test."""
        return self._client(room).diagnostics(day)


ADAPTER = DOPBSPAdapter()


# --------------------------------------------------------------------------- #
# self-test
# --------------------------------------------------------------------------- #
def _demo() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "data", "rooms.json"), encoding="utf-8") as fh:
        rooms = [r for r in json.load(fh)
                 if r.get("provider_id") == ADAPTER.provider_id]
    if not rooms:
        print("no arena rooms in data/rooms.json")
        return 1

    today = _zagreb_now().date()
    days = [str(today + timedelta(days=offset)) for offset in (1, 2, 8)]
    print(f"provider : {ADAPTER.provider_name} ({ADAPTER.provider_id})")
    print(f"today    : {today} (Europe/Zagreb)   room pages: {len(rooms)}")
    print(f"dates    : {', '.join(days)}")

    for room in rooms:
        print(f"\n=== {room['room']} ===")
        print(f"    url: {room['url']}")
        try:
            diag = ADAPTER.diagnostics(room, days[0])
        except AdapterError as exc:
            print(f"    setup failed: {exc}")
            continue
        print("    calendar {id} | weekday mask {mask} | schedule rows {year}: "
              "{rows} | default hours: {hours}".format(
                  id=diag["calendar_id"], mask=diag["weekday_mask"],
                  year=days[0][:4], rows=diag["schedule_rows"],
                  hours=", ".join(diag["default_hours"]) or "-"))
        if "1" not in diag["weekday_mask"]:
            print("    ! DOPBSP reports every weekday disabled for this calendar, "
                  "so its widget offers no bookable day (online booking closed; "
                  "the site asks visitors to call/e-mail).")
        for day in days:
            try:
                found = ADAPTER.slots(room, day)
            except AdapterError as exc:
                print(f"    {day}: AdapterError -> {exc}")
                continue
            label = ", ".join(s.time for s in found) if found else "(none)"
            print(f"    {day}: {len(found)} slot(s)  {label}")
        print(f"    diagnostics[{days[0]}]: {json.dumps(diag, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo())
