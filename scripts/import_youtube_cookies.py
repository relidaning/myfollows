#!/usr/bin/env python3
"""Imports YouTube/Google session cookies from the local Chrome profile into
data/youtube_storage_state.json, so the container can reuse an
already-logged-in session instead of driving a fresh interactive login
window (see douyin-mcp/youtube.py). Run this on the host (not in the
container) — it needs the host's Chrome profile and OS keyring.

Chrome (Linux) encrypts cookie values with AES-128-CBC. The key is derived
via PBKDF2-HMAC-SHA1(password, salt=b"saltysalt", iterations=1) from a
passphrase stored in the OS keyring under the "chrome" application (fetched
via the Secret Service D-Bus API) for "v11"-prefixed cookies, or the fixed
password "peanuts" for older "v10"-prefixed ones. After AES decryption,
Chrome prepends a 32-byte domain-binding hash to the plaintext that must be
stripped — confirmed 2026-07-29 by comparing decrypted bytes against known
cookie value shapes (e.g. NID's "NNN=..." prefix only appeared after byte
32; without stripping it, values start with 32 bytes of binary garbage).

Only .youtube.com / .google.com / accounts.google.com cookies are imported
(not your whole Chrome cookie jar) — but note these are still full Google
account session cookies (SID/SAPISID/etc.), not scoped to YouTube, so the
output file is as sensitive as a password. It's written 0600 and already
covered by the repo's .gitignore (see data/*storage_state.json).
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.request

import dbus
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

CHROME_COOKIES_DB = os.path.expanduser("~/.config/google-chrome/Default/Cookies")
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "youtube_storage_state.json")
RELOAD_URL = "http://localhost:8082/api/youtube/reload_session"

# Chrome's cookies.samesite column -> Playwright's storage_state schema.
SAMESITE_MAP = {-1: "Lax", 0: "None", 1: "Lax", 2: "Strict"}
# Chrome's expires_utc is microseconds since 1601-01-01; this converts to the
# 1970-01-01 (Unix) epoch.
CHROME_EPOCH_OFFSET_US = 11644473600_000_000


def get_chrome_safe_storage_secret() -> bytes:
    bus = dbus.SessionBus()
    service = dbus.Interface(
        bus.get_object("org.freedesktop.secrets", "/org/freedesktop/secrets"),
        "org.freedesktop.Secret.Service",
    )
    # NB: OpenSession returns (output_variant, session_object_path) in that
    # order — easy to get backwards, which fails with an opaque D-Bus
    # assertion crash rather than a catchable Python exception.
    _, session = service.OpenSession("plain", dbus.String("", variant_level=1))
    unlocked, locked = service.SearchItems({"application": "chrome"})
    if not unlocked and locked:
        service.Unlock(locked)
        unlocked, locked = service.SearchItems({"application": "chrome"})
    if not unlocked:
        raise RuntimeError("Couldn't find Chrome's Safe Storage secret in the OS keyring")
    item = dbus.Interface(
        bus.get_object("org.freedesktop.secrets", unlocked[0]),
        "org.freedesktop.Secret.Item",
    )
    _, _, value, _ = item.GetSecret(session)
    return bytes(bytearray(value))


def derive_key(password: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1)
    return kdf.derive(password)


def decrypt_cookie(key: bytes, ciphertext: bytes) -> str:
    cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadded = padded[: -padded[-1]]
    return unpadded[32:].decode("utf-8")


def main():
    if not os.path.exists(CHROME_COOKIES_DB):
        sys.exit(f"Chrome cookies DB not found at {CHROME_COOKIES_DB}")

    key_v11 = derive_key(get_chrome_safe_storage_secret())
    key_v10 = derive_key(b"peanuts")

    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        shutil.copy(CHROME_COOKIES_DB, tmp.name)
        con = sqlite3.connect(tmp.name)
        cur = con.cursor()
        cur.execute(
            """
            SELECT host_key, name, path, encrypted_value, expires_utc,
                   is_secure, is_httponly, samesite
            FROM cookies
            WHERE host_key = '.youtube.com' OR host_key LIKE '%.youtube.com'
               OR host_key = '.google.com' OR host_key = 'accounts.google.com'
            """
        )
        rows = cur.fetchall()
        con.close()

    cookies = []
    skipped = 0
    for host, name, path, encrypted_value, expires_utc, is_secure, is_httponly, samesite in rows:
        prefix, body = bytes(encrypted_value[:3]), bytes(encrypted_value[3:])
        key = key_v11 if prefix == b"v11" else key_v10 if prefix == b"v10" else None
        if key is None:
            skipped += 1
            continue
        try:
            value = decrypt_cookie(key, body)
        except Exception:
            skipped += 1
            continue
        expires = -1.0 if expires_utc == 0 else (expires_utc - CHROME_EPOCH_OFFSET_US) / 1_000_000
        cookies.append({
            "name": name,
            "value": value,
            "domain": host,
            "path": path,
            "expires": expires,
            "httpOnly": bool(is_httponly),
            "secure": bool(is_secure),
            "sameSite": SAMESITE_MAP.get(samesite, "Lax"),
        })

    if not cookies:
        sys.exit("No YouTube/Google cookies found or none could be decrypted — is Chrome logged in?")

    out_path = os.path.abspath(OUTPUT_PATH)
    with open(out_path, "w") as f:
        json.dump({"cookies": cookies, "origins": []}, f)
    os.chmod(out_path, 0o600)
    print(f"Wrote {len(cookies)} cookies to {out_path} ({skipped} skipped)")

    # The server caches its browser context across requests and only reads
    # storage_state.json once per process lifetime otherwise, so without
    # this the import would silently have no effect until a restart.
    try:
        urllib.request.urlopen(urllib.request.Request(RELOAD_URL, method="POST"), timeout=10)
        print("Told the running server to reload the session.")
    except Exception as e:
        print(f"Wrote the file, but couldn't reach {RELOAD_URL} to reload it live ({e}) "
              "— restart the container or call that endpoint manually.")


if __name__ == "__main__":
    main()
