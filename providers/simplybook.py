"""SimplyBook.me adapter — Enigmarium Zagreb (Grička vještica / Femme fatale / Unlock Zagreb).

Enigmarium publishes no schedule of its own: each room page embeds a
SimplyBook.me *v2* widget, so the bookable times come straight out of
SimplyBook's public booking app.

How the widget works (verified against the live site, 2026-11-13)
----------------------------------------------------------------
``https://enigmarium.hr/<room>/`` loads ``//widget.simplybook.it/v2/widget/widget.js``
and then runs::

    var widget = new SimplybookWidget({
        "widget_type": "iframe",
        "url": "https://zagrebenigmarium.simplybook.it",
        "theme": "default",
        "theme_settings": {...},
        "timeline": "modern", "datepicker": "top_calendar",
        "app_config": {"allow_switch_to_ada": 0,
                       "predefined": {"provider": "<room id>", "service": "1"}}
    });

``widget.js`` only builds an iframe pointing at::

    https://zagrebenigmarium.simplybook.it/v2/?widget-type=iframe&theme=default
        &timeline=modern&datepicker=top_calendar&host_url=<embedding page>

so the iframe URL does **not** identify the room — ``app_config`` is handed to
the iframe with ``postMessage()`` after it loads.  The per-room ids therefore
live in the *embedding page*, which is why this adapter reads the room page
first and caches the result (one page fetch per room per process).

Those ids are SimplyBook's own ``provider_id`` (one per room) and
``service_id`` (all three rooms share service 1).  Cross-checked against
``GET /v2/provider/``: ``1`` = "Tajanstveni slučaj zagrebačke vještice",
``2`` = "Femme Fatale", ``3`` = "Unlock Zagreb" (and ``7`` = "Laboratorij crne
kraljice"), each with ``"services": [1]``; and ``GET /v2/service/`` where
service ``1`` is "Enigmarium Zagreb", 60 min, 75 EUR.

The one endpoint this adapter calls
-----------------------------------
::

    GET https://zagrebenigmarium.simplybook.it/v2/booking/time-slots/
        ?from=YYYY-MM-DD&to=YYYY-MM-DD&provider=<id>&service=<id>&count=1

Found in the booking-app bundle ``/v2/js/user_public/init.min.js``:

* ``define("collection/TimeSlotCollection", ... {url: "/booking/time-slots/"})``
  built on ``lib/collection/BaseCollection`` whose ``baseUrl`` is ``"/v2"``;
* the query string is assembled by
  ``controller/step/datetime/BaseDateTimeStep._fetchTimeCollection``, which sends
  ``from``/``to`` (the same day), ``provider``, ``service``, ``count`` plus
  optional ``location``/``category``/``products``/``booking_id``.

The response for one day is a flat JSON array (abridged)::

    [{"id": "2026-11-16 12:30:00", "date": "2026-11-16", "time": "12:30:00",
      "type": "free", "slots_count": 4, "client_date": "2026-11-16",
      "client_time": "12:30:00", "time_diff": 0,
      "available_slots": null, "total_slots": null}, ...]

``time`` is ``HH:MM:SS`` in the company timezone (``"timezone":
"Europe/Zagreb"`` in the ``var config = {...}`` blob the ``/v2/`` shell embeds),
so it needs no conversion.  With ``from == to`` (the **slots** view, what
``timeline_modern_display: "as_slots"`` on these pages asks for) the array is
already restricted to what SimplyBook will sell: fully booked hours and closed
days simply do not appear, and a day outside the booking window is ``[]`` too.

A multi-day range answers with the timeline *display grid* as well: the same
15-minute raster points come back tagged ``"type": "busy"`` (``slots_count: 1``)
next to the real ``"type": "free"`` starts (``slots_count: 4``).  Every row
therefore goes through the ``BOOKABLE_TYPES`` allowlist — ``busy`` can never leak
in — and is bucketed by its own ``date`` field.

The ``free`` rows of a range are exactly the union of the single-day answers.
For 2026-11-14, provider 1, the single-day call returns ``10:30`` and ``20:30``,
and the ``free`` rows of a 2026-11-14…15 range call for the same room are exactly
``10:30`` and ``20:30``, everything between them ``busy``.  Verified up to a
31-day range (2026-11-14…12-14 → 836 rows, 176 ``free``, 31 distinct days).

So the adapter asks for **one ranged call per room** (``RANGE_DAYS``), not one
per day: 31 requests plus 31 pacing sleeps per room is what made this provider
58% of total scan time, for data identical to the ranged call.

Headers / session / token flow
------------------------------
There is **no token and no session**.  ``/v2/booking/time-slots/`` is
anonymous: it answers byte-identically with plain headers, with the extra
``X-Csrf-Token`` + ``X-Debug`` that the SPA installs through ``$.ajaxSetup()``,
and with or without the ``sess_user_publicv2_*`` / ``sb_*`` cookies the ``/v2/``
shell hands out.  The adapter sends only polite XHR-ish headers (``Accept:
application/json, ...``, ``X-Requested-With: XMLHttpRequest``, ``Referer:
<host>/v2/?widget-type=iframe``) and keeps no cookie jar.

Endpoints that do *not* help (checked — do not retry them)
----------------------------------------------------------
* ``https://user-api.simplybook.me/v2/<method>`` — the historical public API.
  Every method tried (``getBookableItems``, ``getStartTimeMatrix``,
  ``getCompanyParam``, ``session``) answers a JSON-RPC error envelope
  (``-32600 Invalid Request`` / ``-32601 Method not found`` / ``Access denied``).
  The v2 widget does not use it at all; it talks to its own ``/v2/`` host.
* Server-rendered slot HTML: ``/v2/``, ``/v2/booking/`` and friends all return
  the same JS shell (``<div id="sb_main">`` + RequireJS); no slots are in the
  HTML.
* ``/v2/booking/validate-time/`` (``model/TimeValidateModel``) looks like the
  ideal "is this exact slot really bookable" oracle but is not one: it is a
  ``GET``, it is CSRF-wired (a bare ``POST`` answers
  ``403 {"error":"Invalid CSRF token"}``), and with a genuine session it only
  ever returned ``{"dates":[],"error_dates":[],"error":null}`` for both real and
  invented start times.  Not used.

Traps
-----
* **Rate limiting looks like an empty day.** Too many requests in a row and a
  balancer answers **HTTP 200** with ``<!DOCTYPE html>`` /
  ``<title>Please wait</title>`` (``/balancer/resources/...``).  That is never
  ``[]``: the adapter paces itself (``MIN_INTERVAL``), recognises the queue page,
  backs off (``BACKOFF``) and only then raises ``AdapterError`` — so a throttled
  day is reported as a failure to retry, never as "no availability".  It is also
  why ``slots()`` never asks for a whole week at once.
* **``count`` must stay 1.** The widget's ``model/BookingModel`` defaults to
  ``count: 1`` and never moves it here (the ``group_booking`` plugin is inactive
  and ``app_config`` has no ``group_booking`` key).  Each room is a provider with
  ``"qty": "1"`` — one game at a time — so ``count=2`` or more makes the API
  return ``[]`` for *every* date.  That is not "no availability".
* **The booking window is ~3 months.** ``var config`` carries ``min_date`` /
  ``max_date`` (2026-11-13 … 2027-02-13 on the day this was written).  Outside it
  every room returns ``[]``; like any other closed day that is a valid empty
  result, not an error.
* **Today is usually empty by the evening** because of SimplyBook's lead time —
  again ``[]``, not a failure.
* The widget config is the authority for the room ids; the built-in
  ``FALLBACK_ROOMS`` table (verified against ``/v2/provider/``) is only consulted
  when a room page stops carrying ``app_config.predefined``, so a page restyle
  cannot silently point one room at another room's calendar.  A page that
  carries neither raises ``AdapterError``.

End times (best effort)
-----------------------
SimplyBook stores a duration per *service*, not per slot, and the widget derives
the end time itself (``TimeSlotView.serializeData``:
``datetime + 60 * service.duration``).  The adapter copies that maths from
``GET /v2/service/`` (60 min for service 1), cached once per process.  That call
is optional: if it fails the slots are still returned with ``end=None``.
"""
from __future__ import annotations

import datetime
import json
import re
import threading
import time
import urllib.parse

try:  # normal package import (project root already on sys.path)
    from fetch import fetch
    from providers.base import Adapter, AdapterError, Slot, parse_hhmm
except ImportError:  # .../providers/simplybook.py run as a script: add the project root
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from fetch import fetch
    from providers.base import Adapter, AdapterError, Slot, parse_hhmm

WIDGET_MARKERS = ("SimplybookWidget", "simplybookwidget")
SLOTS_PATH = "/v2/booking/time-slots/"
SERVICES_PATH = "/v2/service/"

# The widget's model/BookingModel default; see the ``count`` trap above.
DEFAULT_COUNT = 1
# Seconds between two requests to the same SimplyBook host (the balancer is twitchy).
MIN_INTERVAL = 1.5
# Attempts per API call when the balancer parks us in its "Please wait" queue,
# and the pauses between them (it can stay tripped for a while).
#
# Kept deliberately low: with 4 attempts x BACKOFF the adapter could spend ~39 s
# asleep on a single request, and a full scan makes ~32 of them - measured at
# 58% of total scan time, for data that arrives as a stale fallback anyway.
# Two attempts still covers a transient queue page without stalling the scan.
MAX_ATTEMPTS = 2
BACKOFF = (4.0, 10.0, 25.0)
# One ranged call answers a whole scan window. A 31-day range returns the same
# `free` rows as 31 single-day calls (the `busy` grid rows it also carries are
# dropped by BOOKABLE_TYPES), costs ~160 KB, and replaces ~31 requests plus
# ~31 pacing sleeps - which is what made this provider 58% of total scan time.
RANGE_DAYS = 31
# The widget publishes a ~3-month window; never ask beyond it.
MAX_AHEAD_DAYS = 89
# After a failed span fetch, leave that room alone for this long. The scan asks
# for each day separately, so without a cooldown one throttled room would
# re-attempt the whole window once per day.
FAILURE_COOLDOWN = 120.0
# ``type`` values SimplyBook uses for a time it will actually sell.  An allowlist,
# never a blocklist: "busy" and friends must never leak into the calendar.
BOOKABLE_TYPES = {"free", "available"}

# Last-resort room -> (company host, provider_id, service_id), verified against
# GET /v2/provider/ on 2026-11-13.  Only used when a room page yields no ids.
FALLBACK_ROOMS = {
    "tajanstveni-slucaj-zagrebacke-vjestice":
        ("https://zagrebenigmarium.simplybook.it", "1", "1"),
    "femme-fatale":
        ("https://zagrebenigmarium.simplybook.it", "2", "1"),
    "unlock-zagreb":
        ("https://zagrebenigmarium.simplybook.it", "3", "1"),
}

_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

_PACE_LOCK = threading.Lock()
_PACE_LAST = 0.0

_DURATION_LOCK = threading.Lock()
_DURATION_CACHE: dict[str, int | None] = {}

# SimplyBook answers a throttled request with a "please wait" queue page or by
# simply hanging. Sitting through the generic 45 s x 3-attempt budget produces
# nothing usable - only a stale fallback - while stalling the whole scan (this
# adapter measured at 58% of total scan time). Fail fast instead and let the
# stale cache answer for the rooms that could not be reached.
_TIMEOUT = 12
_RETRIES = 1


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
def _pace() -> None:
    """Keep at least ``MIN_INTERVAL`` seconds between requests to SimplyBook."""
    global _PACE_LAST
    with _PACE_LOCK:
        wait = MIN_INTERVAL - (time.monotonic() - _PACE_LAST)
        if wait > 0:
            time.sleep(wait)
        _PACE_LAST = time.monotonic()


def _fetch(url: str, headers: dict | None = None) -> tuple[int, str]:
    """``fetch()`` with its RuntimeError translated into an ``AdapterError``."""
    try:
        return fetch(url, headers=headers, cache=False,
                     timeout=_TIMEOUT, retries=_RETRIES)
    except Exception as exc:  # noqa: BLE001 - fetch() raises after its own retries
        raise AdapterError(f"simplybook: cannot fetch {url}: {exc}") from exc


def _is_queue_page(body: str) -> bool:
    """True for the balancer's HTTP-200 "Please wait" page (a rate limit)."""
    head = body[:4000].lower()
    return "<title>please wait</title>" in head or "/balancer/resources" in head


def _request_json(url: str, *, referer: str, what: str):
    """GET ``url`` and decode JSON, backing off while the balancer queues us."""
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
    }
    delay, last = 0.0, "no attempt"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if delay:
            time.sleep(delay)
        delay = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
        _pace()
        status, text = _fetch(url, headers)
        if status >= 500:
            raise AdapterError(f"simplybook {what}: HTTP {status}")
        body = text.lstrip()
        if body.startswith(("[", "{")):
            try:
                return json.loads(body)
            except ValueError as exc:
                last = f"invalid JSON ({exc})"
        elif _is_queue_page(body):
            last = "rate-limited (\"Please wait\" queue page)"
        else:
            raise AdapterError(f"simplybook {what}: unexpected response {body[:120]!r}")
    raise AdapterError(f"simplybook {what}: gave up after {MAX_ATTEMPTS} attempts ({last})")


# --------------------------------------------------------------------------- #
# widget config parsing
# --------------------------------------------------------------------------- #
def _json_object(text: str, start: int) -> tuple[dict | None, int]:
    """Extract the balanced ``{...}`` at/after ``start`` and parse it as JSON."""
    brace = text.find("{", start)
    if brace == -1:
        return None, -1
    depth, in_string, escaped = 0, False, False
    for i in range(brace, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[brace:i + 1]), i + 1
                except json.JSONDecodeError:
                    return None, -1
    return None, -1


def _widget_config(markup: str) -> dict | None:
    """The ``new SimplybookWidget({...})`` options object embedded in the page."""
    for marker in WIDGET_MARKERS:
        start = markup.find(marker)
        if start == -1:
            continue
        config, _ = _json_object(markup, start + len(marker))
        if isinstance(config, dict):
            return config
    return None


def _numeric(value) -> str | None:
    """SimplyBook ids are small integers; anything else is not an id."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value).strip()
    return text if re.fullmatch(r"\d+", text) else None


def _company_url(config: dict | None) -> str | None:
    """Absolute SimplyBook host from the widget's ``url`` option."""
    if not isinstance(config, dict):
        return None
    raw = config.get("url") or config.get("company_url")
    if not isinstance(raw, str):
        return None
    url = raw.strip().rstrip("/")
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    if not url.startswith(("http://", "https://")):
        return "https://" + url
    return url


def _predefined_ids(config: dict | None) -> tuple[str | None, str | None]:
    """(provider_id, service_id) from ``app_config.predefined``, plus fallbacks.

    Some embeds pin the ids at the top level instead of inside ``app_config`` and
    some use ``service_id``/``event_id``; all of them are accepted.
    """
    if not isinstance(config, dict):
        return None, None
    app = config.get("app_config")
    predefined = app.get("predefined") if isinstance(app, dict) else None
    if not isinstance(predefined, dict):
        own = config.get("predefined")
        predefined = own if isinstance(own, dict) else {}

    provider = _numeric(predefined.get("provider"))
    service = _numeric(predefined.get("service"))
    if not provider:
        provider = _numeric(config.get("provider_id")) or _numeric(config.get("provider"))
    if not service:
        for key in ("service_id", "event_id", "service"):
            service = _numeric(config.get(key))
            if service:
                break
    return provider, service


def _slug(url: str) -> str:
    path = urllib.parse.urlparse(url).path.strip("/")
    return path.rsplit("/", 1)[-1].lower() if path else ""


# --------------------------------------------------------------------------- #
# payload parsing
# --------------------------------------------------------------------------- #
def _slot_times(payload, day: str) -> list[str]:
    """Unique ``HH:MM`` start times from a ``time-slots`` payload, sorted.

    The live shape is a flat list of slot dicts; lists of bare time strings and
    ``{"data": [...]}`` / ``{"YYYY-MM-DD": [...]}`` / ``{"YYYY-MM-DD":
    {"slot_start": [...]}}`` wrappers are tolerated so a cosmetic API change
    does not silently empty the calendar.
    """
    items = payload
    if isinstance(payload, dict):
        for key in ("data", "items", "results", "slots", "time_slots"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
        else:
            day_payload = payload.get(day)
            if isinstance(day_payload, list):
                items = day_payload
            elif isinstance(day_payload, dict):
                items = day_payload.get("slot_start") or day_payload.get("start") or []
            else:
                items = []
    if not isinstance(items, list):
        raise AdapterError(f"simplybook: unexpected time-slots payload {str(payload)[:120]}")

    found: set[str] = set()
    for item in items:
        if isinstance(item, str):
            label = item
        elif isinstance(item, dict):
            kind = str(item.get("type") or item.get("status") or "").strip().lower()
            if kind and kind not in BOOKABLE_TYPES:
                continue
            when = item.get("date") or item.get("client_date")
            if when and str(when)[:10] != day:
                continue
            label = str(item.get("time") or item.get("start_time") or item.get("start")
                        or item.get("id") or "")
        else:
            continue
        stamp = parse_hhmm(label)
        if stamp:
            found.add(stamp)
    return sorted(found)


def _slots_by_day(payload) -> dict[str, list[str]]:
    """``day -> [HH:MM]`` for a *ranged* payload, keeping only bookable rows.

    Same allowlist as :func:`_slot_times`, but bucketed by each row's own
    ``date`` instead of being restricted to a single day, so one 31-day response
    can answer every day of a scan.
    """
    if not isinstance(payload, list):
        raise AdapterError(f"simplybook: unexpected time-slots payload {str(payload)[:120]}")

    by_day: dict[str, set[str]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or item.get("status") or "").strip().lower()
        if kind and kind not in BOOKABLE_TYPES:
            continue
        day = str(item.get("date") or item.get("client_date") or "")[:10]
        if not _DAY_RE.fullmatch(day):
            continue
        label = str(item.get("time") or item.get("start_time") or item.get("start")
                    or item.get("id") or "")
        stamp = parse_hhmm(label)
        if stamp:
            by_day.setdefault(day, set()).add(stamp)
    return {day: sorted(times) for day, times in by_day.items()}


def _add_minutes(hhmm: str, minutes: int) -> str | None:
    hours, mins = (int(part) for part in hhmm.split(":"))
    total = hours * 60 + mins + minutes
    if total >= 24 * 60:
        return None
    return f"{total // 60:02d}:{total % 60:02d}"


def _service_duration(base_url: str, service_id: str) -> int | None:
    """Service length in minutes, cached per company host; ``None`` on any trouble.

    Purely cosmetic (it only fills ``Slot.end``), so every failure is swallowed
    and the answer — including "unknown" — is cached for the process.
    """
    key = f"{base_url}|{service_id}"
    with _DURATION_LOCK:
        if key in _DURATION_CACHE:
            return _DURATION_CACHE[key]

    minutes: int | None = None
    try:
        _pace()
        status, text = fetch(base_url + SERVICES_PATH, cache=False,
                             timeout=_TIMEOUT, retries=_RETRIES, headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        })
        if status < 400 and text.lstrip().startswith("["):
            for item in json.loads(text):
                if str(item.get("id")) != str(service_id):
                    continue
                raw = str(item.get("duration") or "").strip()
                if raw.isdigit() and int(raw) > 0:
                    minutes = int(raw)
                break
    except Exception:  # noqa: BLE001 - optional detail, never fail the scrape for it
        minutes = None

    with _DURATION_LOCK:
        _DURATION_CACHE[key] = minutes
    return minutes


# --------------------------------------------------------------------------- #
# one room
# --------------------------------------------------------------------------- #
class EnigmariumRoom:
    """A room page plus the SimplyBook ids its widget config carries."""

    def __init__(self, page_url: str) -> None:
        self.page_url = page_url
        status, markup = _fetch(page_url)
        if status != 200:
            raise AdapterError(f"{page_url} -> HTTP {status}")

        config = _widget_config(markup)
        base = _company_url(config)
        provider, service = _predefined_ids(config)
        if not (base and provider and service):
            known = FALLBACK_ROOMS.get(_slug(page_url))
            if known:
                base = base or known[0]
                provider = provider or known[1]
                service = service or known[2]
        if not base:
            raise AdapterError(f"{page_url}: SimplyBook widget url not found (markup changed?)")
        if not (provider and service):
            raise AdapterError(f"{page_url}: SimplyBook widget carries no provider/service id")

        self.base_url = base
        self.provider_id = provider
        self.service_id = service

        # bookable starts for a whole span, so a full scan costs one call per
        # room instead of one per room per day
        self._window: dict[str, list[str]] | None = None
        self._window_span: tuple[str, str] | None = None
        self._window_lock = threading.Lock()
        # a failed span fetch is not retried per day - without this one throttled
        # room would re-attempt the whole window once for every day of the scan
        self._window_failed_at: float | None = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"<EnigmariumRoom provider={self.provider_id} service={self.service_id} "
                f"{self.base_url}>")

    def slots_url(self, day: str) -> str:
        query = urllib.parse.urlencode({
            "from": day,
            "to": day,
            "provider": self.provider_id,
            "service": self.service_id,
            "count": str(DEFAULT_COUNT),
        })
        return f"{self.base_url}{SLOTS_PATH}?{query}"

    def raw_payload(self, day: str):
        """Undecoded JSON for one day — handy when the shape changes."""
        return _request_json(self.slots_url(day),
                             referer=f"{self.base_url}/v2/?widget-type=iframe",
                             what=f"time-slots {day}")

    def range_url(self, start: str, end: str) -> str:
        query = urllib.parse.urlencode({
            "from": start,
            "to": end,
            "provider": self.provider_id,
            "service": self.service_id,
            "count": str(DEFAULT_COUNT),
        })
        return f"{self.base_url}{SLOTS_PATH}?{query}"

    def _window_for(self, day: str) -> dict[str, list[str]]:
        """Bookable starts for a whole span, fetched once per room per scan."""
        with self._window_lock:
            span = self._window_span
            if self._window is not None and span and span[0] <= day <= span[1]:
                return self._window

            if (self._window_failed_at is not None
                    and time.monotonic() - self._window_failed_at < FAILURE_COOLDOWN):
                raise AdapterError(
                    "simplybook: span fetch failed moments ago, not retrying yet")

            first = datetime.date.fromisoformat(day)
            last = first + datetime.timedelta(days=RANGE_DAYS - 1)
            limit = datetime.date.today() + datetime.timedelta(days=MAX_AHEAD_DAYS)
            last = max(first, min(last, limit))

            try:
                payload = _request_json(
                    self.range_url(day, last.isoformat()),
                    referer=f"{self.base_url}/v2/?widget-type=iframe",
                    what=(f"time-slots {day}..{last.isoformat()} "
                          f"provider={self.provider_id} service={self.service_id}"),
                )
            except Exception:
                self._window_failed_at = time.monotonic()
                raise
            self._window = _slots_by_day(payload)
            self._window_span = (day, last.isoformat())
            self._window_failed_at = None
            return self._window

    def slots(self, day: str) -> list[Slot]:
        if not _DAY_RE.fullmatch(day or ""):
            raise AdapterError(f"simplybook: bad day {day!r}, expected YYYY-MM-DD")
        times = self._window_for(day).get(day, [])
        minutes = _service_duration(self.base_url, self.service_id)
        return [
            Slot(time=hhmm, end=_add_minutes(hhmm, minutes) if minutes else None)
            for hhmm in times
        ]


# --------------------------------------------------------------------------- #
# adapter
# --------------------------------------------------------------------------- #
class SimplybookAdapter(Adapter):
    """Enigmarium Zagreb on SimplyBook.me: one SimplyBook provider per room."""

    provider_id = "enigmarium"
    provider_name = "Enigmarium"

    def __init__(self) -> None:
        self._rooms: dict[str, EnigmariumRoom] = {}
        self._lock = threading.Lock()

    def _room(self, room: dict) -> EnigmariumRoom:
        url = room.get("schedule_url") or room.get("url") or ""
        if not url:
            raise AdapterError(f"simplybook: room {room.get('id')!r} has no url")
        with self._lock:
            client = self._rooms.get(url)
        if client is None:
            client = EnigmariumRoom(url)
            with self._lock:
                self._rooms[url] = client
        return client

    def slots(self, room: dict, day: str) -> list[Slot]:
        return self._room(room).slots(day)


ADAPTER = SimplybookAdapter()


# --------------------------------------------------------------------------- #
# self-test:  python providers/simplybook.py [--raw] [YYYY-MM-DD ...]
# --------------------------------------------------------------------------- #
def _main(argv: list[str]) -> int:
    import datetime
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "data", "rooms.json"), encoding="utf-8") as fh:
        rooms = [r for r in json.load(fh) if r.get("provider_id") == ADAPTER.provider_id]
    if not rooms:
        print("no enigmarium rooms in data/rooms.json")
        return 1

    today = datetime.date.today()
    wanted = [a for a in argv if not a.startswith("-")]
    try:
        days = [datetime.date.fromisoformat(a).isoformat() for a in wanted]
    except ValueError:
        print("usage: python providers/simplybook.py [--raw] [YYYY-MM-DD ...]")
        return 2
    if not days:
        days = [(today + datetime.timedelta(days=n)).isoformat() for n in range(3)]

    print(f"SimplyBook.me / Enigmarium — {len(rooms)} rooms, today is {today.isoformat()}")
    if "--raw" in argv:
        try:
            client = ADAPTER._room(rooms[0])          # noqa: SLF001 - debug helper
            print(f"\nraw payload for {rooms[0]['room']} on {days[0]}:")
            print(json.dumps(client.raw_payload(days[0]), indent=1, ensure_ascii=False)[:2000])
        except AdapterError as exc:
            print(f"raw payload failed: {exc}")

    rows = empty = 0
    for day in days:
        print(f"\n{day}")
        for room in rooms:
            try:
                found = ADAPTER.slots(room, day)
            except AdapterError as exc:
                print(f"  {room['room']:<16}  ERROR  {exc}")
                continue
            rows += 1
            if not found:
                empty += 1
                print(f"  {room['room']:<16}  (none — closed, fully booked or out of window)")
                continue
            shown = ", ".join(f"{s.time}-{s.end}" if s.end else s.time for s in found)
            print(f"  {room['room']:<16}  {len(found):>2} slots: {shown}")

    if rows and empty == rows:
        print("\nWARNING: every room/date came back empty — verify the parser "
              "(run with --raw) before trusting this run.")

    print("\nresolved SimplyBook ids (cached for this process)")
    for room in rooms:
        url = room.get("schedule_url") or room.get("url")
        client = ADAPTER._rooms.get(url)              # noqa: SLF001 - debug helper
        if client is not None:
            print(f"  {room['room']:<16}  provider={client.provider_id:<3} "
                  f"service={client.service_id:<3} {client.base_url}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
