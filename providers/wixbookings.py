"""Wix Bookings adapter — Elephant in the Escape Room, room "EX (IT) YU".

Provider page (the catalog's ``url`` for this room is a dead directory link, so
``room["schedule_url"]`` is used when present)::

    https://www.elephantescape.hr/booking-calendar/ex-it-yu

What the page gives us (works, plain HTTP, no browser)
------------------------------------------------------
``GET /booking-calendar/ex-it-yu`` -> **200**, ~1.19 MB of Wix "thunderbolt"
SSR HTML.  It contains ``<script type="application/json" id="wix-warmup-data">``
with the complete *catalog* record of the service, from which this adapter
reads (see :class:`WixBookingsCalendar`):

* ``appId``                 ``13d21c63-b5ec-5912-8397-c3a5ddb27a97`` (Wix Bookings)
* ``serviceId``             ``d9af3239-aa9b-499d-848f-28f47f525036`` (type ``APPOINTMENT``,
                            name ``EX(IT) YU``, defaultCapacity 1)
* ``schedule.id``           ``1e02c5ca-aa9b-4ed3-915b-b367b476967e``
* durations                 ``70`` minutes, ``timeBetweenSessions`` 50
* staff                     ``Game Master`` (working-hours schedule ``50e8d205-…``)
* timezone                  site timezone ``Europe/Belgrade`` (Wix's alias for Zagreb)
* price                     ``50€ - 90€``, payment ``inPerson`` (cash), no online payment
* booking policy            latest booking ``480`` min before start (site text: "for
                            slots starting in less than 8 h, call us")

What the page does **not** give us — and why this adapter cannot return times
---------------------------------------------------------------------------
The slot list is not server-rendered.  The calendar widget
(``https://static.parastorage.com/services/bookings-calendar-widget/1.3660.0/
BookingCalendarViewerWidgetNoCss.bundle.min.js``) ships an SSR *skeleton* only
(``data-hook="widget-loader"`` / ``widget-loader-spinner``) and then fetches
availability from the Wix api-gateway over XHR::

    GET /_api/bookings/v1/availability?serviceId=<id>&fromDate=…&toDate=…

That route exists, but it refuses anonymous callers.  Measured on 2026-11-13,
with ``Referer``/``Origin``/``x-wix-client-artifact-id`` set, with and without
``Authorization``/``x-wix-instance``/``x-wix-instance-id``/
``x-wix-app-definition-id`` headers, and with the instance id in the query
string — *always* the same answer::

    HTTP 401
    {"message":"api gateway failed to resolve instance id",
     "details":{"applicationError":{"code":"FAILED_TO_RESOLVE_INSTANCE_ID"}}}

The widget signs its requests with ``authorization: <signedInstance>`` (see the
``WixHeaders`` builder in the bundle: ``{authorization: opts.signedInstance}``,
and the ``x-wix-linguist`` helper that reads ``signedInstance.split(".")[1]``
and ``JSON.parse(atob(...)).instanceId``).  That signed instance is a JWT minted
by Wix per app instance (``e4b0799d-3d27-4628-968e-3aea4ccbc303``) and is never
present in the public HTML: ``grep`` for ``signedInstance`` -> 0 hits, for a
JWT (``eyJ…``) -> only the editor's unrelated ``signedAppRenderInfo``.

Other spellings tried (all answer with the site's own Wix error page, i.e. the
route does not exist):

    GET  /_api/bookings/v1/services                    404 (HTML)
    GET  /_api/bookings/v1/services/<serviceId>        404 (HTML)
    GET  /_api/bookings/v1/schedules/<scheduleId>      404 (HTML)
    POST /_api/bookings/v1/availability                404 (HTML)
    POST /_api/bookings/v1/availability/query          404 (HTML)
    GET  /_api/bookings/v1/availability/calendar       404 (HTML)
    POST /_api/bookings/v1/time-slot/query             404 (HTML)
    GET  /_api/bookings-availability/v2/time-slot/query 404 (HTML)
    GET  /_api/wix-bookings-api/v1/services/<id>       404 (HTML)
    POST /_api/wix-bookings-api/v1/availability        403 (HTML)
    POST /_api/cloud-data/v1/wix-bookings/…            404 (empty)
    POST /_api/wix-code-public-dispatcher-ng/siteview/graphql  404 (HTML)
    POST /_api/v1/access-tokens                        403 (HTML)  — needs CSRF + client secret
    GET  /_api/v2/dynamicmodel                          200 (visitor bootstrap, no bookings)
    GET  https://editor.wixapps.net/render/prod/editor/
         bookings-calendar-widget/1.3660.0/BookingCalendar   200 (loader shell, needs instance)

Page URL variants (``?date=``, ``?selectedDate=``, ``?startDate=``, ``?day=``,
``/booking-calendar/ex-it-yu/2026-11-16``) were also tried: none of them
server-renders slots (no ``HH:MM`` slot markup, no ``timeSlot`` payload).

Conclusion: reading bookable times requires executing JavaScript in a browser so
that Wix mints a signed instance token; **there is no plain-HTTP path**.  Per the
project contract this adapter therefore raises :class:`AdapterError`
("wix: availability requires signed instance token …") instead of inventing
times — it never returns a time Wix did not publish.  ``[]`` is still reserved
for "closed / nothing available", which we cannot distinguish either, so it is
not returned as a stand-in.

The metadata parsed from the warmup data is still exposed via
``ADAPTER.service_info(room)`` for the aggregator/link-checker, and
:meth:`WixBookingsCalendar.slots` keeps a tolerant parser for the availability
payload, so the day Wix exposes an anonymous route the adapter starts working
without changes.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import urllib.parse
from datetime import date, datetime, timedelta, timezone

if __package__ in (None, ""):  # run as a script: python providers/wixbookings.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fetch import fetch
from providers.base import Adapter, AdapterError, Slot, parse_hhmm

BOOKINGS_APP_ID = "13d21c63-b5ec-5912-8397-c3a5ddb27a97"
SCHEDULE_URL = "https://www.elephantescape.hr/booking-calendar/ex-it-yu"
AVAILABILITY_PATH = "/_api/bookings/v1/availability"
WARMUP_ELEMENT = "wix-warmup-data"

# Keys the Wix availability payload could plausibly use for a slot list / bounds.
_SLOT_LISTS = ("slots", "timeSlots", "availabilitySlots")
_START_KEYS = ("startDate", "startTime", "slotStart", "start")
_END_KEYS = ("endDate", "endTime", "slotEnd", "end")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _script_json(html: str, element_id: str) -> dict | None:
    """Parse ``<script type="application/json" id="...">`` out of a Wix page."""
    match = re.search(
        r'<script[^>]*type="application/json"[^>]*id="%s"[^>]*>' % re.escape(element_id),
        html,
    )
    if not match:
        return None
    end = html.find("</script>", match.end())
    if end == -1:
        return None
    try:
        blob = json.loads(html[match.end():end])
    except json.JSONDecodeError:
        return None
    return blob if isinstance(blob, dict) else None


def _last_sunday(year: int, month: int) -> date:
    day = date(year, month, 1) + timedelta(days=31)
    day = day.replace(day=1) - timedelta(days=1)          # last day of month
    while day.weekday() != 6:                              # 6 = Sunday
        day -= timedelta(days=1)
    return day


def _to_local_hhmm(stamp: datetime) -> str:
    """Render an ISO timestamp as Europe/Zagreb wall-clock time (``HH:MM``).

    ``zoneinfo`` needs a tz database that plain Windows Python usually lacks, so
    the EU DST rule is applied directly: UTC+1, switching to UTC+2 from the last
    Sunday of March 01:00 UTC until the last Sunday of October 01:00 UTC.
    """
    if stamp.tzinfo is None:
        return stamp.strftime("%H:%M")
    utc = stamp.astimezone(timezone.utc)
    dst_start = datetime.combine(_last_sunday(utc.year, 3), datetime.min.time(),
                                 timezone.utc) + timedelta(hours=1)
    dst_end = datetime.combine(_last_sunday(utc.year, 10), datetime.min.time(),
                               timezone.utc) + timedelta(hours=1)
    offset = 2 if dst_start <= utc < dst_end else 1
    return (utc + timedelta(hours=offset)).strftime("%H:%M")


def _parse_stamp(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _slot_time(value) -> str | None:
    """``"2026-11-16T10:00:00+01:00"`` -> ``"10:00"`` (local), ``"10:00"`` -> ``"10:00"``."""
    if isinstance(value, str) and ":" not in value:
        return None                       # date-only value, no start time in it
    stamp = _parse_stamp(value)
    if stamp is not None:
        return _to_local_hhmm(stamp)
    if isinstance(value, str):
        return parse_hhmm(value)
    return None


def _slots_from_payload(payload, day: str, where: str) -> list[Slot]:
    """Tolerant reader for a Wix availability response.

    Only real start times published by Wix are used; anything unrecognised is an
    ``AdapterError`` (never a made-up slot list).
    """
    entries = None
    if isinstance(payload, dict):
        for key in _SLOT_LISTS:
            candidate = payload.get(key)
            if isinstance(candidate, list):
                entries = candidate
                break
        if entries is None:
            for value in payload.values():
                if isinstance(value, dict):
                    for key in _SLOT_LISTS:
                        if isinstance(value.get(key), list):
                            entries = value[key]
                            break
                if entries is not None:
                    break
    elif isinstance(payload, list):
        entries = payload

    if entries is None:
        raise AdapterError(f"wix: unrecognised availability payload from {where}")

    out: list[Slot] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_start = next((entry.get(k) for k in _START_KEYS if entry.get(k)), None)
        start = _slot_time(raw_start)
        if not start:
            continue
        stamp = _parse_stamp(raw_start) or _parse_stamp(entry.get("date") or "")
        if stamp is not None and stamp.date().isoformat() != day:
            continue                                # different day, don't guess
        if start in seen:
            continue
        seen.add(start)
        raw_end = next((entry.get(k) for k in _END_KEYS if entry.get(k)), None)
        out.append(Slot(time=start, end=_slot_time(raw_end)))
    out.sort(key=lambda slot: slot.time)
    return out


def _is_permanent(reason: str) -> bool:
    """True when retrying the same request cannot possibly help."""
    lowered = reason.lower()
    return ("signed instance" in lowered or "endpoint gone" in lowered
            or "unrecognised availability payload" in lowered)


# --------------------------------------------------------------------------- #
# one service / schedule page
# --------------------------------------------------------------------------- #
class WixBookingsCalendar:
    """The Bookings service page for one Wix room, plus its availability call."""

    def __init__(self, page_url: str):
        self.page_url = page_url
        parts = urllib.parse.urlsplit(page_url)
        self.base = f"{parts.scheme}://{parts.netloc}" if parts.scheme else ""
        if not self.base:
            raise AdapterError(f"wix: not an absolute url: {page_url}")

        status, body = fetch(page_url, cache=False)
        if status != 200:
            raise AdapterError(f"{page_url} -> HTTP {status}")
        self.html = body

        warmup = _script_json(body, WARMUP_ELEMENT)
        if not warmup:
            raise AdapterError(f"{page_url}: {WARMUP_ELEMENT} missing (page layout changed?)")
        apps = warmup.get("appsWarmupData") or {}
        app = apps.get(BOOKINGS_APP_ID) or {}

        self.service = self._find_service(app)
        self.settings = self._find_settings(app)
        self.service_id = str(self.service.get("id") or "")
        if not self.service_id:
            raise AdapterError(f"{page_url}: service id not found in warmup data")

        schedule = self.service.get("schedule") or {}
        self.schedule_id = str(schedule.get("id") or "")
        constraints = schedule.get("availabilityConstraints") or {}
        durations = constraints.get("durations") or []
        self.duration = None
        if durations and isinstance(durations[0], dict):
            self.duration = durations[0].get("minutes")
        self.session_durations = constraints.get("sessionDurations") or []

        self.session_window = self._session_window(schedule)
        props = (self.settings or {}).get("siteProperties") or {}
        self.timezone = props.get("timeZone") or ""
        self.late_booking_minutes = self._late_booking_minutes()
        self.price = self._price_text()
        self.blocked_reason: str | None = None      # set once availability is hopeless
        self.staff = [m.get("name") for m in (self.service.get("staffMembers") or [])
                      if isinstance(m, dict) and m.get("name")]

    # -- warmup parsing ---------------------------------------------------- #
    @staticmethod
    def _find_service(app: dict) -> dict:
        for key, value in app.items():
            if "queryServices" not in key or not isinstance(value, dict):
                continue
            services = value.get("services")
            if isinstance(services, list) and services and isinstance(services[0], dict):
                return services[0]
        raise AdapterError("wix: service record not found in warmup data")

    @staticmethod
    def _find_settings(app: dict) -> dict:
        for key, value in app.items():
            if "getBookingsSettings" not in key or not isinstance(value, dict):
                continue
            settings = value.get("bookingsSettings")
            if isinstance(settings, dict):
                return settings
        return {}

    @staticmethod
    def _session_window(schedule: dict) -> tuple[date | None, date | None]:
        """First/last date the schedule can produce sessions for, from the page."""
        first = _parse_stamp(schedule.get("firstSessionStart"))
        last = _parse_stamp(schedule.get("lastSessionEnd"))
        return (first.date() if first else None, last.date() if last else None)

    def _late_booking_minutes(self) -> int | None:
        policy = self.service.get("bookingPolicy") or {}
        limit = policy.get("limitLateBookingPolicy") or {}
        if limit.get("enabled"):
            minutes = limit.get("latestBookingInMinutes")
            return minutes if isinstance(minutes, int) else None
        return None

    def _price_text(self) -> str:
        payment = self.service.get("payment") or {}
        custom = payment.get("custom") or {}
        return str(custom.get("description") or "")

    # -- metadata ---------------------------------------------------------- #
    def info(self) -> dict:
        """Everything the public page honestly exposes (no availability)."""
        meta = {
            "page": self.page_url,
            "service_id": self.service_id,
            "service_name": self.service.get("name") or "",
            "service_type": self.service.get("type") or "",
            "schedule_id": self.schedule_id,
            "timezone": self.timezone,
            "duration_minutes": self.duration,
            "capacity": self.service.get("defaultCapacity"),
            "price": self.price,
            "staff": self.staff,
            "online_booking": (self.service.get("onlineBooking") or {}).get("enabled"),
            "late_booking_minutes": self.late_booking_minutes,
            "session_window": "..".join(d.isoformat() for d in self.session_window if d),
        }
        return {k: v for k, v in meta.items() if v not in (None, "", [])}

    # -- availability ------------------------------------------------------ #
    def availability_url(self, day: str) -> str:
        query = urllib.parse.urlencode({
            "serviceId": self.service_id,
            "fromDate": day,
            "toDate": day,
            "timezone": "Europe/Zagreb",
        })
        return f"{self.base}{AVAILABILITY_PATH}?{query}"

    def raw_availability(self, day: str) -> tuple[int, str]:
        """One ``GET`` on the availability route the widget itself uses."""
        return fetch(self.availability_url(day), cache=False, headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": self.page_url,
            "Origin": self.base,
            "x-wix-client-artifact-id": "bookings-calendar-widget",
        })

    def slots(self, day: str) -> list[Slot]:
        try:
            wanted = date.fromisoformat(day)
        except ValueError:
            raise AdapterError(f"wix: day is not an ISO date: {day!r}") from None

        # Outside the schedule's own session window Wix cannot offer a slot at
        # all, so "closed" is the honest answer and no request is needed.
        first, last = self.session_window
        if (first and wanted < first) or (last and wanted > last):
            return []

        if self.blocked_reason:                     # one probe per page, then stop
            raise AdapterError(self.blocked_reason)

        status, body = self.raw_availability(day)
        if status == 200:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                raise AdapterError(
                    f"wix: availability returned non-JSON for {day} ({body[:120]!r})") from None
            return _slots_from_payload(payload, day, self.page_url)

        detail = ""
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            app_error = (parsed.get("details") or {}).get("applicationError") or {}
            detail = str(app_error.get("code") or parsed.get("message") or "")
        detail = detail.strip()

        if status in (401, 403):
            code = f" {detail}" if detail else ""
            raise AdapterError(
                "wix: availability requires signed instance token "
                f"(api gateway HTTP {status}{code}) — Wix mints it in the browser, "
                f"page {self.page_url}")
        if status == 404:
            raise AdapterError(
                f"wix: availability endpoint gone (HTTP 404 for {AVAILABILITY_PATH}) — "
                "wix changed the route")
        raise AdapterError(f"wix: availability HTTP {status} for {day}"
                           + (f" ({detail})" if detail else ""))


# --------------------------------------------------------------------------- #
# adapter
# --------------------------------------------------------------------------- #
class WixBookingsAdapter(Adapter):
    """Wix Bookings — Elephant in the Escape Room (single room, "EX (IT) YU").

    Not readable over plain HTTP: /_api/bookings/v1/availability needs a signed
    instance JWT that Wix only mints for the browser-rendered widget (HTTP 401
    FAILED_TO_RESOLVE_INSTANCE_ID).  The adapter keeps the investigated code so
    the work is not lost, but flags itself unsupported so the scanner skips it
    and the UI explains why instead of pretending the room is free.
    """

    provider_id = "elephant"
    provider_name = "Elephant in the Escape Room"
    supported = False
    unsupported_reason = ("Wix Bookings needs a signed browser token "
                          "(HTTP 401); book on elephantescape.hr instead")

    def __init__(self) -> None:
        self._pages: dict[str, WixBookingsCalendar] = {}
        self._lock = threading.Lock()

    def _page(self, room: dict) -> WixBookingsCalendar:
        url = room.get("schedule_url") or room["url"]
        with self._lock:
            page = self._pages.get(url)
        if page is None:
            page = WixBookingsCalendar(url)
            with self._lock:
                self._pages[url] = page
        return page

    def service_info(self, room: dict) -> dict:
        """Catalog metadata for ``room`` (one page fetch, then cached)."""
        return self._page(room).info()

    def slots(self, room: dict, day: str) -> list[Slot]:
        """Bookable start times Wix publishes for ``day`` (ISO date, Europe/Zagreb).

        Returns ``[]`` for days outside the schedule's session window (Wix offers
        nothing there) and raises :class:`AdapterError` otherwise — availability
        is behind a signed instance token, so no times can be read without a
        browser (see the module docstring).  The failure is remembered on the
        page object, so one probe per process is made, not one per day.
        """
        page = self._page(room)
        try:
            return page.slots(day)
        except AdapterError as exc:
            message = str(exc)
            if _is_permanent(message) and not page.blocked_reason:
                with self._lock:
                    page.blocked_reason = page.blocked_reason or message
            raise


ADAPTER = WixBookingsAdapter()


# --------------------------------------------------------------------------- #
# self-test: python providers/wixbookings.py
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from datetime import date as _date

    ROOM = {
        "id": "elephant/na-c5-a1e-sobe",
        "provider_id": "elephant",
        "provider": "Elephant in the Escape Room",
        "room": "EX (IT) YU",
        "url": "https://www.elephantescape.hr/na%C5%A1e-sobe",
        "schedule_url": SCHEDULE_URL,
    }

    print(f"{ADAPTER.provider_name} [{ADAPTER.provider_id}] — self-test")
    page = None
    try:
        page = ADAPTER._page(ROOM)
        print(f"page       : {page.page_url}")
        for key, value in ADAPTER.service_info(ROOM).items():
            print(f"  {key:<20} {value}")
    except AdapterError as exc:
        print(f"PAGE FAILED: {exc}")

    days = [(_date.today() + timedelta(days=n)).isoformat() for n in (1, 2, 3)]
    print(f"\navailability probe for {', '.join(days)}")

    evidence = None
    if page is not None:
        try:
            evidence = page.raw_availability(days[0])
        except Exception as exc:                       # noqa: BLE001
            print(f"  request failed: {type(exc).__name__}: {exc}")

    if evidence is not None:
        status, body = evidence
        print(f"  GET {page.availability_url(days[0]).replace(page.base, '')}")
        print(f"  -> HTTP {status}: {body.strip()[:220] or '(empty body)'}")

    for day in days:
        try:
            found = ADAPTER.slots(ROOM, day)
            shown = ", ".join(s.time for s in found) or "none — closed / fully booked"
            print(f"  {day}: {len(found)} slot(s): {shown}")
        except AdapterError as exc:
            print(f"  {day}: AdapterError — {exc}")

    print("\nVERDICT — NOT FEASIBLE over plain HTTP (no fabricated times).")
    if page is not None and evidence is not None:
        print("  Reason: the Wix Bookings calendar widget fetches slots from")
        print(f"  GET {AVAILABILITY_PATH} and the api gateway answers")
        print(f"  HTTP {evidence[0]} FAILED_TO_RESOLVE_INSTANCE_ID without a signed")
        print("  instance JWT, which Wix only mints for the browser-rendered widget.")
        print(f"  The public page still parses fine (service {page.service_id}, "
              f"{page.duration} min slots, tz {page.timezone or 'n/a'}).")
    print("  A headless browser (Playwright/Selenium) or a Wix API key is required.")
