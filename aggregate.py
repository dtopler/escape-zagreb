"""Aggregate every room's schedule into one normalized JSON document.

Run directly for a one-off scan:

    python aggregate.py            # today .. +30 days
    python aggregate.py 44         # today .. +44 days

The web server imports :func:`scan` and runs it on a background thread, so the
same code path feeds the UI and the CLI.
"""
from __future__ import annotations

import concurrent.futures as futures
import contextlib
import datetime as dt
import json
import os
import sys
import threading
import time
from zoneinfo import ZoneInfo

from fetch import ROOT
from providers import adapter_for, is_supported, load_errors, unsupported_reason

TZ = ZoneInfo("Europe/Zagreb")
DATA = os.path.join(ROOT, "data")
STATE_PATH = os.path.join(DATA, "availability.json")
CACHE_PATH = os.path.join(DATA, "slot_cache.json")
ROOMS_PATH = os.path.join(DATA, "rooms.json")
CACHE_TTL = int(os.environ.get("ESCAPE_CACHE_TTL", 20 * 60))   # seconds
PER_HOST_CONCURRENCY = 3
# providers that punish parallel sessions with captchas / queue pages
PER_PROVIDER_CONCURRENCY = {"portal": 1, "enigmarium": 2}

# ---------------------------------------------------------------- progress ---
_progress = {
    "running": False,
    "phase": "idle",
    "done": 0,
    "total": 0,
    "started_at": None,
    "finished_at": None,
    "errors": [],
    "message": "",
}
_progress_lock = threading.Lock()
# Wall time actually spent inside each adapter. Printed at the end of a scan so
# a slow provider is visible in the CI log instead of being guessed at from a
# long silence.
_timing: dict[str, float] = {}
_timing_lock = threading.Lock()
_scan_started = 0.0


def progress() -> dict:
    with _progress_lock:
        return json.loads(json.dumps(_progress))


def _set(**kw) -> None:
    with _progress_lock:
        _progress.update(kw)


def _tick(message: str = "") -> None:
    with _progress_lock:
        _progress["done"] += 1
        done, total = _progress["done"], _progress["total"]
        if message:
            _progress["message"] = message
        # CI has no UI to poll, so emit a line now and then to keep the log alive
        if _scan_started and total and (done % 20 == 0 or done == total):
            print(f"  [{done}/{total}] {time.time() - _scan_started:.0f}s", flush=True)


def _note_error(text: str) -> None:
    with _progress_lock:
        if len(_progress["errors"]) < 40:
            _progress["errors"].append(text)


# ------------------------------------------------------------------- cache ---
class SlotCache:
    def __init__(self, path: str = CACHE_PATH, ttl: int = CACHE_TTL):
        self.path = path
        self.ttl = ttl
        self._lock = threading.Lock()
        self.data: dict[str, dict] = {}
        try:
            with open(path, encoding="utf-8") as fh:
                self.data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            self.data = {}
        self.dirty = False

    def get(self, key: str):
        with self._lock:
            hit = self.data.get(key)
        if not hit:
            return None
        if time.time() - hit.get("t", 0) > self.ttl:
            return None
        return hit.get("v")

    def put(self, key: str, value) -> None:
        with self._lock:
            self.data[key] = {"t": time.time(), "v": value}
            self.dirty = True

    def flush(self) -> None:
        with self._lock:
            if not self.dirty:
                return
            snapshot = dict(self.data)
            self.dirty = False
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False)
        os.replace(tmp, self.path)


_cache = SlotCache()


class StaleCache:
    """Last *successful* value per room/day, kept well past the TTL.

    Providers rate-limit and captcha us; when a request fails we would rather
    show yesterday's answer marked as stale than pretend the room is closed.
    """

    def __init__(self, path: str = os.path.join(DATA, "stale_cache.json"),
                 keep_days: int = 120):
        self.path = path
        self.keep = keep_days * 86400
        self._lock = threading.Lock()
        try:
            with open(path, encoding="utf-8") as fh:
                self.data: dict[str, dict] = json.load(fh)
        except (OSError, json.JSONDecodeError):
            self.data = {}

    def get(self, key: str):
        with self._lock:
            hit = self.data.get(key)
        return hit.get("v") if hit else None

    def put(self, key: str, value) -> None:
        with self._lock:
            self.data[key] = {"t": time.time(), "v": value}

    def flush(self) -> None:
        cutoff = time.time() - self.keep
        with self._lock:
            pruned = {k: v for k, v in self.data.items() if v.get("t", 0) >= cutoff}
            self.data = pruned
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(pruned, fh, ensure_ascii=False)
        os.replace(tmp, self.path)


_stale = StaleCache()


def _nullcontext():
    return contextlib.nullcontext()


# --------------------------------------------------------------- helpers ---
def load_rooms() -> list[dict]:
    with open(ROOMS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def window(days_ahead: int = 30, start: str | None = None) -> list[str]:
    first = (dt.date.fromisoformat(start) if start
             else dt.datetime.now(TZ).date())
    return [(first + dt.timedelta(days=i)).isoformat() for i in range(days_ahead + 1)]


def provider_catalog(rooms: list[dict]) -> list[dict]:
    """Provider rows for the UI, including why a provider is unsupported."""
    seen: dict[str, dict] = {}
    for room in rooms:
        entry = seen.setdefault(room["provider_id"], {
            "id": room["provider_id"],
            "name": room["provider"],
            "rooms": 0,
            "supported": is_supported(room["provider_id"]),
        })
        entry["rooms"] += 1
    for pid, entry in seen.items():
        if not entry["supported"]:
            entry["reason"] = unsupported_reason(pid)
    return sorted(seen.values(), key=lambda p: (not p["supported"], p["name"]))


# ------------------------------------------------------------------ scan ---
def scan(days_ahead: int = 30, *, start: str | None = None,
         providers: list[str] | None = None, force: bool = False,
         workers: int = 10) -> dict:
    """Scrape ``rooms x days`` and write data/availability.json."""
    started = time.time()
    global _scan_started
    _scan_started = started
    with _timing_lock:
        _timing.clear()
    rooms_all = load_rooms()
    days = window(days_ahead, start)
    room_sel = [r for r in rooms_all
                if (not providers or r["provider_id"] in providers)
                and is_supported(r["provider_id"])]
    room_sel.sort(key=lambda r: (r["provider_id"], r["room"]))

    todo: list[tuple[dict, str]] = []
    for room in room_sel:
        if adapter_for(room["provider_id"]) is None:
            continue
        for day in days:
            key = f"{room['id']}|{day}"
            if not force and _cache.get(key) is not None:
                continue
            todo.append((room, day))

    _set(running=True, phase="scanning", done=0, total=max(1, len(todo)),
         started_at=dt.datetime.now(TZ).isoformat(timespec="seconds"),
         finished_at=None, errors=[], message="starting")

    host_slots: dict[str, threading.Semaphore] = {}
    host_lock = threading.Lock()
    provider_slots: dict[str, threading.Semaphore] = {}
    provider_lock = threading.Lock()

    def semaphore_for(url: str) -> threading.Semaphore:
        host = url.split("/")[2] if "//" in url else url
        with host_lock:
            sem = host_slots.get(host)
            if sem is None:
                sem = threading.Semaphore(PER_HOST_CONCURRENCY)
                host_slots[host] = sem
            return sem

    def provider_gate(provider_id: str) -> threading.Semaphore | None:
        limit = PER_PROVIDER_CONCURRENCY.get(provider_id)
        if not limit:
            return None
        with provider_lock:
            sem = provider_slots.get(provider_id)
            if sem is None:
                sem = threading.Semaphore(limit)
                provider_slots[provider_id] = sem
            return sem

    results: dict[str, dict[str, list]] = {
        r["id"]: {d: [] for d in days} for r in room_sel
    }
    room_status: dict[str, dict] = {
        r["id"]: {"status": "ok", "error": None, "failed": 0, "stale": 0, "errors": []}
        for r in room_sel
    }

    def work(room: dict, day: str):
        adapter = adapter_for(room["provider_id"])
        key = f"{room['id']}|{day}"
        cached = None if force else _cache.get(key)
        if cached is not None:
            _tick()
            return key, cached
        url = room.get("schedule_url") or room["url"]
        gate = provider_gate(room["provider_id"])
        sem = semaphore_for(url)
        with sem, (gate if gate is not None else _nullcontext()):
            call_started = time.perf_counter()
            # printed before the call on purpose: when a provider hangs, the last
            # "->" line without a matching "<-" names it
            print(f"  -> {room['id']} {day}", flush=True)
            try:
                slots = adapter.slots(room, day)
                value = [s.as_dict() for s in slots]
                _cache.put(key, value)
                _stale.put(key, value)
                return key, value
            except Exception as exc:  # noqa: BLE001
                status = room_status[room["id"]]
                status["failed"] += 1
                status["error"] = f"{type(exc).__name__}: {exc}"[:220]
                if len(status["errors"]) < 3:
                    status["errors"].append(f"{day}: {status['error']}"[:260])
                _note_error(f"{room['id']} {day}: {type(exc).__name__}: {exc}"[:300])
                fallback = _stale.get(key)
                if fallback is not None:
                    status["stale"] += 1
                    _tick()
                    return key, fallback
                _tick()
                return key, []
            finally:
                seconds = time.perf_counter() - call_started
                if seconds >= 2.0:
                    print(f"  <- {room['id']} {day} {seconds:.1f}s", flush=True)
                with _timing_lock:
                    _timing[room["provider_id"]] = (
                        _timing.get(room["provider_id"], 0.0) + seconds)

    if todo:
        with futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            pending = {pool.submit(work, room, day): (room, day) for room, day in todo}
            for future in futures.as_completed(pending):
                room, day = pending[future]
                try:
                    key, value = future.result()
                except Exception as exc:  # noqa: BLE001 - never kill the scan
                    _note_error(f"{room['id']} {day}: {type(exc).__name__}: {exc}"[:300])
                    continue
                results[room["id"]][day] = value
    else:
        _tick()

    # merge cached days that were never queued
    for room in room_sel:
        for day in days:
            key = f"{room['id']}|{day}"
            if not results[room["id"]][day]:
                cached = _cache.get(key)
                if cached:
                    results[room["id"]][day] = cached
    _cache.flush()
    _stale.flush()

    rooms_out = []
    for room in room_sel:
        status = room_status[room["id"]]
        failed, stale, total_days = status["failed"], status["stale"], len(days)
        if failed == 0:
            label, error = "ok", None
        elif stale and stale == failed:
            label = "stale"
            error = (f"provider unreachable, showing previous data for {stale} day(s) — "
                     + (status["error"] or ""))
        elif failed >= total_days:
            label, error = "error", status["error"]
        else:
            label = "partial"
            error = (f"{failed}/{total_days} days not refreshed — "
                     + "; ".join(status["errors"][:2]))
        day_map = {d: v for d, v in results[room["id"]].items() if v}
        rooms_out.append({
            **{k: room.get(k) for k in ("id", "provider_id", "provider", "room",
                                        "url", "schedule_url", "players_min",
                                        "players_max", "duration_min", "tags")},
            "slots": day_map,
            "total_slots": sum(len(v) for v in day_map.values()),
            "status": label,
            "error": error,
            "failed_days": failed,
            "stale_days": stale,
            "unsupported": False,
        })

    # rooms whose provider has no adapter are still listed, flagged, for the UI
    for room in rooms_all:
        if room["provider_id"] in {r["provider_id"] for r in room_sel}:
            continue
        rooms_out.append({
            **{k: room.get(k) for k in ("id", "provider_id", "provider", "room",
                                        "url", "schedule_url", "players_min",
                                        "players_max", "duration_min", "tags")},
            "slots": {}, "total_slots": 0, "status": "unsupported",
            "error": None, "failed_days": 0, "stale_days": 0, "unsupported": True,
        })

    doc = {
        "generated_at": dt.datetime.now(TZ).isoformat(timespec="seconds"),
        "took_seconds": round(time.time() - started, 1),
        "tz": "Europe/Zagreb",
        "window": {"start": days[0], "end": days[-1], "days": len(days)},
        "providers": provider_catalog(rooms_all),
        "days": days,
        "rooms": sorted(rooms_out, key=lambda r: (r["provider"], r["room"])),
    }
    # per-provider stats, so the UI can say "live but nothing open" / "blocked"
    for prov in doc["providers"]:
        mine = [r for r in rooms_out if r["provider_id"] == prov["id"]]
        prov["slots"] = sum(r["total_slots"] for r in mine)
        prov["rooms_with_slots"] = sum(1 for r in mine if r["total_slots"])
        prov["rooms_failed"] = sum(1 for r in mine if r["status"] in ("error", "partial", "stale"))
        prov["rooms_stale"] = sum(1 for r in mine if r["stale_days"])
        prov["error"] = next((r["error"] for r in mine
                              if r["status"] in ("error", "stale")), None)

    # where the wall time went — the first thing to look at when CI is slow
    if _timing:
        names = {p["id"]: p["name"] for p in doc["providers"]}
        print("\n  adapter time, summed per provider:", flush=True)
        for pid, secs in sorted(_timing.items(), key=lambda kv: -kv[1]):
            print(f"    {names.get(pid, pid):<26} {secs:8.1f}s", flush=True)
        print(f"    {'(scan wall clock)':<26} {time.time() - started:8.1f}s", flush=True)

    os.makedirs(DATA, exist_ok=True)
    tmp = f"{STATE_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)

    ok_rooms = sum(1 for r in rooms_out if r["total_slots"])
    _set(running=False, phase="done", done=_progress["total"],
         finished_at=dt.datetime.now(TZ).isoformat(timespec="seconds"),
         message=f"{ok_rooms} rooms with slots")
    return doc


def scan_async(**kw) -> threading.Thread:
    def runner():
        try:
            scan(**kw)
        except Exception as exc:  # noqa: BLE001
            _set(running=False, phase="error", message=f"{type(exc).__name__}: {exc}")
            _note_error(str(exc))
    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    horizon = int(args[0]) if args else 30
    providers = None
    for flag in flags:
        if flag.startswith("--providers="):
            providers = [p for p in flag.split("=", 1)[1].split(",") if p]
    doc = scan(horizon, force="--force" in flags, providers=providers)
    print(f"{doc['window']['start']} .. {doc['window']['end']} "
          f"({doc['window']['days']} days) in {doc['took_seconds']}s")
    for prov in doc["providers"]:
        flag = "ok" if prov["supported"] else "UNSUPPORTED"
        print(f"  {prov['name']:<32} {prov['rooms']:>2} rooms  {flag}"
              + ("" if prov["supported"] else f"  ({prov.get('reason')})"))
    print()
    for room in doc["rooms"]:
        days_with = len(room["slots"])
        total = room["total_slots"]
        note = f"  [{room['status']}] {room['error']}" if room["error"] else ""
        print(f"  {room['id']:<52} {total:>4} slots on {days_with:>2} days{note}")
