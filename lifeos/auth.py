import base64
import hashlib
import hmac
import json
import time
from typing import Any

from fastapi import Depends, HTTPException, Request, Response, status

from lifeos.config import settings

COOKIE_NAME = "lifeos_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(payload: str) -> str:
    return hmac.new(settings.lifeos_secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def verify_password(password: str) -> bool:
    return hmac.compare_digest(password, settings.lifeos_password)


def create_session_token() -> str:
    payload = {"sub": "owner", "iat": int(time.time())}
    encoded = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{encoded}.{_sign(encoded)}"


def read_session_token(token: str | None) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(encoded)):
        return None
    try:
        payload = json.loads(_unb64(encoded))
    except (ValueError, TypeError):
        return None
    if int(time.time()) - int(payload.get("iat", 0)) > SESSION_TTL_SECONDS:
        return None
    return payload


def set_session_cookie(response: Response) -> None:
    response.set_cookie(
        COOKIE_NAME,
        create_session_token(),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.lifeos_cookie_secure,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME)


def require_user(request: Request) -> dict[str, Any]:
    payload = read_session_token(request.cookies.get(COOKIE_NAME))
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return payload


UserDependency = Depends(require_user)

