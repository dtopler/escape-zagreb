"""Measure how fast a runner can reach each booking host.

Run manually via the `probe` workflow. Prints DNS / TCP / TLS / HTTP timings per
host so a slow scan can be blamed on the network instead of guessed at.
"""
import collections
import json
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

hosts: collections.Counter = collections.Counter()
for room in json.load(open("data/rooms.json", encoding="utf-8")):
    url = room.get("schedule_url") or room.get("url") or ""
    if url.startswith("http"):
        hosts[urllib.parse.urlparse(url).netloc] += 1

print(f"{'host':<40} {'dns':>7} {'tcp':>7} {'tls':>7} {'http':>7} {'total':>8}  result")
print("-" * 100)

for host, rooms in hosts.most_common():
    t0 = time.perf_counter()
    dns = tcp = tls = http = 0.0
    result = ""
    try:
        socket.gethostbyname(host)
        dns = time.perf_counter() - t0
        with socket.create_connection((host, 443), timeout=10) as sock:
            tcp = time.perf_counter() - t0
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with ctx.wrap_socket(sock, server_hostname=host):
                tls = time.perf_counter() - t0
        req = urllib.request.Request(
            f"https://{host}/", headers={"User-Agent": "escape-probe/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read(4096)
            http = time.perf_counter() - t0
            result = f"HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001
        result = f"{type(exc).__name__}: {str(exc)[:60]}"
    total = time.perf_counter() - t0
    print(f"{host:<40} {dns*1000:6.0f}ms {tcp*1000:6.0f}ms {tls*1000:6.0f}ms "
          f"{http*1000:6.0f}ms {total*1000:7.0f}ms  {result}  (rooms={rooms})")
