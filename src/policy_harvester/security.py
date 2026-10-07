from __future__ import annotations

import base64
import hashlib
import os
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _key(master_key: str) -> bytes:
    if len(master_key) < 20:
        raise ValueError("MASTER_KEY must contain at least 20 characters")
    try:
        decoded = base64.urlsafe_b64decode(master_key + "=" * (-len(master_key) % 4))
        if len(decoded) == 32:
            return decoded
    except ValueError:
        pass
    return hashlib.sha256(master_key.encode()).digest()


def encrypt_secret(value: str, master_key: str, secret_key: str) -> bytes:
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key(master_key)).encrypt(nonce, value.encode(), secret_key.encode())
    return nonce + ciphertext


def decrypt_secret(value: bytes, master_key: str, secret_key: str) -> str:
    if len(value) < 29:
        raise ValueError("invalid encrypted secret")
    plaintext = AESGCM(_key(master_key)).decrypt(value[:12], value[12:], secret_key.encode())
    return plaintext.decode()


def mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "••••"
    return f"{value[:3]}...{value[-4:]}"


def validate_proxy_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise ValueError("proxy URL must use http, https, socks5, or socks5h and include a host")
    if parsed.query or parsed.fragment:
        raise ValueError("proxy URL cannot contain a query string or fragment")
    return value


def mask_proxy_url(value: str) -> str:
    parsed = urlsplit(value)
    port = f":{parsed.port}" if parsed.port else ""
    credentials = "••••@" if parsed.username or parsed.password else ""
    return f"{parsed.scheme}://{credentials}{parsed.hostname}{port}"
