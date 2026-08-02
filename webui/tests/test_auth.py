"""Tests for auth.role_from_claims/role_meets - the mapping from Keycloak
client-role claims to Switchboard's viewer/operator/admin tiers, and the
tier comparison require_role() in app.py is built on.

Worth pinning down precisely because the failure direction that matters is
one-sided: a user who should have no access at all ending up with any
access is a real privilege escalation. role_from_claims returns None (not
"viewer") for the missing-claim case - a Keycloak account existing is not
the same as being granted anything in this app, so the caller
(api_auth_callback) refuses to even create a session rather than quietly
granting read access.
"""
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from auth import role_from_claims, role_meets, verify_logout_token

try:
    from joserfc import jwt as jose_jwt
    from joserfc.jwk import KeySet, RSAKey
    HAVE_JOSERFC = True
except ImportError:
    HAVE_JOSERFC = False


def test_admin_role_present():
    claims = {"resource_access": {"switchboard": {"roles": ["admin"]}}}
    assert role_from_claims(claims, "switchboard") == "admin"


def test_operator_role_present():
    claims = {"resource_access": {"switchboard": {"roles": ["operator"]}}}
    assert role_from_claims(claims, "switchboard") == "operator"


def test_viewer_role_present():
    claims = {"resource_access": {"switchboard": {"roles": ["viewer"]}}}
    assert role_from_claims(claims, "switchboard") == "viewer"


def test_highest_role_wins_when_multiple_assigned():
    claims = {"resource_access": {"switchboard": {"roles": ["viewer", "operator"]}}}
    assert role_from_claims(claims, "switchboard") == "operator"


def test_no_resource_access_returns_none():
    """Authenticated but no role claim at all - e.g. a Keycloak user
    nobody has assigned a client role to yet. Must get nothing, not
    viewer."""
    assert role_from_claims({}, "switchboard") is None


def test_unrecognized_roles_return_none():
    claims = {"resource_access": {"switchboard": {"roles": ["some-other-app-role"]}}}
    assert role_from_claims(claims, "switchboard") is None


def test_roles_for_a_different_client_are_ignored():
    """A user could be admin of some unrelated Keycloak client - that must
    not leak into this app's role, or resource_access from any other
    client-registered app becomes a privilege escalation path."""
    claims = {"resource_access": {"some-other-app": {"roles": ["admin"]}}}
    assert role_from_claims(claims, "switchboard") is None


def test_role_meets_ordering():
    assert role_meets("admin", "viewer")
    assert role_meets("admin", "operator")
    assert role_meets("admin", "admin")
    assert role_meets("operator", "viewer")
    assert role_meets("operator", "operator")
    assert not role_meets("operator", "admin")
    assert not role_meets("viewer", "operator")
    assert not role_meets("viewer", "admin")


def test_role_meets_unknown_role_fails_closed():
    """An unrecognized role string (shouldn't happen given role_from_claims
    only ever returns one of the three or None, but require_role's own
    defensive check) must not satisfy any tier."""
    assert not role_meets("bogus", "viewer")


def test_role_meets_none_fails_every_tier():
    """None - no role assigned at all - must not satisfy even the lowest
    tier (viewer). This is the actual enforcement point for "no roles
    assigned means no access": api_auth_callback refuses to create a
    session when role_from_claims returns None, but if that were ever
    bypassed, role_meets(None, ...) is the backstop."""
    assert not role_meets(None, "viewer")
    assert not role_meets(None, "operator")
    assert not role_meets(None, "admin")


# --- verify_logout_token (OIDC Back-Channel Logout) ---
#
# Sessions here are signed cookies with no server-side table, so this is
# the only path that can end a session Keycloak-side without the user
# ever clicking this app's own Log out button (see app.py's
# _revoke_sids/api_auth_backchannel_logout). A forged or malformed token
# accepted here is a real way to either DoS a legitimate session (revoke
# someone else's) or - if validation were looser - inject state; every
# rejection case below was hit live against a real signed token during
# development, not assumed.

ISSUER = "https://keycloak.example.com/realms/master"
CLIENT_ID = "switchboard"

pytestmark_joserfc = pytest.mark.skipif(not HAVE_JOSERFC, reason="joserfc not installed")


def _make_signed_token(claims, key=None, kid="test-key"):
    key = key or RSAKey.generate_key(2048, parameters={"kid": kid})
    header = {"alg": "RS256", "kid": kid}
    return jose_jwt.encode(header, claims, key), key


def _base_claims(**overrides):
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "iat": int(time.time()),
        "jti": "evt-1",
        "sid": "kc-session-1",
        "sub": "user-1",
        "events": {"http://schemas.openid.net/event/backchannel-logout": {}},
    }
    claims.update(overrides)
    return claims


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, jwks_json):
        self._jwks_json = jwks_json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, timeout=None):
        return _FakeResponse(self._jwks_json)


def _run(coro):
    import asyncio
    return asyncio.run(coro)


@pytestmark_joserfc
def test_verify_logout_token_accepts_a_valid_token():
    token, key = _make_signed_token(_base_claims())
    jwks_json = KeySet([key]).as_dict(private=False)
    with patch("auth.httpx.AsyncClient", return_value=_FakeAsyncClient(jwks_json)):
        sid, sub = _run(verify_logout_token(token, ISSUER, CLIENT_ID, "https://fake/jwks"))
    assert sid == "kc-session-1"
    assert sub == "user-1"


@pytestmark_joserfc
def test_verify_logout_token_rejects_wrong_audience():
    token, key = _make_signed_token(_base_claims(aud="some-other-client"))
    jwks_json = KeySet([key]).as_dict(private=False)
    with patch("auth.httpx.AsyncClient", return_value=_FakeAsyncClient(jwks_json)):
        with pytest.raises(ValueError, match="audience"):
            _run(verify_logout_token(token, ISSUER, CLIENT_ID, "https://fake/jwks"))


@pytestmark_joserfc
def test_verify_logout_token_rejects_wrong_issuer():
    token, key = _make_signed_token(_base_claims(iss="https://not-our-keycloak.example.com/realms/master"))
    jwks_json = KeySet([key]).as_dict(private=False)
    with patch("auth.httpx.AsyncClient", return_value=_FakeAsyncClient(jwks_json)):
        with pytest.raises(ValueError, match="issuer"):
            _run(verify_logout_token(token, ISSUER, CLIENT_ID, "https://fake/jwks"))


@pytestmark_joserfc
def test_verify_logout_token_rejects_missing_backchannel_event():
    """A token that's otherwise a perfectly valid ID-token-shaped JWT but
    doesn't carry the specific backchannel-logout event must not be
    treated as a logout - this is what stops any other JWT Keycloak
    issues (an access token, a refresh token) from being replayed here to
    revoke a session."""
    token, key = _make_signed_token(_base_claims(events={}))
    jwks_json = KeySet([key]).as_dict(private=False)
    with patch("auth.httpx.AsyncClient", return_value=_FakeAsyncClient(jwks_json)):
        with pytest.raises(ValueError, match="event"):
            _run(verify_logout_token(token, ISSUER, CLIENT_ID, "https://fake/jwks"))


@pytestmark_joserfc
def test_verify_logout_token_rejects_a_nonce():
    """Per spec, a logout_token must NOT contain a nonce - nonces belong
    to the front-channel Authorization Code flow. One present is a sign
    of misuse, not something to tolerate."""
    token, key = _make_signed_token(_base_claims(nonce="should-not-be-here"))
    jwks_json = KeySet([key]).as_dict(private=False)
    with patch("auth.httpx.AsyncClient", return_value=_FakeAsyncClient(jwks_json)):
        with pytest.raises(ValueError, match="nonce"):
            _run(verify_logout_token(token, ISSUER, CLIENT_ID, "https://fake/jwks"))


@pytestmark_joserfc
def test_verify_logout_token_rejects_stale_iat():
    token, key = _make_signed_token(_base_claims(iat=int(time.time()) - 3600))
    jwks_json = KeySet([key]).as_dict(private=False)
    with patch("auth.httpx.AsyncClient", return_value=_FakeAsyncClient(jwks_json)):
        with pytest.raises(ValueError, match="iat"):
            _run(verify_logout_token(token, ISSUER, CLIENT_ID, "https://fake/jwks"))


@pytestmark_joserfc
def test_verify_logout_token_rejects_forged_signature():
    """The token claims to be signed by a key in the real JWKS but is
    actually signed by a different key entirely - the case that matters
    most: this is what stops anyone who doesn't hold Keycloak's private
    key from forging a logout for someone else's session."""
    real_key = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
    forged_key = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
    token, _ = _make_signed_token(_base_claims(), key=forged_key)
    jwks_json = KeySet([real_key]).as_dict(private=False)  # only the real key is published
    with patch("auth.httpx.AsyncClient", return_value=_FakeAsyncClient(jwks_json)):
        with pytest.raises(Exception):
            _run(verify_logout_token(token, ISSUER, CLIENT_ID, "https://fake/jwks"))


@pytestmark_joserfc
def test_verify_logout_token_rejects_missing_sid_and_sub():
    claims = _base_claims()
    del claims["sid"]
    del claims["sub"]
    token, key = _make_signed_token(claims)
    jwks_json = KeySet([key]).as_dict(private=False)
    with patch("auth.httpx.AsyncClient", return_value=_FakeAsyncClient(jwks_json)):
        with pytest.raises(ValueError, match="sid|sub"):
            _run(verify_logout_token(token, ISSUER, CLIENT_ID, "https://fake/jwks"))
