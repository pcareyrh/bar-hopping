import os
from cryptography.fernet import Fernet

_key = os.getenv("ENCRYPTION_KEY", "").encode()
_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet, _key
    if _fernet is None:
        key = os.getenv("ENCRYPTION_KEY", "").encode()
        if not key:
            raise RuntimeError("ENCRYPTION_KEY env var is not set")
        _fernet = Fernet(key)
    return _fernet


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()
