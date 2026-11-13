"""Adapter interface shared by every provider scraper.

Each provider module defines a subclass of :class:`Adapter` and exposes it as
``ADAPTER``.  The aggregator calls ``slots(room, day)`` for every room/day pair
inside the requested window and stores the normalized result.

Contract
--------
* ``day`` is an ISO date string in **Europe/Zagreb** local time (YYYY-MM-DD).
* Return ``[]`` when the room is simply not bookable that day.
* Return only *bookable* (i.e. not fully booked / not closed) start times.
* Raise :class:`AdapterError` for infrastructure problems (HTTP 5xx, changed
  markup, missing nonce).  Never raise for "no availability".
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


class AdapterError(Exception):
    """Provider could not be read (site down, markup changed, blocked)."""


@dataclass
class Slot:
    time: str                 # "14:30" start time, Europe/Zagreb
    end: str | None = None     # "15:45" if the provider exposes an end time
    note: str = ""             # e.g. "last slot", "English"

    def as_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, "")}


class Adapter:
    """Base class: override ``provider_id``, ``provider_name`` and ``slots``."""

    provider_id: str = ""
    provider_name: str = ""

    #: False when the provider's schedule cannot be read over plain HTTP
    #: (e.g. it needs a signed token minted by a browser).  Such providers are
    #: still listed in the UI, but never scanned and never counted as free.
    supported: bool = True
    #: short human explanation shown in the UI when ``supported`` is False
    unsupported_reason: str = ""

    def slots(self, room: dict, day: str) -> list[Slot]:
        """Return bookable start times for ``room`` on ``day``."""
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.provider_id}>"


def parse_hhmm(text: str) -> str | None:
    """Pull the first ``HH:MM`` out of a label like ``"10:30 - 11:45"``."""
    import re
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", text or "")
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return f"{hh:02d}:{mm:02d}"
