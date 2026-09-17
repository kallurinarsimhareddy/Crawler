"""Token verification: what gets in, and everything that must not."""

from __future__ import annotations

import base64
import json
import time
import unittest
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from cloud.api.auth import AuthError, AuthUnavailableError, DevTokenIssuer, SupabaseTokenVerifier
from cloud.api.settings import load_settings

SUPABASE = "https://abcdefgh.supabase.co"
SECRET = "legacy-supabase-jwt-secret-with-enough-length-0123"
USER = str(uuid.uuid4())


def claims(**overrides):
    now = int(time.time())
    base = {
        "sub": USER,
        "email": "alice@example.com",
        "role": "authenticated",
        "aud": "authenticated",
        "iss": f"{SUPABASE}/auth/v1",
        "iat": now,
        "exp": now + 3600,
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


class FakeJwks:
    """Stands in for PyJWKClient: returns the key registered for the token's kid."""

    def __init__(self) -> None:
        self.keys = {}

    def add(self, kid, public_key):
        self.keys[kid] = public_key

    def get_signing_key_from_jwt(self, token):
        kid = jwt.get_unverified_header(token).get("kid")
        if kid not in self.keys:
            raise jwt.PyJWKClientError("unknown kid")

        class Key:
            key = self.keys[kid]
            algorithm_name = "ES256"

        return Key()


class DownJwks:
    def get_signing_key_from_jwt(self, token):
        raise jwt.PyJWKClientConnectionError("unreachable")


class TestSupabaseVerifier(unittest.TestCase):
    def setUp(self) -> None:
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.jwks = FakeJwks()
        self.jwks.add("k1", self.private.public_key())
        self.verifier = SupabaseTokenVerifier(SUPABASE, jwt_secret=SECRET, jwks_client=self.jwks)

    def es256(self, payload, kid="k1", key=None):
        return jwt.encode(payload, key or self.private, algorithm="ES256", headers={"kid": kid})

    def assertRejected(self, token, verifier=None):
        with self.assertRaises(AuthError):
            (verifier or self.verifier).verify(token)

    def test_a_valid_asymmetric_token_identifies_the_user(self) -> None:
        principal = self.verifier.verify(self.es256(claims()))
        self.assertEqual((principal.user_id, principal.email), (USER, "alice@example.com"))

    def test_a_valid_legacy_hs256_token_identifies_the_user(self) -> None:
        self.assertEqual(self.verifier.verify(jwt.encode(claims(), SECRET, algorithm="HS256")).user_id, USER)

    def test_expired_tokens_are_rejected(self) -> None:
        past = int(time.time()) - 7200
        with self.assertRaisesRegex(AuthError, "expired"):
            self.verifier.verify(self.es256(claims(iat=past, exp=past + 60)))

    def test_small_clock_skew_is_tolerated(self) -> None:
        now = int(time.time())
        self.verifier.verify(self.es256(claims(iat=now - 100, exp=now - 10)))

    def test_wrong_signature_audience_issuer_and_missing_claims_are_rejected(self) -> None:
        other = ec.generate_private_key(ec.SECP256R1())
        cases = {
            "foreign key": self.es256(claims(), key=other),
            "unknown kid": self.es256(claims(), kid="nope"),
            "wrong secret": jwt.encode(claims(), "x" * 40, algorithm="HS256"),
            "wrong audience": self.es256(claims(aud="service")),
            "wrong issuer": self.es256(claims(iss="https://evil.supabase.co/auth/v1")),
            "no exp": self.es256(claims(exp=None)),
            "no sub": self.es256(claims(sub=None)),
            "no iat": self.es256(claims(iat=None)),
        }
        for name, token in cases.items():
            with self.subTest(name):
                self.assertRejected(token)

    def test_non_user_roles_and_anonymous_sessions_are_rejected(self) -> None:
        for name, payload in {
            "anon key": claims(role="anon"),
            "service role": claims(role="service_role"),
            "anonymous sign-in": claims(is_anonymous=True),
            "non-uuid subject": claims(sub="admin"),
        }.items():
            with self.subTest(name):
                self.assertRejected(self.es256(payload))

    def test_alg_none_is_rejected(self) -> None:
        header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=")
        body = base64.urlsafe_b64encode(json.dumps(claims()).encode()).rstrip(b"=")
        self.assertRejected(f"{header.decode()}.{body.decode()}.")

    def test_hs256_is_refused_when_no_secret_is_configured(self) -> None:
        """Algorithm confusion: an HS256 token must never be checked against anything else."""
        verifier = SupabaseTokenVerifier(SUPABASE, jwks_client=self.jwks)
        self.assertRejected(jwt.encode(claims(), SECRET, algorithm="HS256"), verifier)

    def test_asymmetric_is_refused_without_jwks(self) -> None:
        verifier = SupabaseTokenVerifier(SUPABASE, jwt_secret=SECRET, use_jwks=False)
        self.assertRejected(self.es256(claims()), verifier)

    def test_garbage_and_oversized_tokens_are_rejected(self) -> None:
        for token in ("", "not-a-jwt", "a.b.c", "x" * 10000):
            with self.subTest(token=token[:20]):
                self.assertRejected(token)

    def test_unreachable_jwks_is_unavailable_not_unauthorized(self) -> None:
        verifier = SupabaseTokenVerifier(SUPABASE, jwks_client=DownJwks())
        with self.assertRaises(AuthUnavailableError):
            verifier.verify(self.es256(claims()))


class TestDevTokens(unittest.TestCase):
    def setUp(self) -> None:
        self.issuer = DevTokenIssuer("development-secret-that-is-long-enough-000")

    def test_issue_and_verify(self) -> None:
        session = self.issuer.issue("Dev@Example.com")
        principal = self.issuer.verify(session["access_token"])
        self.assertEqual(principal.user_id, session["user_id"])
        self.assertEqual(principal.email, "dev@example.com")
        self.assertEqual(self.issuer.user_id_for("dev@example.com"), session["user_id"])

    def test_dev_tokens_expire_and_are_bound_to_their_secret(self) -> None:
        old = self.issuer.issue("a@example.com", now=time.time() - 10 * 3600)
        with self.assertRaisesRegex(AuthError, "expired"):
            self.issuer.verify(old["access_token"])
        other = DevTokenIssuer("another-development-secret-long-enough-111")
        with self.assertRaises(AuthError):
            other.verify(self.issuer.issue("a@example.com")["access_token"])

    def test_supabase_tokens_are_not_dev_tokens(self) -> None:
        token = jwt.encode(claims(), "development-secret-that-is-long-enough-000", algorithm="HS256")
        with self.assertRaises(AuthError):  # issuer differs
            self.issuer.verify(token)

    def test_short_secrets_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            DevTokenIssuer("short")


class TestAuthSettings(unittest.TestCase):
    def test_dev_auth_is_refused_outside_development(self) -> None:
        for environment in ("staging", "production"):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                load_settings({"CAREERCLOUD_ENV": environment, "CAREERCLOUD_AUTH_MODE": "dev"})

    def test_supabase_url_must_be_https(self) -> None:
        with self.assertRaises(ValueError):
            load_settings({"CAREERCLOUD_SUPABASE_URL": "http://abcdefgh.supabase.co"})

    def test_weak_secrets_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            load_settings({"CAREERCLOUD_SUPABASE_JWT_SECRET": "short"})
        with self.assertRaises(ValueError):
            load_settings({"CAREERCLOUD_AUTH_MODE": "dev", "CAREERCLOUD_DEV_JWT_SECRET": "short"})

    def test_the_service_role_key_is_flagged_if_given_to_the_api(self) -> None:
        with self.assertLogs("cloud.api.settings", level="WARNING") as logs:
            load_settings({"CAREERCLOUD_SUPABASE_SERVICE_ROLE_KEY": "secret"})
        self.assertIn("SERVICE_ROLE_KEY", logs.output[0])


if __name__ == "__main__":
    unittest.main()
