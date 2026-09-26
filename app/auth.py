import logging
from functools import lru_cache
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings

bearer = HTTPBearer(auto_error=True)
CLOCK_SKEW_S = 30  # phones and servers disagree by a few seconds; a fresh token can look "not yet valid"
log = logging.getLogger(__name__)


@lru_cache
def jwks_client(supabase_url: str) -> jwt.PyJWKClient:
    """Supabase's public signing keys (new projects sign with ES256). Cached, refetched on unknown key ids."""
    return jwt.PyJWKClient(f"{supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json", cache_keys=True)


def verify(token: str, settings: Settings) -> dict:
    alg = jwt.get_unverified_header(token).get("alg")
    if alg == "HS256":  # legacy projects sign with a shared secret
        return jwt.decode(token, settings.supabase_jwt_secret, algorithms=["HS256"], audience="authenticated",
                          leeway=CLOCK_SKEW_S)
    key = jwks_client(settings.supabase_url).get_signing_key_from_jwt(token)
    return jwt.decode(token, key.key, algorithms=["ES256", "RS256"], audience="authenticated", leeway=CLOCK_SKEW_S)


def current_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
    settings: Settings = Depends(get_settings),
) -> UUID:
    try:
        return UUID(verify(credentials.credentials, settings)["sub"])
    except (jwt.PyJWTError, KeyError, ValueError) as error:
        log.warning("rejected session token: %s: %s", type(error).__name__, error)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session") from error
