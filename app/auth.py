from functools import lru_cache
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings

bearer = HTTPBearer(auto_error=True)


@lru_cache
def jwks_client(url: str):
    return jwt.PyJWKClient(url)


def current_user_id(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UUID:
    try:
        if settings.supabase_jwks_url:
            key = (
                jwks_client(settings.supabase_jwks_url)
                .get_signing_key_from_jwt(credentials.credentials)
                .key
            )
            algorithms = ["ES256", "RS256"]
        else:
            if not settings.supabase_jwt_secret:
                raise HTTPException(status_code=503, detail="JWT verification is not configured")
            key = settings.supabase_jwt_secret
            algorithms = ["HS256"]
        claims = jwt.decode(
            credentials.credentials,
            key,
            algorithms=algorithms,
            audience="authenticated",
            issuer=settings.supabase_jwt_issuer or None,
            options={"require": ["sub", "exp", "aud"]},
        )
        if claims.get("role") != "authenticated":
            raise ValueError("Authenticated user token required")
        return UUID(claims["sub"])
    except (jwt.PyJWTError, KeyError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session"
        ) from error
