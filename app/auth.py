import logging
from functools import lru_cache
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings

bearer = HTTPBearer(auto_error=True)
CLOCK_SKEW_S = 30
log = logging.getLogger(__name__)


@lru_cache
def jwks_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_keys=True)


def verify(token: str, settings: Settings) -> dict:
    algorithm = jwt.get_unverified_header(token).get("alg")
    if settings.supabase_jwks_url:
        key = jwks_client(settings.supabase_jwks_url).get_signing_key_from_jwt(token).key
        algorithms = ["ES256", "RS256"]
    elif algorithm == "HS256":
        if not settings.supabase_jwt_secret:
            raise HTTPException(status_code=503, detail="JWT verification is not configured")
        key = settings.supabase_jwt_secret
        algorithms = ["HS256"]
    elif settings.supabase_url:
        jwks_url = settings.supabase_url.rstrip("/") + "/auth/v1/.well-known/jwks.json"
        key = jwks_client(jwks_url).get_signing_key_from_jwt(token).key
        algorithms = ["ES256", "RS256"]
    else:
        raise HTTPException(status_code=503, detail="JWT verification is not configured")
    claims = jwt.decode(
        token,
        key,
        algorithms=algorithms,
        audience="authenticated",
        issuer=settings.supabase_jwt_issuer or None,
        leeway=CLOCK_SKEW_S,
        options={"require": ["sub", "exp", "aud"]},
    )
    if claims.get("role") != "authenticated":
        raise ValueError("Authenticated user token required")
    return claims


def current_user_id(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UUID:
    try:
        return UUID(verify(credentials.credentials, settings)["sub"])
    except (jwt.PyJWTError, KeyError, ValueError) as error:
        log.warning("rejected session token: %s", type(error).__name__)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session"
        ) from error
