"""Encryption keys and this device's identity. Keys never leave this computer
unless you copy the recovery key yourself."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import uuid
from pathlib import Path

from .recover import decode_key  # noqa: F401 - re-exported


def iron_home():
    return Path(os.environ.get("IRON_HOME") or Path.home() / ".iron")


def generate_key():
    return secrets.token_bytes(32)


def key_check(key):
    """A public fingerprint that tells a wrong key from the right one without revealing it."""
    return hmac.new(key, b"iron-key-check-v1", hashlib.sha256).hexdigest()[:32]


def encode_key(key):
    return "iron1-" + base64.urlsafe_b64encode(key).decode().rstrip("=")


def key_file(repo_id):
    return iron_home() / "keys" / ("%s.key" % repo_id)


def save_key(repo_id, key):
    path = key_file(repo_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encode_key(key) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def load_key(repo_id):
    env = os.environ.get("IRON_KEY")
    if env:
        return decode_key(env)
    path = key_file(repo_id)
    if path.is_file():
        return decode_key(path.read_text(encoding="utf-8"))
    return None


def identity():
    """Who is writing: a stable random device id plus readable names."""
    path = iron_home() / "device.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {"device_id": uuid.uuid4().hex, "device": socket.gethostname()}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass
    out = {"device": data.get("device", "unknown"), "device_id": data.get("device_id", "")}
    agent = os.environ.get("IRON_AGENT")
    if agent:
        out["agent"] = agent
        if os.environ.get("IRON_SESSION"):
            out["session"] = os.environ["IRON_SESSION"]
    return out
