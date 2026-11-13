"""WooCommerce Bookings adapter — ClueGo (4 rooms) and Fox in a Box (3 rooms).

Mechanism, reverse-engineered from ``woocommerce-bookings/dist/frontend.js``:

1. The product page embeds two JSON blobs::

       var wc_bookings_booking_form = {..., "product_id":"986", ...};
       var booking_form_params = {"ajax_url":"...admin-ajax.php",
                                  "nonce":{...},"server_timezone":"Europe/Zagreb"};

2. Time slots for one day come from an AJAX call (exactly what the real booking
   widget fires when you click a date)::

       POST {ajax_url}
       action=wc_bookings_get_blocks
       form=<urlencoded serialisation of the booking form with the target date>

   The response is the HTML of the time picker::

       <li><a data-value="2026-11-16T13:30:00+0100">13:30</a></li>

3. A separate endpoint powers the date picker
   (``/?wc-ajax=wc_bookings_find_booked_day_blocks``); it is not needed here
   because step 2 already returns the authoritative bookable start times.

Only start times are published by the widget, so ``end`` stays ``None`` unless
the label itself carries a range.
"""
from __future__ import annotations

import html
import json
import re
import threading
import urllib.parse

from fetch import fetch
from providers.base import Adapter, AdapterError, Slot, parse_hhmm

FORM_CLASS = "wc-bookings-booking-form"


def _json_var(text: str, name: str):
    """Parse ``var name = {...};`` out of a script blob."""
    start = text.find(name)
    if start == -1:
        return None
    brace = text.find("{", start + len(name))
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


class WCBookings:
    """One WooCommerce Bookings product page."""

    def __init__(self, page_url: str):
        self.page_url = page_url
        status, body = fetch(page_url, cache=False)
        if status != 200:
            raise AdapterError(f"{page_url} -> HTTP {status}")
        self.html = body
        self.params = _json_var(body, "booking_form_params") or {}
        conf = (_json_var(body, "wc_bookings_booking_form")
                or _json_var(body, "wc_bookings_booking_form_"))
        if not conf:
            raise AdapterError(f"{page_url}: booking form config not found")
        self.product_id = str(conf.get("product_id") or "")
        self.ajax_url = self.params.get("ajax_url") or urllib.parse.urljoin(
            page_url, "/wp-admin/admin-ajax.php")
        self.inputs = self._form_inputs()

    def _form_inputs(self) -> dict[str, str]:
        pos = self.html.find(FORM_CLASS)
        if pos == -1:
            return {}
        start = self.html.rfind("<form", 0, pos)
        end = self.html.find("</form>", pos)
        chunk = self.html[start if start != -1 else 0:end if end != -1 else len(self.html)]
        inputs: dict[str, str] = {}
        for tag in re.finditer(r"<(input|select|textarea)\b[^>]*>", chunk, re.I):
            raw = tag.group(0)
            name = re.search(r'\bname=["\']([^"\']+)["\']', raw)
            if not name:
                continue
            nm = name.group(1)
            if nm.startswith("wc_bookings_field_start_date_"):
                continue
            if tag.group(1).lower() == "select":
                rest = chunk[tag.end():]
                close = rest.find("</select>")
                sel = rest[:close if close != -1 else 0]
                chosen = re.search(r'<option[^>]*selected[^>]*value=["\']([^"\']*)', sel)
                inputs[nm] = chosen.group(1) if chosen else ""
            else:
                val = re.search(r'\bvalue=["\']([^"\']*)["\']', raw)
                inputs[nm] = html.unescape(val.group(1)) if val else ""
        if self.product_id and "add-to-cart" not in inputs:
            inputs["add-to-cart"] = self.product_id
        return inputs

    def slots_html(self, day: str) -> str:
        payload = dict(self.inputs)
        year, month, dom = day.split("-")
        payload.update({
            "wc_bookings_field_start_date_day": dom,
            "wc_bookings_field_start_date_month": month,
            "wc_bookings_field_start_date_year": year,
        })
        body = urllib.parse.urlencode({
            "action": "wc_bookings_get_blocks",
            "form": urllib.parse.urlencode(payload),
            "get_next_day": "false",
        }).encode()
        status, text = fetch(self.ajax_url, method="POST", data=body, cache=False, headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.page_url,
        })
        if status >= 500:
            raise AdapterError(f"{self.page_url}: get_blocks HTTP {status}")
        return text

    def slots(self, day: str) -> list[Slot]:
        text = self.slots_html(day)
        out: list[Slot] = []
        for m in re.finditer(r'<a[^>]*data-value=["\']([^"\']+)["\'][^>]*>(.*?)</a>', text, re.S):
            value = m.group(1).strip()
            label = html.unescape(re.sub(r"<[^>]+>", " ", m.group(2)))
            label = re.sub(r"\s+", " ", label).strip()
            if not value:
                continue
            stamp = re.search(r"T(\d{2}):(\d{2})", value)
            start = f"{stamp.group(1)}:{stamp.group(2)}" if stamp else parse_hhmm(label)
            if not start:
                continue
            end = None
            if "-" in label:
                after = label.split("-", 1)[1]
                end = parse_hhmm(after)
            out.append(Slot(time=start, end=end))
        out.sort(key=lambda s: s.time)
        return out


class WCBookingsAdapter(Adapter):
    provider_id = "wcbookings"
    provider_name = "WooCommerce Bookings"

    def __init__(self) -> None:
        self._cache: dict[str, WCBookings] = {}
        self._lock = threading.Lock()

    def _client(self, room: dict) -> WCBookings:
        url = room.get("schedule_url") or room["url"]
        with self._lock:
            client = self._cache.get(url)
        if client is None:
            client = WCBookings(url)
            with self._lock:
                self._cache[url] = client
        return client

    def slots(self, room: dict, day: str) -> list[Slot]:
        return self._client(room).slots(day)


ADAPTER = WCBookingsAdapter()
