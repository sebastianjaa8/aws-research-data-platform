"""
Auth0 JWT validation + PostgreSQL session context.
Validates RS256-signed tokens from Auth0, extracts the researcher ID (sub claim),
and sets it as a PostgreSQL session variable so Row-Level Security policies apply.
"""
import os
from functools import lru_cache

import jwt
import psycopg2
import requests

AUTH0_DOMAIN   = os.environ["AUTH0_DOMAIN"]     # e.g. "your-tenant.us.auth0.com"
AUTH0_AUDIENCE = os.environ["AUTH0_AUDIENCE"]   # e.g. "https://api.research-platform.io"
JWKS_URL       = f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"


@lru_cache(maxsize=1)
def _get_jwks() -> dict:
    """Cached fetch of Auth0 public keys. Cache avoids per-request JWKS calls."""
    return requests.get(JWKS_URL, timeout=5).json()


def validate_token(token: str) -> dict:
    """
    Validates an Auth0 JWT and returns the payload.
    Raises jwt.InvalidTokenError on any validation failure.
    """
    header  = jwt.get_unverified_header(token)
    jwks    = _get_jwks()
    key     = next(k for k in jwks["keys"] if k["kid"] == header["kid"])
    pub_key = jwt.algorithms.RSAAlgorithm.from_jwk(key)

    return jwt.decode(
        token,
        pub_key,
        algorithms=["RS256"],
        audience=AUTH0_AUDIENCE,
        issuer=f"https://{AUTH0_DOMAIN}/",
        options={"verify_exp": True, "require": ["sub", "exp"]},
    )


def set_researcher_context(conn: psycopg2.extensions.connection, token: str) -> str:
    """
    Validates token, then sets app.researcher_id as a PostgreSQL session variable.
    PostgreSQL RLS policies read this variable to enforce per-researcher isolation.

    Returns the researcher_id (Auth0 sub claim) for use in application logic.
    """
    payload       = validate_token(token)
    researcher_id = payload["sub"]

    with conn.cursor() as cur:
        # LOCAL = scoped to current transaction; reset on rollback/commit
        cur.execute(
            "SELECT set_config('app.researcher_id', %s, true)",
            (researcher_id,),
        )

    return researcher_id
