"""Print the catalogue (id / room / provider / schedule url)."""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

rooms = json.load(open("data/rooms.json", encoding="utf-8"))
only = sys.argv[1:] or None
for r in rooms:
    if only and r["provider_id"] not in only:
        continue
    print(f"{r['id']:<50} {r['room'][:26]:<27} {r['schedule_url']}")
print(f"\n{len(rooms)} rooms")
