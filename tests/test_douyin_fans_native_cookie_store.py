"""Constructed Java cookie streams only; never read a device or account file."""
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.douyin_fans.native_cookie_store import (
    CookieFormatError, DEFAULT_ALLOWED_DOMAINS, MAX_COOKIE_HEX_CHARS, MAX_XML_BYTES,
    decode_cookie, decode_store,
)


COOKIE_CLASS = "com.bytedance.frameworks.baselib.network.http.impl.SerializableHttpCookie"


def utf(text):
    # ASCII suffices for the deliberately constructed HTTP cookie attributes.
    raw = text.encode("utf-8")
    return len(raw).to_bytes(2, "big") + raw


def string(text):
    return b"\x70" if text is None else b"\x74" + utf(text)


def cookie_stream(*, name="sessionid", value="offline-value", domain=".snssdk.com", path="/",
                  max_age=600, created_ms=1_000_000, secure=1, cookie_class=COOKIE_CLASS,
                  version=1, discard=0, http_only=0, flags=3,
                  name_token=None, value_token=None, long_blocks=False):
    def block(raw):
        return (b"\x7a" + len(raw).to_bytes(4, "big") if long_blocks else
                b"\x77" + bytes([len(raw)])) + raw

    header = b"\xac\xed\x00\x05\x73\x72" + utf(cookie_class) + b"\0" * 8
    descriptor = bytes([flags]) + b"\0\0\x78\x70"
    fields = ((string(name) if name_token is None else name_token)
              + (string(value) if value_token is None else value_token)
              + string(None) + string(None) + string(domain))
    tail = (version.to_bytes(4, "big", signed=True) + bytes([secure, discard, http_only])
            + created_ms.to_bytes(8, "big", signed=True))
    return (header + descriptor + fields + block(max_age.to_bytes(8, "big", signed=True))
            + string(path) + string(None) + block(tail) + b"\x78")


def store(*entries):
    return ("<map>" + "".join('<string name="' + key + '">' + encoded + '</string>'
                              for key, encoded in entries) + "</map>").encode("utf-8")


class NativeCookieStoreTests(unittest.TestCase):
    def test_known_cookie_returns_only_confirmed_jar_fields_and_original_domain(self):
        cookie = decode_cookie(cookie_stream().hex())
        self.assertEqual(cookie, {"name": "sessionid", "value": "offline-value",
                                 "domain": ".snssdk.com", "path": "/",
                                 "secure": True, "expires": 1600.0})
        self.assertNotIn("httpOnly", cookie)

    def test_tail_version_precedes_secure_and_other_boolean_flags(self):
        data = cookie_stream(version=1, secure=1, discard=1, http_only=1)
        self.assertTrue(decode_cookie(data.hex())["secure"])
        self.assertFalse(decode_cookie(cookie_stream(version=1, secure=0).hex())["secure"])
        self.assertTrue(decode_cookie(cookie_stream(version=0, secure=1).hex())["secure"])
        self.assertNotIn("httpOnly", decode_cookie(data.hex()))

    def test_long_primitive_blocks_and_string_references_are_supported(self):
        name_reference = b"\x71" + (0x7E0002).to_bytes(4, "big")
        cookie = decode_cookie(cookie_stream(value_token=name_reference, long_blocks=True).hex())
        self.assertEqual(cookie["value"], "sessionid")

    def test_reference_to_class_object_or_missing_handle_is_rejected(self):
        for handle in (0x7E0000, 0x7E0001, 0x7E9999):
            with self.subTest(handle=handle), self.assertRaises(CookieFormatError):
                decode_cookie(cookie_stream(value_token=b"\x71" + handle.to_bytes(4, "big")).hex())

    def test_wrong_class_flags_primitive_blocks_and_tokens_are_rejected(self):
        cases = [cookie_stream(cookie_class="java.lang.Runtime"), cookie_stream(flags=2),
                 cookie_stream(name_token=b"\x73"), cookie_stream(secure=2),
                 cookie_stream(version=2), cookie_stream(discard=2), cookie_stream(http_only=2)]
        good = cookie_stream()
        cases.extend([b"\0" + good[1:], good.replace(b"\x77\x08", b"\x77\x07", 1),
                      good + b"\0", good[:-1] + b"\x70"])
        for data in cases:
            with self.subTest(size=len(data)), self.assertRaises(CookieFormatError):
                decode_cookie(data.hex())

    def test_truncated_streams_fail_with_controlled_errors(self):
        good = cookie_stream()
        for end in (0, 1, 5, 7, 20, len(good) - 20, len(good) - 1):
            with self.subTest(end=end), self.assertRaises(CookieFormatError):
                decode_cookie(good[:end].hex())

    def test_hex_input_is_bounded_and_strict(self):
        for value in (None, b"00", "", "1", "not-hex", "00 00", "0" * (MAX_COOKIE_HEX_CHARS + 2)):
            with self.subTest(kind=type(value).__name__), self.assertRaises(CookieFormatError):
                decode_cookie(value)

    def test_default_domains_are_explicit_and_never_rebound(self):
        self.assertEqual(DEFAULT_ALLOWED_DOMAINS, ("snssdk.com", "aweme.snssdk.com"))
        xml = store(("https://.snssdk.com/|sessionid", cookie_stream().hex()),
                    ("https://aweme.snssdk.com/|sessionid", cookie_stream(domain="aweme.snssdk.com").hex()),
                    ("https://evil.snssdk.com/|sessionid", cookie_stream(domain="evil.snssdk.com").hex()))
        with patch("modules.douyin_fans.native_cookie_store.time.time", return_value=1100):
            self.assertEqual([c["domain"] for c in decode_store(xml)],
                             [".snssdk.com", "aweme.snssdk.com"])

    def test_key_cannot_smuggle_a_cookie_with_another_serialized_domain(self):
        xml = store(("https://.snssdk.com/|sessionid", cookie_stream(domain="evil.example").hex()))
        self.assertEqual(decode_store(xml), [])
        other = store(("https://fandom.starplaylet.com/|sessionid",
                       cookie_stream(domain="fandom.starplaylet.com", max_age=-1).hex()))
        self.assertEqual(decode_store(other), [])
        self.assertEqual(decode_store(other, ("fandom.starplaylet.com",))[0]["domain"],
                         "fandom.starplaylet.com")

    def test_expiry_filters_past_and_exact_boundary_but_preserves_session_cookie(self):
        entries = [cookie_stream(max_age=99), cookie_stream(max_age=100),
                   cookie_stream(max_age=101), cookie_stream(max_age=-1)]
        xml = store(*[("https://.snssdk.com/|cookie" + str(i), data.hex())
                      for i, data in enumerate(entries)])
        with patch("modules.douyin_fans.native_cookie_store.time.time", return_value=1100):
            self.assertEqual([c["expires"] for c in decode_store(xml)], [1101, -1])
            self.assertEqual(len(decode_store(xml, include_expired=True)), 4)

    def test_zero_max_age_is_always_expired_even_with_future_creation_time(self):
        xml = store(("https://.snssdk.com/|sessionid",
                     cookie_stream(max_age=0, created_ms=9_999_999_000).hex()))
        self.assertEqual(decode_store(xml), [])
        self.assertEqual(decode_store(xml, include_expired=True)[0]["expires"], 0)

    def test_null_path_defaults_to_root_and_cookie_attributes_are_validated(self):
        self.assertEqual(decode_cookie(cookie_stream(path=None).hex())["path"], "/")
        for kwargs in ({"name": "bad;name"}, {"name": "bad=name"},
                       {"value": "bad\r\nvalue"}, {"value": "bad;value"},
                       {"domain": "https://snssdk.com"}, {"path": "relative"},
                       {"path": "/bad\npath"}, {"domain": None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CookieFormatError):
                decode_cookie(cookie_stream(**kwargs).hex())

    def test_xml_bounds_invalid_roots_and_entities_are_rejected_without_output(self):
        output = io.StringIO()
        cases = [b"x" * (MAX_XML_BYTES + 1), b"<map>", b"<secret/>", None,
                 b'<!DOCTYPE map [<!ENTITY secret "offline-secret">]><map/>',
                 '<!DOCTYPE map [<!ENTITY secret "offline-secret">]><map/>'.encode("utf-16"),
                 b'<?xml version="1.0" encoding="offline-secret"?><map/>',
                 b"<map><string name='https://snssdk.com/|secret'>offline-secret</string></map>"]
        with redirect_stdout(output), redirect_stderr(output):
            for raw in cases:
                with self.subTest(kind=type(raw).__name__), self.assertRaises(CookieFormatError) as error:
                    decode_store(raw)
                self.assertNotIn("offline-secret", str(error.exception))
        self.assertEqual(output.getvalue(), "")

    def test_empty_map_and_unrelated_preferences_do_not_decode_entries(self):
        self.assertEqual(decode_store(b"<map/>"), [])
        self.assertEqual(decode_store(b'<map><int name="count" value="1"/>'
                                     b'<string name="https://other.example/|x">not-hex</string></map>'), [])

    def test_allowed_domains_cannot_be_a_string_url_or_wildcard(self):
        for domains in ("snssdk.com", ("https://snssdk.com",), ("*.snssdk.com",), (True,)):
            with self.subTest(domains=domains), self.assertRaises(CookieFormatError):
                decode_store(b"<map/>", domains)


if __name__ == "__main__":
    unittest.main()
