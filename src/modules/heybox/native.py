"""Heybox Android request signing for APP 1.3.398 / shader 14.

The signer is a Python reimplementation of the protocol behavior documented by
Cansiny0320/heybox-daily (MIT), with deterministic compatibility vectors kept
in tests/test_heybox_core.py. It is intentionally version-pinned: changing
the app build requires new verified vectors.
"""
from __future__ import annotations

import datetime as _datetime
import hashlib
import hmac
import secrets
import time

MASK = (1 << 64) - 1
_K = (
    0x428a2f98d728ae22, 0x7137449123ef65cd, 0xb5c0fbcfec4d3b2f, 0xe9b5dba58189dbbc,
    0x3956c25bf348b538, 0x59f111f1b605d019, 0x923f82a4af194f9b, 0xab1c5ed5da6d8118,
    0xd807aa98a3030242, 0x12835b0145706fbe, 0x243185be4ee4b28c, 0x550c7dc3d5ffb4e2,
    0x72be5d74f27b896f, 0x80deb1fe3b1696b1, 0x9bdc06a725c71235, 0xc19bf174cf692694,
    0xe49b69c19ef14ad2, 0xefbe4786384f25e3, 0xfc19dc68b8cd5b5, 0x240ca1cc77ac9c65,
    0x2de92c6f592b0275, 0x4a7484aa6ea6e483, 0x5cb0a9dcbd41fbd4, 0x76f988da831153b5,
    0x983e5152ee66dfab, 0xa831c66d2db43210, 0xb00327c898fb213f, 0xbf597fc7beef0ee4,
    0xc6e00bf33da88fc2, 0xd5a79147930aa725, 0x6ca6351e003826f, 0x142929670a0e6e70,
    0x27b70a8546d22ffc, 0x2e1b21385c26c926, 0x4d2c6dfc5ac42aed, 0x53380d139d95b3df,
    0x650a73548baf63de, 0x766a0abb3c77b2a8, 0x81c2c92e47edaee6, 0x92722c851482353b,
    0xa2bfe8a14cf10364, 0xa81a664bbc423001, 0xc24b8b70d0f89791, 0xc76c51a30654be30,
    0xd192e819d6ef5218, 0xd69906245565a910, 0xf40e35855771202a, 0x106aa07032bbd1b8,
    0x19a4c116b8d2d0c8, 0x1e376c085141ab53, 0x2748774cdf8eeb99, 0x34b0bcb5e19b48a8,
    0x391c0cb3c5c95a63, 0x4ed8aa4ae3418acb, 0x5b9cca4f7763e373, 0x682e6ff3d6b2b8a3,
    0x748f82ee5defb2fc, 0x78a5636f43172f60, 0x84c87814a1f0ab72, 0x8cc702081a6439ec,
    0x7ba0ea2d98160007, 0x7eabf2d0c21f964a, 0x8dbe8d038b409545, 0x90bb1721582e8285,
    0x99a2ad45936d4e61, 0x9f86e289fe03e73a, 0xa84c4472faa9a82f, 0xb3df34fce89e0532,
    0xb99bb8d7b173534f, 0xbc76cbab1aea1f9c, 0xc226a69a780f3cc3, 0xd304f19aa233957d,
    0xde1be20a212129dd, 0xe39bb43755141950, 0xee84927cea48ddd2, 0xf3edd2773c523b67,
    0xfbfdfe53a8d32f2a, 0xbee2c7ab77e9e25, 0xe90181cf1b09e56, 0x25f57204c725bed8,
)
_IV = (
    0x6A09E667F3BCC908, 0xBB67AE8584CAA73B, 0x3C6EF372FE94F82B, 0xA54FF53A5F1D36F1,
    0x510E527FADE682D1, 0x9B05688C2B3E6C1F, 0x1F83D9ABFB41BD6B, 0x5BE0CD19137E2179,
)
_HKEY_KEY = b"LFEJGJJOKCEHODNMNFIKKONFBKIHJFKB"
_DAILY_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _ror(value: int, bits: int) -> int:
    return ((value >> bits) | (value << (64 - bits))) & MASK


def _custom_sha512(data: bytes) -> bytes:
    size = ((len(data) + 1 + 16 + 127) // 128) * 128
    padded = bytearray(size)
    padded[:len(data)] = data
    padded[len(data)] = 0x80
    padded[-16:] = (len(data) * 8).to_bytes(16, "big")
    state = list(_IV)
    for offset in range(0, size, 128):
        block = padded[offset:offset + 128]
        words = [int.from_bytes(block[i:i + 8], "big") for i in range(0, 128, 8)]
        for i in range(16, 80):
            x, y = words[i - 15], words[i - 2]
            s0 = _ror(x, 1) ^ _ror(x, 8) ^ (x >> 7)
            s1 = _ror(y, 38) ^ _ror(y, 55) ^ (y >> 6)
            words.append((s1 + words[i - 7] + s0 + words[i - 16]) & MASK)
        a, b, c, d, e, f, g, h = state
        for i in range(80):
            big1 = _ror(e, 14) ^ _ror(e, 18) ^ _ror(e, 41)
            choose = (e & (f ^ g)) ^ f
            t1 = (h + big1 + choose + _K[i] + words[i]) & MASK
            big0 = _ror(a, 21) ^ _ror(a, 31) ^ _ror(a, 39)
            majority = (a & (b | c)) | (b & c)
            t2 = (big0 + majority) & MASK
            a, b, c, d, e, f, g, h = (t1 + t2) & MASK, a, b, c, (d + t1) & MASK, e, f, g
        for i, value in enumerate((a, b, c, d, e, f, g, h)):
            state[i] = (state[i] + value) & MASK
    return b"".join(value.to_bytes(8, "big") for value in state)


def _custom_hmac_sha512(key: bytes, data: bytes) -> bytes:
    if len(key) > 128:
        key = _custom_sha512(key)
    ipad = bytearray([0x52] * 128)
    opad = bytearray([0x6C] * 128)
    for i, value in enumerate(key):
        ipad[i] ^= value
        opad[i] ^= value
    return _custom_sha512(bytes(opad) + _custom_sha512(bytes(ipad) + data))


def _e2(value: str) -> bytes:
    raw = value.encode("utf-8")
    raw = raw.split(b"\0", 1)[0][:127]
    out = bytearray(raw)
    for i, char in enumerate(out):
        transformed = (char & 0x7F) ^ 0x37
        out[i] = (((char & 7) << 4) ^ 0x37) if transformed == 0 or (i + 1) % 3 == 0 else transformed
    return bytes(out)


def _e3(value: str) -> bytes:
    out = bytearray(_e2(value))
    for i, char in enumerate(out):
        transformed = (char & 0x7F) ^ 0x46
        out[i] = (((char & 31) << 2) ^ 0x46) if transformed == 0 or (i + 1) % 5 == 0 else transformed
    return bytes(out)


def _reflected_crc32(data: bytes, polynomial: int) -> int:
    crc = 0xFFFFFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (polynomial if crc & 1 else 0)
    return (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF


def _daily_nonce_alphabet(timestamp: str) -> str:
    date = _datetime.datetime.fromtimestamp(int(timestamp), _datetime.timezone.utc)
    day_key = date.year * 10000 + date.month * 100 + date.day
    return "".join(_DAILY_ALPHABET[(day_key + 705 + i * 7) % 62] for i in range(32))


def make_native_nonce(timestamp: str, index_source=None) -> str:
    """Return the APP's 32-character nonce; index_source supports golden tests."""
    alphabet = _daily_nonce_alphabet(timestamp)
    choose = index_source or secrets.randbelow
    return "".join(alphabet[choose(32)] for _ in range(32))


def normalize_path(path: str) -> str:
    return "/" + "/".join(part for part in path.split("/") if part) + "/"


def sign_native(path: str, timestamp: str, debug_info: str, os_version: str,
                version: str = "1.3.398", nonce_index_source=None) -> dict:
    """Create the native query fields for the validated Heybox Android build."""
    path = str(path)
    prefix = path + str(timestamp)
    digest = hmac.new(_HKEY_KEY, (prefix + debug_info).encode("utf-8"), hashlib.sha512).digest()
    custom = _custom_hmac_sha512(_e2(prefix), _e3(debug_info + os_version + version)).hex()
    return {
        "_time": str(timestamp),
        "nonce": make_native_nonce(str(timestamp), nonce_index_source),
        "hkey": format(_reflected_crc32(digest, 0x88A4B5A0), "X"),
        "_rnd": "14:" + format(_reflected_crc32(custom.encode("ascii"), 0xAD350290), "X"),
    }


def current_native_signature(path: str, device_id: str, heybox_id: str,
                             os_version: str = "17", version: str = "1.3.398") -> dict:
    return sign_native(normalize_path(path), str(int(time.time())),
                       device_id + heybox_id, os_version, version)
