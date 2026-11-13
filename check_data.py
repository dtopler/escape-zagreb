"""Sanity-check data/availability.json before trusting the UI.

    python check_data.py [--strict]

Catches the failure modes that would silently mislead the page: malformed times,
slots outside the scan window, duplicate room ids, rooms claiming slots they do
not have, and providers that are "supported" but produced nothing at all.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, "data", "availability.json")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

problems: list[str] = []
warnings: list[str] = []


def problem(msg: str) -> None:
    problems.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def main() -> int:
    strict = "--strict" in sys.argv
    if not os.path.exists(STATE):
        print("no data/availability.json - run: python aggregate.py 30")
        return 1
    with open(STATE, encoding="utf-8") as fh:
        doc = json.load(fh)

    start = doc["window"]["start"]
    end = doc["window"]["end"]
    days = set(doc["days"])
    print(f"scan {doc['generated_at']}  window {start}..{end} "
          f"({doc['window']['days']} days) in {doc.get('took_seconds')}s")

    # ---- window integrity
    d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    expect = {(d0 + dt.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)}
    if days != expect:
        problem(f"days[] does not match the window: {len(days)} vs {len(expect)}")

    # ---- rooms
    ids = [r["id"] for r in doc["rooms"]]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        problem(f"duplicate room ids: {sorted(dupes)}")

    supported = {p["id"] for p in doc["providers"] if p["supported"]}
    by_provider: dict[str, int] = {}
    total = 0
    for room in doc["rooms"]:
        slots = room.get("slots") or {}
        count = 0
        for day, entries in slots.items():
            if day not in days:
                problem(f"{room['id']}: slot day {day} outside the scan window")
            if not entries:
                problem(f"{room['id']}: empty list stored for {day} (should be omitted)")
            seen = set()
            for entry in entries:
                stamp = entry["time"] if isinstance(entry, dict) else entry
                if not TIME_RE.match(str(stamp)):
                    problem(f"{room['id']} {day}: bad time {stamp!r}")
                if str(stamp) in seen:
                    problem(f"{room['id']} {day}: duplicate slot {stamp}")
                seen.add(str(stamp))
                count += 1
            ordered = [ (e["time"] if isinstance(e, dict) else e) for e in entries ]
            if ordered != sorted(ordered):
                problem(f"{room['id']} {day}: slots not sorted")
        if room.get("total_slots") != count:
            problem(f"{room['id']}: total_slots={room.get('total_slots')} but {count} slots stored")
        if room.get("unsupported") and count:
            problem(f"{room['id']}: unsupported but has {count} slots")
        if room.get("status") == "error" and count:
            warn(f"{room['id']}: marked error but still has {count} slots")
        if room.get("status") == "partial":
            warn(f"{room['id']}: {room.get('failed_days')} day(s) not read ({room.get('error')})")
        total += count
        by_provider.setdefault(room["provider"], []).append(count)
        if room["provider_id"] in supported and count == 0 and not room.get("error"):
            warn(f"{room['id']}: supported provider, zero slots in the whole window")

    # ---- providers that are supported but produced nothing anywhere
    for prov in doc["providers"]:
        if prov["supported"] and not any(by_provider.get(prov["name"], [])):
            warn(f"provider {prov['name']} is marked supported but returned no slots")

    print(f"rooms {len(ids)}   slots {total}   providers "
          f"{sum(1 for p in doc['providers'] if p['supported'])}/{len(doc['providers'])} live")
    for name, counts in sorted(by_provider.items()):
        if sum(counts):
            print(f"   {name:<34} {sum(counts):>5} slots  "
                  f"{sum(1 for c in counts if c):>2}/{len(counts)} rooms")

    for w in warnings:
        print(f"WARN  {w}")
    for p in problems:
        print(f"FAIL  {p}")
    print(f"\n{len(warnings)} warning(s), {len(problems)} problem(s)")
    return 1 if problems or (strict and warnings) else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
