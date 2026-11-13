"""Build data/room_meta.json — players, duration and genres for every room.

Sources, in order of preference
--------------------------------
1. World of Escapes (hr.worldofescapes.com), which publishes a structured player
   range + duration per room and has one page per genre (`/zagreb/tags/<tag>`)
   that lists the rooms in it.
2. The provider's own room page, regex-scraped for "2-6 igrača" style text, used
   for rooms World of Escapes does not list (Arena, Elephant, ...). Those get
   ``"tags": []`` and ``"players_source": "provider page"``; an unknown player
   range stays null so the UI can be honest about it.

Run:  python build_meta.py        (writes data/room_meta.json)
"""
from __future__ import annotations

import json
import os
import re
import sys

from fetch import ROOT, fetch

DATA = os.path.join(ROOT, "data")
WOE = "https://hr.worldofescapes.com"

# our room id -> worldofescapes quest slug
ALIASES = {
    "cluego/krada-mona-lise": "cluego-escape-rooms-zagreb-mona-lisa-heist",
    "cluego/tajna-profesora-balthazara": "cluego-escape-room-zagreb-memento",
    "cluego/memento": "cluego-escape-rooms-zagreb-memento",
    "escapeart/robin-hood": "escape-art-robin-hood",
    "escapeart/drakuline-odaje": "escape-art-drakuline-odaje",
    "escapeart/magicna-knjiznica": "escape-art-magicna-knjiznica",
    "foxinabox/reservation-index-game-zodiac-killer": "fox-in-a-box-zagreb-zodiac-killer",
    "foxinabox/reservation-index-game-prison-day-20": "fox-in-a-box-zagreb-prison-break",
    "enigmarium/tajanstveni-slucaj-zagrebacke-vjestice":
        "enigmarium-the-mysterious-case-of-the-zagreb-witch",
    "enigmarium/femme-fatale": "enigmarium-femme-fatale",
    "portal/avantura-kocke": "portal-escape-cubes",
    "portal/avanture-dvorac-crne-kraljice": "portal-escape-black-queens-castle",
    "portal/avanture-tajna-don-carla": "portal-escape-don-carlos-secret",
    "portal/avanture-ozirisov-hram": "portal-escape-temple-of-osiris",
    "lockup/room-single-haunted-html": "the-old-lockup-haunted",
    "lockup/room-detail-asp-roomid-4": "the-old-lockup-haunted-2-the-morgue",
    "lockup/room-detail-asp-roomid-1": "the-old-lockup-kidnapped",
    "lockup/room-detail-asp-roomid-3": "the-old-lockup-the-crypt",
    "lockup/room-detail-asp-roomid=5": "the-old-lockup-the-bailiff",
    "lockup/room-detail-asp-roomid-5": "the-old-lockup-the-bailiff",
    "lockup/room-detail-asp-roomid-6": "the-old-lockup-narcos",
    "lockup/room-detail-asp-roomid-7": "the-old-lockup-panic-room",
    "lockup/room-detail-asp-roomid-8": "the-old-lockup-the-ritual",
    "exitgames/booking-system-trinity": "exitgames-zagreb-a-i-trinity-dawn-of-the-robots",
    "exitgames/booking-system-teslino-nasljede": "exitgames-zagreb-teslas-legacy",
}

# worldofescapes tag -> display genre (player-count and promo tags are skipped)
GENRES = {
    "scary": "Horror",
    "vampires": "Vampires",
    "detective": "Detective",
    "heist": "Heist",
    "historical": "Historic",
    "ancient-worlds": "Ancient",
    "family": "Family",
    "adventure": "Adventure",
    "mystical": "Mystical",
    "futuristic": "Sci-fi",
    "prison-break": "Prison",
    "hospital": "Hospital",
    "outdoor": "Outdoor",
    "vr": "VR",
    "challenging": "Challenging",
    "long": "Long",
}

PLAYERS_RE = re.compile(r"(\d{1,2})\s*[–—\-]\s*(\d{1,2})(?:\s*(?:igra|osob|players))", re.I)
DURATION_RE = re.compile(r"(\d{2,3})\s*min", re.I)

# Genres for rooms World of Escapes does not tag, taken from the room's own name
# or page copy ("Pustolovina Divljeg Zapada" -> Adventure, "na otvorenom" ->
# Outdoor, "1202." -> Historic, "Heist"/"Crime" -> the obvious genre). Only
# rooms whose genre is unambiguous are listed; everything else stays untagged so
# the UI never claims a genre it cannot back up.
MANUAL_TAGS = {
    "cluego/anno-1202": ["Historic"],
    "foxinabox/reservation-index-game-bank": ["Adventure"],
    "enigmarium/unlock-zagreb": ["Outdoor", "Adventure"],
    "arena/game-rooms-amazing-heist": ["Heist"],
    "arena/game-rooms-mind-crime-2": ["Detective"],
    "arena/game-rooms-secret-agent-room": ["Detective"],
}


def woe_cards() -> dict[str, dict]:
    """slug -> {name, players_min, players_max, duration_min} from the city page."""
    st, body = fetch(f"{WOE}/zagreb", cache=False)
    out: dict[str, dict] = {}
    # each card: <a href="/zagreb/quests/SLUG"><img title="NAME" ...> ...
    #            <span class="params__text">2 – 6</span>
    for m in re.finditer(r'href="(/zagreb/quests/[^"]+)"', body):
        slug = m.group(1).rsplit("/", 1)[-1]
        chunk = body[m.start():m.start() + 4000]
        title = re.search(r'title="([^"]+)"', chunk)
        players = re.search(r'params--players.*?params__text">([^<]+)<', chunk, re.S)
        dur = re.search(r'params--time.*?params__text">([^<]+)<', chunk, re.S) \
            or DURATION_RE.search(chunk)
        pmin = pmax = None
        if players:
            nums = re.findall(r"\d+", players.group(1))
            if len(nums) >= 2:
                pmin, pmax = int(nums[0]), int(nums[1])
            elif nums:
                pmin = pmax = int(nums[0])
        dur_min = None
        if dur:
            dm = DURATION_RE.search(dur.group(0) if dur.lastindex else dur.group(1))
            dur_min = int(dm.group(1)) if dm else None
        out.setdefault(slug, {
            "name": re.sub(r"&amp;", "&", title.group(1)) if title else slug,
            "players_min": pmin,
            "players_max": pmax,
            "duration_min": dur_min,
        })
    return out


def woe_tags() -> dict[str, list[str]]:
    """slug -> [genre, ...] by walking every genre tag page."""
    tags: dict[str, set[str]] = {}
    for tag, label in GENRES.items():
        st, body = fetch(f"{WOE}/zagreb/tags/{tag}", cache=False)
        if st != 200:
            print(f"  ! tag {tag} -> HTTP {st}")
            continue
        slugs = set(re.findall(r'href="/zagreb/quests/([^"/]+)"', body))
        print(f"  {label:<12} {len(slugs)} rooms")
        for slug in slugs:
            tags.setdefault(slug, set()).add(label)
    return {k: sorted(v) for k, v in tags.items()}


def scrape_players(url: str) -> tuple[int | None, int | None, int | None]:
    """Fallback: read a player range + duration off the provider's own page."""
    try:
        st, body = fetch(url, cache=False)
    except Exception:  # noqa: BLE001
        return None, None, None
    if st != 200:
        return None, None, None
    text = re.sub(r"<script.*?</script>", " ", body, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;?", " ", text)
    m = PLAYERS_RE.search(text)
    pmin = pmax = None
    if m:
        pmin, pmax = int(m.group(1)), int(m.group(2))
    d = DURATION_RE.search(text)
    return pmin, pmax, (int(d.group(1)) if d else None)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    with open(os.path.join(DATA, "rooms.json"), encoding="utf-8") as fh:
        rooms = json.load(fh)

    print("worldofescapes city page ...")
    cards = woe_cards()
    print(f"  {len(cards)} quests")
    print("worldofescapes tag pages ...")
    tags = woe_tags()

    meta: dict[str, dict] = {}
    for room in rooms:
        slug = ALIASES.get(room["id"])
        card = cards.get(slug) if slug else None
        entry = {
            "players_min": None, "players_max": None, "duration_min": None,
            "tags": [], "source": None,
        }
        if card:
            entry.update({
                "players_min": card["players_min"],
                "players_max": card["players_max"],
                "duration_min": card["duration_min"],
                "tags": tags.get(slug, []),
                "source": "worldofescapes",
            })
        if entry["players_min"] is None or entry["players_max"] is None or not entry["tags"]:
            pmin, pmax, dur = scrape_players(room.get("url") or room.get("schedule_url"))
            if entry["players_min"] is None and pmin:
                entry["players_min"], entry["players_max"] = pmin, pmax
                entry["source"] = entry["source"] or "provider page"
            if entry["duration_min"] is None and dur:
                entry["duration_min"] = dur
        if not entry["tags"] and room["id"] in MANUAL_TAGS:
            entry["tags"] = list(MANUAL_TAGS[room["id"]])
            entry["source"] = (entry["source"] or "") + "+manual" if entry["source"] else "manual"
        meta[room["id"]] = entry

    path = os.path.join(DATA, "room_meta.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)

    known_p = sum(1 for m in meta.values() if m["players_min"])
    known_t = sum(1 for m in meta.values() if m["tags"])
    print(f"\n{len(meta)} rooms -> {os.path.relpath(path, ROOT)}")
    print(f"  players known: {known_p}/{len(meta)}   genres known: {known_t}/{len(meta)}")
    for room in rooms:
        e = meta[room["id"]]
        p = f"{e['players_min']}-{e['players_max']}" if e["players_min"] else "?"
        print(f"  {room['id']:<52} {p:>5} p  {e['duration_min'] or '?':>3} min  "
              f"{','.join(e['tags']) or '-'}")


if __name__ == "__main__":
    main()
