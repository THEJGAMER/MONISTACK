"""Per-user OIDC login against an external, BYO Keycloak instance, plus
basic RBAC role mapping from Keycloak client roles.

Replaces the old shared HTTP Basic Auth (single admin/password). Uses
Authlib's Starlette integration (`authlib.integrations.starlette_client`)
rather than hand-rolling PKCE/state/nonce/JWKS validation - that's the
wrong place to save code on something security-critical.
"""
import logging
import time

import httpx
from authlib.integrations.starlette_client import OAuth
from joserfc import jwt as jose_jwt
from joserfc.jwk import KeySet

log = logging.getLogger(__name__)

BACKCHANNEL_LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"

ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}
ROLES_BY_RANK = ["viewer", "operator", "admin"]


def build_oauth_client(issuer_url, client_id, client_secret):
    """`issuer_url` is the Keycloak realm base, e.g.
    https://keycloak.example.com/realms/master - Authlib appends
    /.well-known/openid-configuration itself via server_metadata_url."""
    oauth = OAuth()
    oauth.register(
        name="keycloak",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url=f"{issuer_url.rstrip('/')}/.well-known/openid-configuration",
        # PKCE (S256) - not on by default, has to be requested explicitly
        # (confirmed live: the authorize URL had no code_challenge at all
        # until this was added). Required per this app's design, not
        # optional hardening.
        #
        # "roles" scope - without explicitly requesting it, Keycloak
        # doesn't include resource_access (client roles) in the ID token
        # at all, even though a "roles" client scope with a "client
        # roles" mapper exists by default (confirmed live: the ID token
        # had zero resource_access key, not an empty one, until this was
        # added - this app's whole RBAC model depends on that claim).
        client_kwargs={"scope": "openid profile email roles", "code_challenge_method": "S256"},
    )
    return oauth.keycloak


def role_from_claims(claims, client_id):
    """Reads `resource_access[client_id].roles` (Keycloak *client* roles,
    not realm roles) and returns the highest of viewer/operator/admin
    present, or None if authenticated but no recognized role claim exists
    at all - fail-closed to *no access*, not to viewer. A Keycloak account
    existing is not the same as being granted anything in this app; the
    caller (api_auth_callback) refuses to even start a session when this
    returns None, rather than quietly granting read access."""
    resource_access = claims.get("resource_access") or {}
    client_roles = set((resource_access.get(client_id) or {}).get("roles") or [])
    best = None
    for role in ROLES_BY_RANK:
        if role in client_roles:
            best = role
    return best


def role_meets(role, min_role):
    """None (no role assigned) never meets any tier, including viewer -
    ROLE_RANK.get(None, -1) is always below viewer's rank of 0."""
    return ROLE_RANK.get(role, -1) >= ROLE_RANK.get(min_role, 0)


async def verify_logout_token(logout_token, issuer_url, client_id, jwks_uri):
    """Validates a Keycloak Back-Channel Logout token (OIDC Back-Channel
    Logout 1.0) and returns the (sid, sub) it names - the app's own
    sessions are signed cookies with no server-side table, so this is the
    only way a session ends anywhere except this app's own /api/auth/logout
    button: revoking by sid lets app.py's require_auth reject a cookie
    whose Keycloak-side session has already ended, without ever needing to
    look anything up in a database.

    Checked, per spec: signature (same JWKS as any ID token), issuer,
    audience, a genuine backchannel-logout event, a sid/sub to revoke by,
    freshness (iat within 5 minutes - not a replay of an old token), and
    explicitly the ABSENCE of a nonce (a logout_token carrying one is not
    spec-compliant and a sign of misuse - nonces belong to the
    front-channel Authorization Code flow only). Raises ValueError with a
    reason on any failure; never raises for "this just isn't a valid
    logout" in a way that would look like a server error.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(jwks_uri)
        resp.raise_for_status()
        keyset = KeySet.import_key_set(resp.json())
    token = jose_jwt.decode(logout_token, keyset)
    claims = token.claims
    if claims.get("iss", "").rstrip("/") != issuer_url.rstrip("/"):
        raise ValueError("issuer mismatch")
    aud = claims.get("aud")
    aud_list = aud if isinstance(aud, list) else [aud]
    if client_id not in aud_list:
        raise ValueError("audience mismatch")
    if BACKCHANNEL_LOGOUT_EVENT not in (claims.get("events") or {}):
        raise ValueError("missing backchannel-logout event")
    if "nonce" in claims:
        raise ValueError("logout_token must not contain a nonce")
    iat = claims.get("iat")
    if not iat or time.time() - iat > 300:
        raise ValueError("logout_token missing iat or too old")
    sid = claims.get("sid")
    sub = claims.get("sub")
    if not sid and not sub:
        raise ValueError("logout_token has neither sid nor sub")
    return sid, sub
