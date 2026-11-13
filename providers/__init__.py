"""Provider registry.

A provider is enabled by the presence of ``providers/<module>.py`` exposing an
``ADAPTER`` object.  Those reusing a common booking engine share a module:

    wc          WooCommerce Bookings  -> cluego, foxinabox
    bookly      Bookly (WP)           -> escapeart
    dopbsp      DOP Booking System    -> arena
    bookeo      Bookeo widget         -> portal
    simplybook  SimplyBook.me         -> enigmarium
    wixbookings Wix Bookings          -> elephant
    lockup      The Old Lock Up (ASP) -> lockup

Import failures are tolerated so one broken provider cannot take the page down.
"""
from __future__ import annotations

import importlib

MODULES = ["wc", "bookly", "dopbsp", "bookeo", "simplybook", "wixbookings", "lockup"]

# provider_id (from the catalog) -> adapter module name
PROVIDER_TO_MODULE = {
    "cluego": "wc",
    "foxinabox": "wc",
    "escapeart": "bookly",
    "arena": "dopbsp",
    "portal": "bookeo",
    "enigmarium": "simplybook",
    "elephant": "wixbookings",
    "lockup": "lockup",
}

_loaded: dict[str, object] = {}
_errors: dict[str, str] = {}


def adapters() -> dict[str, object]:
    """Return {module_name: ADAPTER} for every importable provider module."""
    if _loaded:
        return _loaded
    for name in MODULES:
        try:
            mod = importlib.import_module(f"providers.{name}")
        except Exception as exc:  # noqa: BLE001
            _errors[name] = f"{type(exc).__name__}: {exc}"
            continue
        adapter = getattr(mod, "ADAPTER", None)
        if adapter is not None:
            _loaded[name] = adapter
    return _loaded


def adapter_for(provider_id: str):
    """Adapter handling a catalog provider id, or None."""
    name = PROVIDER_TO_MODULE.get(provider_id)
    if not name:
        return None
    return adapters().get(name)


def is_supported(provider_id: str) -> bool:
    """True when the provider can actually be scraped (adapter exists + works)."""
    adapter = adapter_for(provider_id)
    return adapter is not None and bool(getattr(adapter, "supported", True))


def unsupported_reason(provider_id: str) -> str:
    """Short human reason why a provider cannot be scraped."""
    adapter = adapter_for(provider_id)
    if adapter is None:
        module = PROVIDER_TO_MODULE.get(provider_id, "?")
        return load_errors().get(module, "no adapter implemented")
    return getattr(adapter, "unsupported_reason", "") or "not supported"


def load_errors() -> dict[str, str]:
    adapters()
    return dict(_errors)
