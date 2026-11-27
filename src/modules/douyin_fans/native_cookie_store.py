"""Parse a bounded TTNet cookie store without executing serialized objects.

The parser accepts only the known SerializableHttpCookie wire structure.
It preserves cookie domains for a standard HTTP cookie jar, performs no I/O,
and never logs or persists cookie material. A parsed cookie is not evidence
that the server still authenticates its account.
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET


MAX_XML_BYTES = 2 * 1024 * 1024
MAX_COOKIE_HEX_CHARS = 128 * 1024
DEFAULT_ALLOWED_DOMAINS = ("snssdk.com", "aweme.snssdk.com")
_COOKIE_CLASS = "com.bytedance.frameworks.baselib.network.http.impl.SerializableHttpCookie"
_NON_STRING = object()


class CookieFormatError(ValueError):
    """The supplied data is not the supported, bounded cookie format."""


class _Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.handles = {}
        self.next_handle = 0x7E0000

    def read(self, length):
        if length < 0 or self.pos + length > len(self.data):
            raise CookieFormatError("Truncated cookie stream")
        value = self.data[self.pos:self.pos + length]
        self.pos += length
        return value

    def integer(self, length, *, signed=False):
        return int.from_bytes(self.read(length), "big", signed=signed)

    def utf(self):
        raw = self.read(self.integer(2))
        try:
            # Java modified UTF-8 encodes NUL as C0 80 and supplementary
            # characters as a pair of UTF-16 surrogates, not four-byte UTF-8.
            if b"\x00" in raw or any(byte >= 0xF0 for byte in raw):
                raise UnicodeError
            text = raw.replace(b"\xc0\x80", b"\x00").decode("utf-8", "surrogatepass")
            return text.encode("utf-16-be", "surrogatepass").decode("utf-16-be")
        except UnicodeError:
            raise CookieFormatError("Invalid cookie string encoding") from None

    def handle(self, value):
        self.handles[self.next_handle] = value
        self.next_handle += 1
        return value

    def value(self):
        token = self.integer(1)
        if token == 0x70:  # TC_NULL
            return None
        if token == 0x74:  # TC_STRING
            return self.handle(self.utf())
        if token == 0x71:  # TC_REFERENCE; class/object handles cannot be strings.
            value = self.handles.get(self.integer(4), _NON_STRING)
            if not isinstance(value, str):
                raise CookieFormatError("Invalid cookie string reference")
            return value
        raise CookieFormatError("Unsupported cookie value token")

    def block(self, expected):
        token = self.integer(1)
        if token == 0x77:  # TC_BLOCKDATA
            length = self.integer(1)
        elif token == 0x7A:  # TC_BLOCKDATALONG
            length = self.integer(4)
        else:
            raise CookieFormatError("Missing cookie primitive block")
        if length != expected:
            raise CookieFormatError("Invalid cookie primitive block length")
        return self.read(length)


def decode_cookie(encoded):
    """Decode one hexadecimal Java cookie stream into standard jar fields.

    No Java classes are instantiated. Only HTTP cookie-jar fields are returned;
    the Android discard/httpOnly flags are not needed by the HTTP transport.
    """
    if (not isinstance(encoded, str) or not encoded or len(encoded) > MAX_COOKIE_HEX_CHARS
            or len(encoded) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", encoded)):
        raise CookieFormatError("Invalid or oversized hexadecimal cookie stream")
    reader = _Reader(bytes.fromhex(encoded))
    if reader.read(6) != b"\xac\xed\x00\x05\x73\x72":
        raise CookieFormatError("Unsupported Java cookie stream")
    if reader.utf() != _COOKIE_CLASS:
        raise CookieFormatError("Unexpected serialized cookie class")
    reader.handle(_NON_STRING)
    reader.read(8)  # serialVersionUID; never used to instantiate a class.
    flags, count = reader.integer(1), reader.integer(2)
    if flags != 3 or count > 64:
        raise CookieFormatError("Unsupported cookie class descriptor")
    for _ in range(count):
        kind = chr(reader.integer(1))
        reader.utf()
        if kind in ("L", "["):
            if not isinstance(reader.value(), str):
                raise CookieFormatError("Invalid cookie field descriptor")
        elif kind not in "BCDFIJSZ":
            raise CookieFormatError("Invalid cookie field descriptor")
    if reader.read(2) != b"\x78\x70":
        raise CookieFormatError("Unsupported serialized cookie superclass")
    reader.handle(_NON_STRING)
    name, value, _comment, _comment_url, domain = [reader.value() for _ in range(5)]
    max_age = int.from_bytes(reader.block(8), "big", signed=True)
    path, _port_list = reader.value(), reader.value()
    tail = reader.block(15)
    # APK writeObject order: int version, boolean secure, boolean discard,
    # boolean httpOnly, long whenCreated. A version byte is not a flag.
    version = int.from_bytes(tail[:4], "big", signed=True)
    if version not in (0, 1) or any(flag not in (0, 1) for flag in tail[4:7]):
        raise CookieFormatError("Invalid cookie version or flags")
    when_created = int.from_bytes(tail[7:15], "big", signed=True)
    if reader.read(1) != b"\x78" or reader.pos != len(reader.data):
        raise CookieFormatError("Unexpected trailing cookie data")
    if not all(isinstance(field, str) for field in (name, value, domain)):
        raise CookieFormatError("Missing cookie name, value, or domain")
    if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
            or any(ord(char) < 32 or ord(char) == 127 or char == ";" for char in value)
            or not re.fullmatch(r"\.?[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*", domain)
            or (path is not None and (not isinstance(path, str) or not path.startswith("/")
                or any(ord(char) < 32 or ord(char) == 127 for char in path)))):
        raise CookieFormatError("Invalid cookie attributes")
    expires = (when_created / 1000 + max_age if max_age > 0 else
               0 if max_age == 0 else -1)
    return {"name": name, "value": value, "domain": domain, "path": path or "/",
            "secure": bool(tail[4]), "expires": expires}


def decode_store(xml_bytes, allowed_domains=DEFAULT_ALLOWED_DOMAINS, *, include_expired=False):
    """Read in-memory Android shared preferences; preserve exact cookie scope.

    Only explicitly allowed cookie domains are returned. The size bound is
    applied before XML parsing, and DTD/entity declarations are unsupported.
    Malformed matching entries fail closed with errors containing no data.
    """
    try:
        raw = xml_bytes.encode("utf-8") if isinstance(xml_bytes, str) else xml_bytes
    except UnicodeError:
        raise CookieFormatError("Invalid cookie store encoding") from None
    if not isinstance(raw, bytes) or len(raw) > MAX_XML_BYTES:
        raise CookieFormatError("Invalid or oversized cookie store XML")
    # Android preferences are UTF-8. Reject NUL-encoded UTF-16/32 input so a
    # differently encoded DTD cannot bypass the declaration check below.
    if b"\x00" in raw:
        raise CookieFormatError("Unsupported cookie store encoding")
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise CookieFormatError("Unsupported cookie store XML declaration")
    if not isinstance(allowed_domains, (tuple, list, set, frozenset)):
        raise CookieFormatError("Invalid allowed cookie domains")
    allowed = set()
    for domain in allowed_domains:
        if not isinstance(domain, str) or not re.fullmatch(
                r"[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*", domain):
            raise CookieFormatError("Invalid allowed cookie domains")
        allowed.add(domain.lower())
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError, LookupError):
        raise CookieFormatError("Invalid cookie store XML") from None
    if root.tag != "map":
        raise CookieFormatError("Unsupported cookie store XML root")
    result = []
    now = time.time()
    for entry in root:
        if entry.tag != "string":
            continue
        key = entry.attrib.get("name", "").lower()
        if not any(("//" + domain + "/|") in key or ("//." + domain + "/|") in key
                   for domain in allowed):
            continue
        cookie = decode_cookie(entry.text or "")
        if cookie["domain"].lstrip(".").lower() not in allowed:
            continue
        if not include_expired and 0 <= cookie["expires"] <= now:
            continue
        result.append(cookie)
    return result
