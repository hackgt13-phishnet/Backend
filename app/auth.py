from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings

bearer = HTTPBearer(auto_error=True)


def current_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
    settings: Settings = Depends(get_settings),
) -> UUID:
    try:
        claims = jwt.decode(
            credentials.credentials,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
        return UUID(claims["sub"])
    except (jwt.PyJWTError, KeyError, ValueError) as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session") from error
