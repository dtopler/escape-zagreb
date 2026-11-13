"""Catalogue: every escape room listed on escaperoomzagreb.com, normalized.

The page is a WordPress "banner grid": each card is a link to the provider's own
room page, followed by an <h4> (provider) and <h3> (room name).  The room pages
are where the actual schedules live, so the catalogue keeps that URL together
with an optional ``schedule_url`` when the booking page is a different one.
"""
from __future__ import annotations

import html
import json
import os
import re

from fetch import ROOT, fetch

AGG_URL = "https://www.escaperoomzagreb.com/escape-room-rezervacije/"

CARD_RE = re.compile(
    r'<a class="fill" href="(?P<href>[^"]+)"[^>]*>.*?</a>.*?'
    r'<h4[^>]*>(?P<provider>.*?)</h4>\s*'
    r'<h3[^>]*>(?P<name>.*?)</h3>',
    re.S,
)
TAG_RE = re.compile(r"<[^>]+>")

# host fragment -> (provider_id, display name)
PROVIDERS = {
    "cluego.eu": ("cluego", "ClueGo"),
    "escapeart.hr": ("escapeart", "Escape Art"),
    "theoldlockup.com": ("lockup", "The Old Lock Up"),
    "roomescape.hr": ("foxinabox", "Fox in a Box"),
    "elephantescape.hr": ("elephant", "Elephant in the Escape Room"),
    "enigmarium.hr": ("enigmarium", "Enigmarium"),
    "escape-arena.com": ("arena", "Escape Room Arena"),
    "portalescape.com": ("portal", "Portal Escape"),
    "exitgames.hr": ("exitgames", "Exit Games"),
}

# Explicit room names so the UI reads nicely instead of shouting the <h3>.
ROOM_NAME_OVERRIDES = {
    "cluego.eu/krada-mona-lise": "Krađa Mona Lise",
    "cluego.eu/tajna-profesora-balthazara": "Tajna profesora Baltazara",
    "escapeart.hr/magicna-knjiznica": "Magična knjižnica",
    "escapeart.hr/robin-hood": "Robin Hood",
    "escapeart.hr/drakuline-odaje": "Grof Dracula",
    "theoldlockup.com/room-detail.asp?roomid=4": "Mrtvačnica",
    "theoldlockup.com/room-single_haunted.html": "Ukleti hotel",
    "theoldlockup.com/room-detail.asp?roomid=3": "Kripta",
    "theoldlockup.com/room-detail.asp?roomid=1": "Kidnapped",
    "theoldlockup.com/room-detail.asp?roomid=5": "Presuda",
    "theoldlockup.com/room-detail.asp?roomid=7": "Soba panike",
    "theoldlockup.com/room-detail.asp?roomid=6": "Narcos",
    "theoldlockup.com/room-detail.asp?roomid=8": "Ritual",
    "theoldlockup.com/room-detail.asp?roomid=9": "Promatrači",
    "roomescape.hr/reservation/index/game/zodiac_killer": "Zodiac Killer",
    "roomescape.hr/reservation/index/game/prison/day/20": "Bijeg iz zatvora",
    # the directory still says "Pljačka banke"; the room is now a Wild West adventure
    "roomescape.hr/reservation/index/game/bank": "Golden Nugget Saloon",
    "escape-arena.com/game-rooms/amazing-heist": "The Amazing Heist",
    "escape-arena.com/game-rooms/secret-agent-room": "The Secret Agent Room",
    "elephantescape.hr/na%C5%A1e-sobe": "EX (IT) YOU",
    "exitgames.hr/booking-system/trinity": "Project G.A.R.Y.",
    "exitgames.hr/booking-system/grofica-iz-petrinjske-ulice": "Grofica iz Petrinjske",
    "exitgames.hr/booking-system/teslino-nasljede": "Tesla",
    "enigmarium.hr/tajanstveni-slucaj-zagrebacke-vjestice": "Grička vještica",
    "enigmarium.hr/unlock-zagreb": "Unlock Zagreb",
    "escape-arena.com/game-rooms/mind-crime-2": "Mind Crime",
    "escape-arena.com/game-rooms/the-office-of-john-monroe": "The Office of John Monroe",
    "escape-arena.com/game-rooms/lev-pasted": "The Lab of Dr. Lev Pasted",
    "portalescape.com/avantura-kocke": "Kocke",
    "portalescape.com/avanture/dvorac-crne-kraljice": "Crna kraljica",
    "portalescape.com/avanture/tajna-don-carla": "Tajna Don Carla",
    "portalescape.com/avanture/ozirisov-hram": "Ozirisov hram",
    "exitgames.hr/booking-system/trinity/": "A.I. Trinity: Zora robota",
}

# provider-specific fixes for stale/broken links on the aggregator page
SCHEDULE_URL_OVERRIDES = {
    # ClueGo lists marketing pages; the WooCommerce Bookings products live
    # under /reservation/ (and one under /rezervacije/).
    "cluego.eu/krada-mona-lise": "https://www.cluego.eu/reservation/mona-lisa-heist/",
    "cluego.eu/tajna-profesora-balthazara": "https://www.cluego.eu/rezervacije/tajna-profesora-baltazara/",
    "cluego.eu/memento": "https://www.cluego.eu/reservation/memento/",
    "cluego.eu/anno-1202": "https://www.cluego.eu/reservation/anno-1202/",
    # the aggregator links a stale /day/20 variant of the prison room
    "roomescape.hr/reservation/index/game/prison/day/20":
        "https://zagreb.roomescape.hr/reservation/index/game/prison",
    # The Old Lock Up: the room pages are marketing, the schedule is the ASP
    # booking form, one numeric room_id per room.
    "theoldlockup.com/room-detail.asp?roomid=1":
        "https://www.theoldlockup.com/booking-formb.asp?room_id=1",
    "theoldlockup.com/room-single_haunted.html":
        "https://www.theoldlockup.com/booking-formb.asp?room_id=2",
    "theoldlockup.com/room-detail.asp?roomid=3":
        "https://www.theoldlockup.com/booking-formb.asp?room_id=3",
    "theoldlockup.com/room-detail.asp?roomid=4":
        "https://www.theoldlockup.com/booking-formb.asp?room_id=4",
    "theoldlockup.com/room-detail.asp?roomid=5":
        "https://www.theoldlockup.com/booking-formb.asp?room_id=5",
    "theoldlockup.com/room-detail.asp?roomid=6":
        "https://www.theoldlockup.com/booking-formb.asp?room_id=6",
    "theoldlockup.com/room-detail.asp?roomid=7":
        "https://www.theoldlockup.com/booking-formb.asp?room_id=7",
    "theoldlockup.com/room-detail.asp?roomid=8":
        "https://www.theoldlockup.com/booking-formb.asp?room_id=8",
    "theoldlockup.com/room-detail.asp?roomid=9":
        "https://www.theoldlockup.com/booking-formb.asp?room_id=9",
}

# rooms the aggregator lists with a URL that no longer resolves
DEAD_LINKS = {
    "elephantescape.hr/na%C5%A1e-sobe":
        "https://www.elephantescape.hr/booking-calendar/ex-it-yu",
}


def clean(markup: str) -> str:
    text = html.unescape(TAG_RE.sub(" ", markup)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def pretty(name: str) -> str:
    if name.isupper() or name.islower():
        return name[:1].upper() + name[1:].lower()
    return name


def key_for(url: str) -> str:
    """host/path[?meaningful-query] used to look up overrides.

    Tracking parameters (gclid, utm_*) are stripped so the same room always maps
    to the same key; a real query such as ``?roomid=4`` is kept.
    """
    bare = re.sub(r"^https?://", "", url).strip()
    host, _, rest = bare.partition("/")
    labels = host.split(".")
    bare = ".".join(labels[-2:]) + ("/" + rest if rest else "")   # drop subdomains
    if "?" in bare:
        path, query = bare.split("?", 1)
        keep = [p for p in query.split("&")
                if p and not re.match(r"^(gclid|fbclid|utm_|_gl|mc_)", p)]
        bare = path + (f"?{'&'.join(keep)}" if keep else "")
    return bare.rstrip("/")


def provider_for(url: str) -> tuple[str, str]:
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    for fragment, val in PROVIDERS.items():
        if fragment in host:
            return val
    return ("unknown", host.replace("www.", ""))


def slug_for(provider_id: str, url: str) -> str:
    # keep meaningful query params (room_id=4) but drop tracking noise
    key = key_for(url)
    tail = key.split("/", 1)[1] if "/" in key else key
    slug = re.sub(r"[^a-z0-9]+", "-", tail.lower()).strip("-")
    return f"{provider_id}/{slug}"


def parse(body: str) -> list[dict]:
    rooms, seen, used_ids = [], set(), set()
    for match in CARD_RE.finditer(body):
        url = html.unescape(match.group("href")).strip()
        raw_name = clean(match.group("name"))
        if not url.startswith("http") or not raw_name:
            continue
        provider_id, provider_name = provider_for(url)
        key = key_for(url)
        if (provider_id, key) in seen:
            continue
        seen.add((provider_id, key))
        schedule_url = SCHEDULE_URL_OVERRIDES.get(key, DEAD_LINKS.get(key, url))
        room_id = slug_for(provider_id, url)
        if room_id in used_ids:                     # keep ids unique
            n = 2
            while f"{room_id}-{n}" in used_ids:
                n += 1
            room_id = f"{room_id}-{n}"
        used_ids.add(room_id)
        rooms.append({
            "id": room_id,
            "provider_id": provider_id,
            "provider": provider_name,
            "room": ROOM_NAME_OVERRIDES.get(key, pretty(raw_name)),
            "url": url,
            "schedule_url": schedule_url,
            "dead_link": key in DEAD_LINKS,
        })
    return rooms


# Rooms the directory page does not link, but that the providers' own booking
# engines do sell.  Each one below was verified by asking the engine for slots.
#   * Enigmarium's SimplyBook account exposes provider 7 / service 4
#     ("Laboratorij crne kraljice") next to the three linked rooms.
#   * The Old Lock Up's ASP calendar has 11 room_ids; the directory links 9.
#   * ClueGo's sitemap also has /reservation/sipsketch/ (a paint-and-sip event,
#     not an escape room) and the -hr/-2 duplicates of products we already have.
EXTRA_ROOMS = [
    {
        "provider_id": "cluego", "provider": "ClueGo", "room": "403 FORBIDDEN",
        "url": "https://www.cluego.eu/reservation/403-forbidden-kalendar/",
        "schedule_url": "https://www.cluego.eu/reservation/403-forbidden-kalendar/",
    },
    {
        "provider_id": "enigmarium", "provider": "Enigmarium",
        "room": "Laboratorij crne kraljice",
        "url": "https://enigmarium.hr/laboratorij-crne-kraljice/",
        "schedule_url": "https://enigmarium.hr/laboratorij-crne-kraljice/",
    },
    {
        "provider_id": "lockup", "provider": "The Old Lock Up", "room": "Circus",
        "url": "https://www.theoldlockup.com/room-detail.asp?roomid=10",
        "schedule_url": "https://www.theoldlockup.com/booking-formb.asp?room_id=10",
    },
    {
        "provider_id": "lockup", "provider": "The Old Lock Up", "room": "Missing",
        "url": "https://www.theoldlockup.com/room-detail.asp?roomid=11",
        "schedule_url": "https://www.theoldlockup.com/booking-formb.asp?room_id=11",
    },
]


def with_extras(rooms: list[dict]) -> list[dict]:
    """Append EXTRA_ROOMS, keeping ids unique."""
    used = {r["id"] for r in rooms}
    for extra in EXTRA_ROOMS:
        if any(r["provider_id"] == extra["provider_id"]
               and r["room"].lower() == extra["room"].lower() for r in rooms):
            continue
        room = dict(extra)
        room_id = slug_for(room["provider_id"], room["url"])
        if room_id in used:
            continue
        used.add(room_id)
        rooms.append({
            "id": room_id,
            "provider_id": room["provider_id"],
            "provider": room["provider"],
            "room": room["room"],
            "url": room["url"],
            "schedule_url": room["schedule_url"],
            "dead_link": False,
        })
    return rooms


def with_meta(rooms: list[dict]) -> list[dict]:
    """Attach players / duration / genres from data/room_meta.json when present."""
    path = os.path.join(ROOT, "data", "room_meta.json")
    try:
        with open(path, encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return rooms
    for room in rooms:
        entry = meta.get(room["id"]) or {}
        room["players_min"] = entry.get("players_min")
        room["players_max"] = entry.get("players_max")
        room["duration_min"] = entry.get("duration_min")
        room["tags"] = entry.get("tags") or []
    return rooms


def main() -> None:
    status, body = fetch(AGG_URL)
    assert status == 200, status
    rooms = with_meta(with_extras(parse(body)))
    out = os.path.join(ROOT, "data")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "rooms.json"), "w", encoding="utf-8") as fh:
        json.dump(rooms, fh, ensure_ascii=False, indent=2)
    by_provider: dict[str, list[dict]] = {}
    for room in rooms:
        by_provider.setdefault(room["provider"], []).append(room)
    print(f"{len(rooms)} rooms from {len(by_provider)} providers -> data/rooms.json")
    for provider, items in by_provider.items():
        print(f"\n{provider} ({len(items)})")
        for item in items:
            print(f"   {item['id']:<52} {item['room']}")


if __name__ == "__main__":
    main()
