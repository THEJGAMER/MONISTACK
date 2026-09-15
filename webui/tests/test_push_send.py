"""The send path must hand pywebpush a key it can use.

Found live, on the first real "Send a test page": pywebpush's
vapid_private_key accepts a Vapid instance, a path to a PEM file, or a raw
base64url key string - and we were handing it the PEM *text*, which it
tried to parse as raw DER: "Could not deserialize key data … ASN.1
parsing error: invalid length". The key was fine; the hand-off was wrong.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import push  # noqa: E402

pytest.importorskip("pywebpush")
pytest.importorskip("py_vapid")


def test_pywebpush_receives_a_vapid_instance_built_from_our_pem(tmp_path, monkeypatch):
    from py_vapid import Vapid
    import pywebpush

    keys = push.VapidKeys(tmp_path / "v.json", "mailto:x@example.com")
    assert keys.available
    seen = {}

    def fake_webpush(subscription_info, data, vapid_private_key, vapid_claims, **kw):
        seen["key"] = vapid_private_key
        seen["claims"] = vapid_claims

    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    n = push.PushNotifier(None, keys)

    ok, err, gone = n._webpush({"endpoint": "https://push.example/x", "keys": {"p256dh": "P", "auth": "A"}},
                               {"title": "t", "body": "b", "severity": "info"})

    assert ok and err is None
    assert isinstance(seen["key"], Vapid), "PEM text is not something pywebpush can parse"
    assert seen["claims"] == {"sub": "mailto:x@example.com"}


def test_the_parsed_key_matches_the_stored_public_key(tmp_path):
    """The Vapid we hand over must be *our* key - the one browsers
    subscribed against - or every send is signed by a stranger."""
    from py_vapid.utils import b64urlencode
    from cryptography.hazmat.primitives import serialization

    keys = push.VapidKeys(tmp_path / "v.json", "mailto:x@example.com")
    assert keys.available
    vv = keys.vapid()

    pub = vv.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    assert b64urlencode(pub) == keys.public_key


def test_the_key_is_parsed_once_not_per_send(tmp_path):
    keys = push.VapidKeys(tmp_path / "v.json", "mailto:x@example.com")
    assert keys.available

    assert keys.vapid() is keys.vapid()
