"""Tiny cached HTTP helper used during recon and by the scrapers."""
from __future__ import annotations

import gzip
import hashlib
import io
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(ROOT, "raw")
os.makedirs(RAW, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _slug(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def fetch(url: str, *, method: str = "GET", data: bytes | None = None,
          headers: dict | None = None, cache: bool = True,
          timeout: int = 25, retries: int = 1, tag: str = "") -> tuple[int, str]:
    """Return (status, text). Cached on disk when cache=True (recon convenience).

    ``timeout``/``retries`` bound how long a *failing* host can stall a scan:
    worst case ``timeout * (retries + 1) + 3`` seconds. Healthy calls answer in
    well under a second and are unaffected, so the only thing a bigger budget
    buys is a longer stall - on CI that showed up as a scan reaching 20/280
    room-days in ten minutes while one host sat in its retry ladder.
    """
    key = f"{_slug(url)}{'-' + tag if tag else ''}.txt"
    path = os.path.join(RAW, key)
    if cache and os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.readline().strip()
            body = fh.read()
        try:
            return int(head), body
        except ValueError:
            pass

    hdrs = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "hr,en-US;q=0.8,en;q=0.6",
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        hdrs.update(headers)

    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                text = raw.decode("utf-8", errors="replace")
                status = resp.status
                if cache:
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(f"{status}\n{text}")
                return status, text
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if cache:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(f"{exc.code}\n{body}")
            return exc.code, body
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"fetch failed for {url}: {last}")


def fetch_json(url: str, **kw):
    import json
    status, text = fetch(url, **kw)
    try:
        return status, json.loads(text)
    except Exception:  # noqa: BLE001
        return status, None


if __name__ == "__main__":
    import sys
    st, body = fetch(sys.argv[1], cache=False)
    print(st, len(body))
    print(body[:2000])
