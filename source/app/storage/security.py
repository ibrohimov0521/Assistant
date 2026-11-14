from __future__ import annotations

import hashlib
import hmac
import json

from cryptography.fernet import Fernet, InvalidToken


class Vault:
    """Fernet encryption plus an independent HMAC namespace for indexed identifiers."""

    def __init__(self, key: str | bytes):
        self._key = key.encode() if isinstance(key, str) else key
        self._fernet = Fernet(self._key)
        self._index_key = hashlib.sha256(self._key + b"/assistant-index-v1").digest()

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode("ascii")

    def seal(self, value: str | bytes) -> bytes:
        data = value.encode("utf-8") if isinstance(value, str) else value
        return self._fernet.encrypt(data)

    def open(self, value: bytes) -> bytes:
        return self._fernet.decrypt(value)

    def open_text(self, value: bytes) -> str:
        return self.open(value).decode("utf-8")

    def seal_json(self, value: object) -> bytes:
        return self.seal(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    def open_json(self, value: bytes) -> object:
        return json.loads(self.open_text(value))

    def index(self, namespace: str, value: str | int) -> str:
        message = f"{namespace}:{value}".encode("utf-8")
        return hmac.new(self._index_key, message, hashlib.sha256).hexdigest()


__all__ = ["InvalidToken", "Vault"]

