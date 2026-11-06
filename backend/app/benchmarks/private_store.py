"""Versioned authenticated storage, bound to a row and purpose by AEAD context."""

import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from evalhive_benchmark.protocol import _json_value

MAX_PRIVATE_RECORD = 32 * 1024 * 1024


class PrivateStore:
    def __init__(self, server_secret: str):
        if not server_secret:
            raise ValueError("Private benchmark storage requires a server secret")
        key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"evalhive-benchmark-storage-v1",
            info=b"private-records/aes-256-gcm",
        ).derive(server_secret.encode())
        self._cipher = AESGCM(key)

    def seal(self, context: str, value: dict) -> bytes:
        _json_value(value)
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        if len(data) > MAX_PRIVATE_RECORD:
            raise ValueError("Private benchmark record exceeds its storage limit")
        nonce = os.urandom(12)
        return b"EHBG1" + nonce + self._cipher.encrypt(nonce, data, context.encode())

    def open(self, context: str, value: bytes) -> dict:
        try:
            if not value.startswith(b"EHBG1") or len(value) > MAX_PRIVATE_RECORD + 33:
                raise ValueError
            result = json.loads(self._cipher.decrypt(value[5:17], value[17:], context.encode()))
            _json_value(result)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (InvalidTag, ValueError, TypeError, UnicodeError):
            raise ValueError("Private benchmark record cannot be authenticated") from None


def binary(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def unbinary(value: str) -> bytes:
    return base64.b64decode(value, validate=True)
