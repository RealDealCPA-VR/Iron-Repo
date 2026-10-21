"""Optional client-side encryption (PRD section 12).

Everything Iron uploads (bundles and log records) is sealed with AES-256-GCM
before it leaves the computer. Stored names are hashes of the ciphertext, so
the provider cannot even confirm whether you hold a known file.
"""

import os

from .errors import IntegrityError, IronError
from .recover import AAD, MAGIC, decrypt


class PlainCodec:
    encrypted = False

    def encode(self, data):
        return data

    def decode(self, stored):
        return stored


class AesGcmCodec:
    encrypted = True

    def __init__(self, key):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            raise IronError("this repository is encrypted, which needs the 'cryptography' package: "
                            "python -m pip install cryptography") from None
        self._aead = AESGCM(key)
        self.key = key

    def encode(self, data):
        nonce = os.urandom(12)
        return MAGIC + nonce + self._aead.encrypt(nonce, data, AAD)

    def decode(self, stored):
        try:
            return decrypt(self.key, stored)
        except Exception:
            raise IntegrityError("an object failed decryption (wrong key, or it was tampered with)") from None


def crypto_available():
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return False
    return True
