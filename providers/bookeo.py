"""Bookeo customer-UI adapter — Portal Escape Zagreb (provider_id ``portal``).

Portal Escape embeds the Bookeo widget on every room page; behind the easyXDM
shim it is an ordinary server-rendered booking UI (HTML + one JSON endpoint),
so no JavaScript engine is needed.  Reverse-engineered from
``/bookeo/js-17.5.14/bundle_customer_ui2.js``.

Discovery
---------
1. Room page on portalescape.com embeds one script per room::

       <script src="https://bookeo.com/widget.js?a=3251YPPT4U15470C7B7CF&type=3251TH763915470E940B3"></script>

   ``a`` is the Bookeo account (widget id), ``type`` the product id of *that*
   room.  Both are cached per room on the adapter instance.
2. ``https://bookeo.com/widget.js?a=<account>`` exposes the account's shard::

       axiomct_frameUrl='https://www-254b.bookeo.com/bookeo/b_<account>_start.html?inwidget=true&a=<account>'

   Only the host matters; the ``t=`` token of ``axiomct_providerUrl`` turned
   out to be unnecessary, so it is not used.

Session bootstrap — three GETs, in this order, sharing one cookie jar
---------------------------------------------------------------------
* ``GET {host}/bookeo/-misc-widgetProvider.html?aguid=<account>&c=02a``
  sets the ``AXIOMID`` cookie (the "this booking UI lives in a widget" flag).
* ``GET {host}/bookeo/b_<account>_start.html?type=<product>``
  answers a ~8 kB "Pogreška" (error) page — but it is the request that
  creates the booking session.  Without it the next call fails.
* ``GET {host}/bookeo/b_<account>_start.html?type=<product>&inwidget=true&a=<account>``
  returns the time-selection page.  It carries ``cb_bsid``, the CSRF nonces
  ``_axiom_nocsrfid`` / ``_axiom_nocsrfid2``, ``cb_itemId``, the bookable
  window ``cbTimeFixed_Init(<today>, <first day>, <last day>, ...)`` and a
  zlib+base64 JSON blob (``cbTimeFixed_initialData``) holding the slots of the
  month that is currently on screen.

Availability for any month
--------------------------
The page pre-renders only the current month, so any other month costs one XHR
to Bookeo's DWR endpoint (exactly what the calendar's month arrows fire)::

    POST {host}/bookeo/srvrq
    Content-Type: text/plain
    X-Axiom-CFCH: <cb_itemId>
    body: <rsa_b64>_<aes_b64>

    plaintext {"provider":"DWRCustomerBookings","method":"getFixedSlots",
               "params":[bsid, month, year, numAdults, numChildren, numInfants,
                         "", privateEvent, recurrencyRule, nonce]}

* ``nonce`` is ``utils_getTime(<first of month>, ncs2 + "n", bsid)``: UTC
  midnight in milliseconds plus a rolling 16-bit hash of ``bsid + ncs2 + "n"``
  (see ``_time_signature``).
* the body is AES-256-CBC (PKCS#7) whose key is
  ``PBKDF2-HMAC-SHA1(<16 random chars>, <16 random chars>, 1 iteration, 32 B)``;
  that key plus the 16-char IV are RSA-PKCS#1-v1.5 wrapped under the public key
  the customer JS hardcodes (``RSA_PUBLIC_KEY``).
* the reply is a *zlib* stream (starts ``x\\x9c``) of JSON with the same
  per-day structure as ``cbTimeFixed_initialData`` — a list of
  ``{"date":{year,month,day},"slots":[...]}`` covering the whole month.

Traps
-----
* Skipping the public-page GET yields "Vaša sesija je bila neaktivna predugo
  vremena" (session expired), and skipping the provider GET as well yields the
  same, so the three calls must stay in order.
* ``getFixedSlots`` has no ``type`` parameter: the product is bound to the
  session, hence one session (``bsid``) per room.  ``bsid`` cannot be shared.
* Bookeo rate-limits bursts with a Google reCAPTCHA interstitial
  (``id="recaptchaContainer"``).  A browser solves it invisibly, a script
  cannot, so requests are paced (``_PAUSE``), bootstraps are serialized and a
  blocked bootstrap is retried once after a cool-down before raising
  ``AdapterError``.
* ``stopTime`` is start+15 min for *every* slot of *every* room — a slot-block
  marker, not the advertised 75-minute game length — so ``Slot.end`` is
  deliberately left ``None`` rather than reporting a misleading range.
* A slot is bookable only when ``available`` and not ``cancelled`` and not
  ``tooLateToBook`` (the latter renders a "call us" button, no online booking).
* ``price.currency`` comes back as ``"-"`` (account currency not set for the
  customer UI); prices are not exposed through ``Slot`` anyway.

Cost / politeness
-----------------
A cold scan costs 4 requests per room (room page for the product id, then
provider + public + widget page), 1 ``widget.js`` for the account and 1 srvrq
per room for every month that the page did not pre-render.  Measured over all
four rooms and the 31-day window: **21 requests, ~23 s**; a second pass over
the same days is served from memory (0 requests, 0.01 s).  Keep ``ADAPTER``
alive and ask it for many days instead of re-creating it, and leave a few
minutes between cold scans — that is what keeps Bookeo's captcha away.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

if __package__ in (None, ""):        # allow "python providers/bookeo.py" too
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fetch import UA, fetch
from providers.base import Adapter, AdapterError, Slot

# --- endpoints -------------------------------------------------------------
WIDGET_JS = "https://bookeo.com/widget.js?a={account}"
DEFAULT_HOST = "https://www-254b.bookeo.com"
PROVIDER_PATH = "/bookeo/-misc-widgetProvider.html?aguid={account}&c=02a"
START_PATH = "/bookeo/b_{account}_start.html"
SRVRQ_PATH = "/bookeo/srvrq"
REFERER = "https://portalescape.com/rezervacije/"

# --- tunables --------------------------------------------------------------
_PAUSE = 1.2          # seconds of politeness between Bookeo requests
_TIMEOUT = 45
_TRIES = 2            # session bootstrap attempts (captcha / expired pages)
_COOLDOWN = 15.0      # seconds to wait before retrying a blocked bootstrap

_WIDGET_SRC_RE = re.compile(
    r"bookeo\.com/widget\.js\?a=(?P<account>[0-9A-Z]+)"
    r"(?:&(?:amp;)?type=(?P<product>[0-9A-Z]+))?")
_FRAME_RE = re.compile(r"axiomct_frameUrl='(?P<url>[^']+)'")
_INIT_CALL_RE = re.compile(r"axiom_processInitCode\(\s*(?P<var>[A-Za-z_$][\w$]*)\s*\)")
_INIT_ASSIGN_RE = r"(?:var\s+)?{var}\s*=\s*(?P<expr>.*?);\s*return\s+axiom_processInitCode"
_INIT_ARGS_RE = re.compile(
    r"cbTimeFixed_Init\(\s*'(?P<today>[^']*)'\s*,\s*'(?P<first>[^']*)'\s*,\s*'(?P<last>[^']*)'")

# 3072-bit RSA key the customer UI uses to wrap the srvrq body key
# (rsaencrypt_jsEncrypter.setPublicKey in bundle_customer_ui2.js).
RSA_PUBLIC_KEY = (
    "MIIBojANBgkqhkiG9w0BAQEFAAOCAY8AMIIBigKCAYEAuszktR3WI1drsHi8QcZwDQtiL51SVV47WZ28"
    "jmQWUYZDXLX2DeqUgHzHidM4hl75xzldlNGLdBB0oH6Cm4uKsRuu0H0AivGxVUNqtIRAIx/k0+L0BQJ0I"
    "A66ZMUtfopBZtFTOlJYayFj3tleVFqS1dlec/w/Ul941LRhhhwoZc/ckAcw8anbyt5tnsZlOYq5zbz8Org"
    "cRpFHlQVvm/PasvVgula0T/Pb2bPjJbrnDMJYqjlLdwrU+6U2C/X9Fr73ZQOBqmWyv2cFrmaJvwQmlusc"
    "X8n3+7Mf1LUswi7tzgIIpwNmqQkyB36HBDt95uB5bD2mINN519wsDlAYuzIKE/aaGaw261z09uumUcsj3"
    "NHB9+IAC7CWzrM5i4onVYZk5aEln2TR+V6w64TMJ7IOyRUs5kuxJ6/PdDz+I7a7euJrHfGnXEN4BY/trs"
    "b8rbyNLPefrqPIZE67Nq+5XJXscUd1osfpt5ylXniaZtSxreJIUDwDrT/9s4MYqwf1LuaXAgMBAAE="
)

_KEY_ALPHABET = "ACEFHJKLMNPRTUWXY"          # utils_generateRandomKey alphabet
_CTX = ssl.create_default_context()          # mirrors fetch.py's tolerance
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


class _Busy(Exception):
    """Retryable Bookeo answer: captcha interstitial or dead session."""


# ---------------------------------------------------------------------------
# tiny helpers
# ---------------------------------------------------------------------------
def _js_unescape(text: str) -> str:
    """Undo the escapes the page leaves inside the base64 JS string literals."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(text):
            break
        esc = text[i]
        if esc == "x" and i + 2 < len(text):
            out.append(chr(int(text[i + 1:i + 3], 16)))
            i += 3
        elif esc == "u" and i + 4 < len(text):
            out.append(chr(int(text[i + 1:i + 5], 16)))
            i += 5
        else:
            out.append({"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(esc, esc))
            i += 1
    return "".join(out)


def _init_data(page: str) -> list[dict]:
    """Decode the ``cbTimeFixed_initialData`` blob (base64 -> zlib -> JSON)."""
    call = _INIT_CALL_RE.search(page)
    if not call:
        return []
    var = re.escape(call.group("var"))
    assign = re.search(_INIT_ASSIGN_RE.format(var=var), page, re.S)
    if not assign:
        return []
    try:
        blob = "".join(_js_unescape(part)
                       for part in re.findall(r'"((?:[^"\\]|\\.)*)"', assign.group("expr")))
        payload = json.loads(zlib.decompress(base64.b64decode(blob)).decode("utf-8"))
    except (ValueError, zlib.error):
        return []          # the srvrq path below still covers the whole month
    return payload if isinstance(payload, list) else []


def _hhmm(value) -> str | None:
    """``{"hour":9,"minutes":5}`` -> ``"09:05"``."""
    if not isinstance(value, dict):
        return None
    try:
        hour, minute = int(value["hour"]), int(value["minutes"])
    except (KeyError, TypeError, ValueError):
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return None


def _day_key(date_value) -> str | None:
    if not isinstance(date_value, dict):
        return None
    try:
        return (f"{int(date_value['year']):04d}-{int(date_value['month']):02d}"
                f"-{int(date_value['day']):02d}")
    except (KeyError, TypeError, ValueError):
        return None


def _by_day(rows) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = _day_key(row.get("date"))
        if key:
            out[key] = row.get("slots") or []
    return out


def _get_text(url: str) -> str:
    """Plain GET for the marketing pages / widget.js (no cookies needed)."""
    try:
        status, text = fetch(url, cache=False, timeout=_TIMEOUT)
    except RuntimeError as exc:
        raise AdapterError(f"portal: {exc}") from None
    if status >= 400:
        raise AdapterError(f"portal: {url} -> HTTP {status}")
    return text


# ---------------------------------------------------------------------------
# request envelope for the srvrq (DWR) endpoint
# ---------------------------------------------------------------------------
_SBOX = bytes([
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
])
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36, 0x6c, 0xd8, 0xab, 0x4d]


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x11B) if a & 0x100 else a


def _gmul(a: int, b: int) -> int:
    out = 0
    while b:
        if b & 1:
            out ^= a
        a = _xtime(a)
        b >>= 1
    return out


def _aes_round_keys(key: bytes) -> list[list[int]]:
    nk, rounds = len(key) // 4, len(key) // 4 + 6
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (rounds + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = [_SBOX[b] for b in temp[1:] + temp[:1]]
            temp[0] ^= _RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            temp = [_SBOX[b] for b in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    return [sum(words[4 * r:4 * r + 4], []) for r in range(rounds + 1)]


def _aes_block(round_keys: list[list[int]], block: bytes) -> bytes:
    rounds = len(round_keys) - 1
    state = [block[i] ^ round_keys[0][i] for i in range(16)]
    for rnd in range(1, rounds + 1):
        state = [_SBOX[b] for b in state]
        state = [state[((c + r) % 4) * 4 + r] for c in range(4) for r in range(4)]
        if rnd != rounds:
            mixed = []
            for c in range(4):
                col = state[c * 4:c * 4 + 4]
                mixed += [_gmul(col[0], 2) ^ _gmul(col[1], 3) ^ col[2] ^ col[3],
                          col[0] ^ _gmul(col[1], 2) ^ _gmul(col[2], 3) ^ col[3],
                          col[0] ^ col[1] ^ _gmul(col[2], 2) ^ _gmul(col[3], 3),
                          _gmul(col[0], 3) ^ col[1] ^ col[2] ^ _gmul(col[3], 2)]
            state = mixed
        state = [state[i] ^ round_keys[rnd][i] for i in range(16)]
    return bytes(state)


def _aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-256-CBC + PKCS#7, the padding CryptoJS adds."""
    round_keys = _aes_round_keys(key)
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    out, prev = bytearray(), iv
    for i in range(0, len(data), 16):
        prev = _aes_block(round_keys, bytes(a ^ b for a, b in zip(data[i:i + 16], prev)))
        out += prev
    return bytes(out)


def _der_tlv(buf: bytes, pos: int) -> tuple[int, bytes, int]:
    tag = buf[pos]
    pos += 1
    length = buf[pos]
    pos += 1
    if length & 0x80:
        width = length & 0x7F
        length = int.from_bytes(buf[pos:pos + width], "big")
        pos += width
    return tag, buf[pos:pos + length], pos + length


def _rsa_key() -> tuple[int, int]:
    """Modulus/exponent of the SPKI key the booking JS embeds."""
    der = base64.b64decode(RSA_PUBLIC_KEY)
    _, outer, _ = _der_tlv(der, 0)                    # SubjectPublicKeyInfo
    _, _, pos = _der_tlv(outer, 0)                    # AlgorithmIdentifier
    _, bitstring, _ = _der_tlv(outer, pos)            # BIT STRING
    _, sequence, _ = _der_tlv(bitstring[1:], 0)       # RSAPublicKey
    _, modulus, pos = _der_tlv(sequence, 0)
    _, exponent, _ = _der_tlv(sequence, pos)
    return int.from_bytes(modulus, "big"), int.from_bytes(exponent, "big")


def _rsa_pkcs1_v15(message: bytes) -> bytes:
    modulus, exponent = _rsa_key()
    size = (modulus.bit_length() + 7) // 8
    padding = bytearray()
    while len(padding) < size - len(message) - 3:
        byte = secrets.randbelow(255) + 1             # PKCS#1: never 0x00
        padding.append(byte)
    block = b"\x00\x02" + bytes(padding) + b"\x00" + message
    return pow(int.from_bytes(block, "big"), exponent, modulus).to_bytes(size, "big")


def _envelope(plaintext: str) -> str:
    """Port of rsaencrypt_encrypt2(): ``rsa_b64 + "_" + aes_b64``."""
    iv = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(16))
    salt = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(16))
    key = hashlib.pbkdf2_hmac("sha1", salt.encode(), iv.encode(), 1, 32)
    wrapped = _rsa_pkcs1_v15(iv.encode() + key.hex().encode())
    body = _aes_cbc_encrypt(key, iv.encode(), plaintext.encode())
    return f"{base64.b64encode(wrapped).decode()}_{base64.b64encode(body).decode()}"


def _inflate(raw: bytes) -> str:
    for wbits in (15, -15):
        try:
            return zlib.decompress(raw, wbits).decode("utf-8")
        except (zlib.error, UnicodeDecodeError):
            continue
    raise AdapterError(f"bookeo: srvrq reply is not deflate data ({raw[:16]!r})")


def _time_signature(year: int, month: int, bsid: str, nonce: str) -> int:
    """Port of ``utils_getTime(<1st of month>, nonce + "n", bsid)``."""
    midnight = int(dt.datetime(year, month, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
    rolling = 0
    for char in f"{bsid}{nonce}n":
        word = (32 * rolling + ord(char)) & 0xFFFFFFFF
        rolling = abs(((word ^ 0x80000000) - 0x80000000) | 32)   # JS |32 + Math.abs
    return midnight + rolling % 65535


# ---------------------------------------------------------------------------
# one booking session (= one room / product)
# ---------------------------------------------------------------------------
class _Widget:
    """A Bookeo booking session for a single product, plus its month cache."""

    def __init__(self, host: str, account: str, product: str):
        self.host = host.rstrip("/")
        self.account = account
        self.product = product
        self.bsid = ""
        self.nonce = ""
        self.item = ""
        self.first_day = ""
        self.last_day = ""
        self._months: dict[str, dict[str, list[dict]]] = {}
        self._cache_lock = threading.Lock()
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=_CTX))
        self._open()

    # -- urls ---------------------------------------------------------------
    @property
    def start_url(self) -> str:
        return self.host + START_PATH.format(account=self.account)

    @property
    def public_url(self) -> str:
        return f"{self.start_url}?type={self.product}"

    @property
    def widget_url(self) -> str:
        return f"{self.start_url}?type={self.product}&inwidget=true&a={self.account}"

    # -- transport ----------------------------------------------------------
    def _get(self, url: str) -> str:
        time.sleep(_PAUSE)
        request = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "hr,en-US;q=0.8,en;q=0.6",
            "Referer": REFERER,
        })
        try:
            with self.opener.open(request, timeout=_TIMEOUT) as response:
                status, body = response.status, response.read()
        except urllib.error.HTTPError as exc:
            raise AdapterError(f"portal: {url} -> HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise AdapterError(f"portal: {url} -> {exc.reason}") from None
        if status >= 500:
            raise AdapterError(f"portal: {url} -> HTTP {status}")
        return body.decode("utf-8", "replace")

    def _post(self, body: str) -> bytes:
        time.sleep(_PAUSE)
        request = urllib.request.Request(
            self.host + SRVRQ_PATH, data=body.encode(), method="POST",
            headers={"User-Agent": UA, "Content-Type": "text/plain", "Accept": "*/*",
                     "Origin": self.host, "Referer": self.widget_url,
                     "X-Axiom-CFCH": self.item})
        try:
            with self.opener.open(request, timeout=_TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise AdapterError(f"portal: srvrq -> HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise AdapterError(f"portal: srvrq -> {exc.reason}") from None

    # -- session bootstrap --------------------------------------------------
    def _open(self) -> None:
        problem = "unknown"
        for attempt in range(_TRIES):
            if attempt:
                time.sleep(_COOLDOWN)
                self.jar = http.cookiejar.CookieJar()
                self.opener = urllib.request.build_opener(
                    urllib.request.HTTPCookieProcessor(self.jar),
                    urllib.request.HTTPSHandler(context=_CTX))
                self._months.clear()
            try:
                self._bootstrap()
                return
            except _Busy as exc:
                problem = str(exc)
        raise AdapterError(f"portal: cannot open a Bookeo session ({problem})")

    def _bootstrap(self) -> None:
        self._get(self.host + PROVIDER_PATH.format(account=self.account))
        self._get(self.public_url)
        self._absorb(self._get(self.widget_url))

    def _absorb(self, page: str) -> None:
        if "recaptchaContainer" in page:
            raise _Busy("Bookeo answered with its reCAPTCHA interstitial (rate-limited)")
        if "bila neaktivna" in page or "b_sessionExpired" in page:
            raise _Busy("booking session expired")
        bsid = re.search(r'cb_bsid\s*=\s*"([^"]*)"', page)
        if not bsid:
            raise AdapterError("portal: Bookeo time page changed (no cb_bsid)")
        self.bsid = bsid.group(1)
        nonce = re.search(r'_axiom_nocsrfid2\s*=\s*"([^"]*)"', page)
        self.nonce = nonce.group(1) if nonce else ""
        item = re.search(r'cb_itemId\s*=\s*"([^"]*)"', page)
        self.item = item.group(1) if item else ""
        window = _INIT_ARGS_RE.search(page)
        if window:
            self.first_day = window.group("first")
            self.last_day = window.group("last")
        days = _by_day(_init_data(page))
        for day, slots in days.items():
            self._months.setdefault(day[:7], {})[day] = slots

    # -- availability -------------------------------------------------------
    def month(self, prefix: str) -> dict[str, list[dict]]:
        cached = self._months.get(prefix)
        if cached is not None:
            return cached
        with self._cache_lock:
            cached = self._months.get(prefix)
            if cached is None:
                cached = self._fetch_month(int(prefix[:4]), int(prefix[5:7]))
                self._months[prefix] = cached
        return cached

    def _fetch_month(self, year: int, month: int) -> dict[str, list[dict]]:
        payload = {
            "provider": "DWRCustomerBookings",
            "method": "getFixedSlots",
            "params": [self.bsid, month, year, 0, 0, 0, "", False, None,
                       _time_signature(year, month, self.bsid, self.nonce)],
        }
        text = _inflate(self._post(_envelope(json.dumps(payload))))
        try:
            data = json.loads(text)
        except ValueError:
            raise AdapterError(f"portal: srvrq returned non-JSON ({text[:120]!r})") from None
        if isinstance(data, dict) and data.get("AJAXERR"):
            message = str((data.get("exception") or {}).get("message") or "unknown")
            if "ession" in message or "ookie" in message:
                raise _Busy(f"srvrq: {message}")
            raise AdapterError(f"portal: srvrq error - {message}")
        if not isinstance(data, list):
            raise AdapterError("portal: srvrq returned an unexpected payload")
        return _by_day(data)

    def slots(self, day: str) -> list[Slot]:
        if self.first_day and day < self.first_day:
            return []
        if self.last_day and day > self.last_day:
            return []
        out: list[Slot] = []
        seen: set[str] = set()
        for raw in self.month(day[:7]).get(day, ()):
            if not isinstance(raw, dict):
                continue
            if not raw.get("available") or raw.get("cancelled") or raw.get("tooLateToBook"):
                continue
            start = _hhmm(raw.get("startTime"))
            if not start or start in seen:
                continue
            seen.add(start)
            out.append(Slot(time=start))
        out.sort(key=lambda slot: slot.time)
        return out


# ---------------------------------------------------------------------------
# adapter
# ---------------------------------------------------------------------------
class BookeoAdapter(Adapter):
    provider_id = "portal"
    provider_name = "Portal Escape"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._setup_lock = threading.Lock()   # serialize session bootstraps
        self._products: dict[str, tuple[str, str]] = {}
        self._hosts: dict[str, str] = {}
        self._widgets: dict[str, _Widget] = {}

    # -- discovery ----------------------------------------------------------
    def _product(self, room: dict) -> tuple[str, str]:
        key = room.get("id") or room.get("url") or ""
        with self._lock:
            known = self._products.get(key)
        if known:
            return known
        account = product = ""
        for url in (room.get("schedule_url"), room.get("url")):
            if not url:
                continue
            for match in _WIDGET_SRC_RE.finditer(_get_text(url)):
                if match.group("product"):
                    account, product = match.group("account"), match.group("product")
                    break
                account = account or match.group("account")
            if product:
                break
        if not product:
            raise AdapterError(f"portal: no Bookeo product id on {room.get('url')}")
        with self._lock:
            self._products[key] = (account, product)
        return account, product

    def _host(self, account: str) -> str:
        with self._lock:
            known = self._hosts.get(account)
        if known:
            return known
        host = DEFAULT_HOST
        try:
            match = _FRAME_RE.search(_get_text(WIDGET_JS.format(account=account)))
            if match:
                parts = urllib.parse.urlsplit(match.group("url"))
                if parts.scheme and parts.netloc:
                    host = f"{parts.scheme}://{parts.netloc}"
        except AdapterError:
            pass                                   # fall back to the known shard
        with self._lock:
            self._hosts[account] = host
        return host

    # -- session ------------------------------------------------------------
    def _widget(self, room: dict) -> _Widget:
        key = room.get("id") or room.get("url") or ""
        with self._lock:
            widget = self._widgets.get(key)
        if widget is not None:
            return widget
        # one bootstrap at a time: parallel ones look exactly like the bursts
        # Bookeo answers with a reCAPTCHA interstitial
        with self._setup_lock:
            with self._lock:
                widget = self._widgets.get(key)
            if widget is None:
                account, product = self._product(room)
                widget = _Widget(self._host(account), account, product)
                with self._lock:
                    self._widgets[key] = widget
        return widget

    def slots(self, room: dict, day: str) -> list[Slot]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
            raise AdapterError(f"portal: expected YYYY-MM-DD, got {day!r}")
        try:
            dt.date.fromisoformat(day)
        except ValueError:
            raise AdapterError(f"portal: not a real calendar date: {day!r}") from None
        key = room.get("id") or room.get("url") or ""
        for attempt in (0, 1):
            widget = self._widget(room)
            try:
                return widget.slots(day)
            except _Busy as exc:
                with self._lock:
                    self._widgets.pop(key, None)
                if attempt:
                    raise AdapterError(
                        f"portal: {room.get('room', key)} - {exc}") from None
                time.sleep(_PAUSE * 10)
        return []                                   # pragma: no cover


ADAPTER = BookeoAdapter()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "data", "rooms.json"), encoding="utf-8") as handle:
        rooms = [room for room in json.load(handle) if room["provider_id"] == "portal"]

    today = dt.date.today()
    days = [(today + dt.timedelta(days=offset)).isoformat() for offset in (0, 1, 2, 3, 22)]

    print(f"today={today}  rooms={len(rooms)}  adapter={ADAPTER!r}\n")
    failures = 0
    for room in rooms:
        try:
            account, product = ADAPTER._product(room)
            print(f"{room['room']:<16} account={account} product={product}")
            for day in days:
                found = ADAPTER.slots(room, day)
                label = ", ".join(slot.time for slot in found) or "-"
                print(f"    {day}  {len(found):2d}  {label}")
        except AdapterError as exc:
            failures += 1
            print(f"{room['room']:<16} ERROR {exc}")
    print(f"\n{len(rooms)} rooms checked, {failures} failed")
