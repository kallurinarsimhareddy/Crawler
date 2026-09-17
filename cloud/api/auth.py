"""Who is calling: verifying Supabase access tokens.

Every job endpoint requires ``Authorization: Bearer <access token>``. The token
is the one Supabase Auth issues to the browser after sign-in; the API verifies it
and takes the user id from its ``sub`` claim. That id is the job's owner.

**What is checked** — all of it, every request:

* signature, with the algorithm fixed by where the key came from: ``HS256``
  only if a legacy JWT secret is configured, ``ES256``/``RS256`` only with a key
  from the project's JWKS endpoint. ``none`` and anything else are refused, and
  an HS256 token is never checked against a public key;
* ``exp`` (with 30 s leeway), ``iat``, ``aud`` (``authenticated``) and ``iss``
  (``<SUPABASE_URL>/auth/v1``) — all required;
* ``role`` is ``authenticated`` — an ``anon`` or ``service_role`` token is not a
  user;
* ``is_anonymous`` is not true — Supabase anonymous sign-ins get no job data;
* ``sub`` is a UUID.

**Development mode** (``CAREERCLOUD_AUTH_MODE=dev``) issues HS256 tokens signed
with a local secret, so the dashboard can be used without a Supabase project.
The same checks apply with issuer ``careercloud-dev``. Settings refuse dev mode
outside ``development``/``test``.
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import jwt

__all__ = [
    "AuthError",
    "AuthUnavailableError",
    "DevTokenIssuer",
    "Principal",
    "SupabaseTokenVerifier",
    "TokenVerifier",
]

DEV_ISSUER = "careercloud-dev"
_DEV_NAMESPACE = uuid.UUID("7b1f3c0e-2a4d-4c55-9b8e-3f6a0d9c1e27")
_ASYMMETRIC = frozenset({"ES256", "RS256"})
_MAX_TOKEN_LENGTH = 8192


class AuthError(Exception):
    """The request is not authenticated (HTTP 401)."""


class AuthUnavailableError(Exception):
    """Authentication is not configured or its key source is unreachable (HTTP 503)."""


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: Optional[str] = None
    claims: Dict[str, Any] = field(default_factory=dict, repr=False)


class TokenVerifier(ABC):
    mode: str = "unknown"

    @abstractmethod
    def verify(self, token: str) -> Principal:
        """Return the caller, or raise :class:`AuthError`."""


def _principal(claims: Dict[str, Any]) -> Principal:
    if claims.get("role") != "authenticated":
        raise AuthError("token is not for a signed-in user")
    if claims.get("is_anonymous") is True:
        raise AuthError("anonymous sessions cannot access jobs")
    try:
        user_id = str(uuid.UUID(str(claims.get("sub"))))
    except ValueError as error:
        raise AuthError("token subject is not a user id") from error
    email = claims.get("email")
    return Principal(user_id=user_id, email=email if isinstance(email, str) else None, claims=claims)


def _decode(token: str, key: Any, algorithm: str, *, audience: str, issuer: str, leeway: float) -> Dict[str, Any]:
    try:
        return jwt.decode(
            token,
            key,
            algorithms=[algorithm],
            audience=audience,
            issuer=issuer,
            leeway=leeway,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as error:
        raise AuthError("token has expired") from error
    except jwt.InvalidTokenError as error:
        raise AuthError("token is invalid") from error


def _header(token: str) -> Dict[str, Any]:
    if not token or len(token) > _MAX_TOKEN_LENGTH:
        raise AuthError("token is invalid")
    try:
        return jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as error:
        raise AuthError("token is invalid") from error


class SupabaseTokenVerifier(TokenVerifier):
    """Verifies Supabase Auth access tokens.

    Args:
        supabase_url: ``https://<project>.supabase.co``.
        jwt_secret: The project's legacy HS256 JWT secret, if it still uses one.
        jwks_client: Resolves signing keys for asymmetric tokens. Defaults to
            ``PyJWKClient`` on ``<supabase_url>/auth/v1/.well-known/jwks.json``,
            which caches keys. Tests inject their own.
    """

    mode = "supabase"

    def __init__(
        self,
        supabase_url: str,
        *,
        jwt_secret: Optional[str] = None,
        audience: str = "authenticated",
        jwks_client: Any = None,
        leeway: float = 30.0,
        use_jwks: bool = True,
    ) -> None:
        self._url = supabase_url.rstrip("/")
        self._issuer = f"{self._url}/auth/v1"
        self._secret = jwt_secret
        self._audience = audience
        self._leeway = leeway
        if jwks_client is None and use_jwks:
            jwks_client = jwt.PyJWKClient(
                f"{self._issuer}/.well-known/jwks.json", cache_keys=True, lifespan=600, timeout=10
            )
        self._jwks = jwks_client

    def verify(self, token: str) -> Principal:
        header = _header(token)
        algorithm = header.get("alg")
        if algorithm == "HS256":
            if not self._secret:
                raise AuthError("token is invalid")
            key: Any = self._secret
        elif algorithm in _ASYMMETRIC:
            if self._jwks is None:
                raise AuthError("token is invalid")
            try:
                signing_key = self._jwks.get_signing_key_from_jwt(token)
            except jwt.PyJWKClientConnectionError as error:
                raise AuthUnavailableError("could not fetch Supabase signing keys") from error
            except jwt.PyJWKClientError as error:
                raise AuthError("token is invalid") from error
            key = signing_key.key
            if getattr(signing_key, "algorithm_name", algorithm) not in (algorithm, None):
                raise AuthError("token is invalid")
        else:
            raise AuthError("token is invalid")
        claims = _decode(
            token, key, algorithm, audience=self._audience, issuer=self._issuer, leeway=self._leeway
        )
        return _principal(claims)


class DevTokenIssuer(TokenVerifier):
    """Issues and verifies local development tokens. Never used in production."""

    mode = "dev"

    def __init__(self, secret: str, *, ttl_seconds: int = 8 * 3600, leeway: float = 30.0) -> None:
        if len(secret) < 32:
            raise ValueError("the development JWT secret must be at least 32 characters")
        self._secret = secret
        self._ttl = ttl_seconds
        self._leeway = leeway

    @staticmethod
    def user_id_for(email: str) -> str:
        """A stable user id per email, so a developer keeps their jobs across sessions."""
        return str(uuid.uuid5(_DEV_NAMESPACE, email.strip().lower()))

    def issue(self, email: str, *, now: Optional[float] = None) -> Dict[str, Any]:
        issued = int(now if now is not None else time.time())
        user_id = self.user_id_for(email)
        claims = {
            "sub": user_id,
            "email": email.strip().lower(),
            "role": "authenticated",
            "aud": "authenticated",
            "iss": DEV_ISSUER,
            "iat": issued,
            "exp": issued + self._ttl,
        }
        token = jwt.encode(claims, self._secret, algorithm="HS256")
        return {"access_token": token, "expires_in": self._ttl, "user_id": user_id, "email": claims["email"]}

    def verify(self, token: str) -> Principal:
        if _header(token).get("alg") != "HS256":
            raise AuthError("token is invalid")
        claims = _decode(
            token, self._secret, "HS256", audience="authenticated", issuer=DEV_ISSUER, leeway=self._leeway
        )
        return _principal(claims)
