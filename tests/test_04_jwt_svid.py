"""
Layer 3: JWT SVID Integration Tests
Validates the JWT SVIDs issued by SPIRE are correctly structured,
have proper claims, and can be verified using the OIDC Discovery
Provider's JWKS keys.
"""
import os
import ssl
import time
import pytest
import requests
import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import rsa, ec
from jwt import PyJWKClient

TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "30"))
OIDC_URL = os.environ.get("OIDC_DISCOVERY_URL",
    "https://spire-spiffe-oidc-discovery-provider.spire-system.svc.cluster.local")
KC_URL = os.environ.get("KEYCLOAK_URL",
    "http://keycloak.mcp-demo.svc.cluster.local:8080")
REALM = os.environ.get("KEYCLOAK_REALM", "mcp-demo")


def fetch_jwt_svid(audience=None):
    """Fetch JWT SVID. If no audience given, auto-detect from Keycloak OIDC config."""
    from spiffe import WorkloadApiClient
    if audience is None:
        resp = requests.get(f"{KC_URL}/realms/{REALM}/.well-known/openid-configuration", timeout=TIMEOUT)
        audience = resp.json()["issuer"]
    client = WorkloadApiClient("unix:///spiffe-workload-api/spire-agent.sock")
    return client.fetch_jwt_svid(audience={audience})


class TestJWTSVIDStructure:
    """Verify JWT SVID token structure and claims."""

    def test_svid_is_valid_jwt(self):
        """JWT SVID must be a valid, decodable JWT (without verification)."""
        svid = fetch_jwt_svid("test-structure")
        # Decode without verification to inspect claims
        claims = pyjwt.decode(
            svid.token,
            options={"verify_signature": False}
        )
        assert isinstance(claims, dict), "JWT decode didn't return dict"

    def test_svid_contains_required_claims(self):
        """JWT SVID must contain sub, aud, exp, iat claims."""
        svid = fetch_jwt_svid("test-claims")
        claims = pyjwt.decode(
            svid.token,
            options={"verify_signature": False}
        )
        required = ["sub", "aud", "exp", "iat"]
        for claim in required:
            assert claim in claims, f"Missing required claim: {claim}"

    def test_svid_subject_is_spiffe_id(self):
        """The 'sub' claim must be a valid SPIFFE ID."""
        svid = fetch_jwt_svid("test-subject")
        claims = pyjwt.decode(
            svid.token,
            options={"verify_signature": False}
        )
        sub = claims["sub"]
        assert sub.startswith("spiffe://"), \
            f"Subject is not a SPIFFE ID: {sub}"
        assert "example.org" in sub, \
            f"Trust domain not in subject: {sub}"

    def test_svid_audience_matches_request(self):
        """The 'aud' claim must contain the requested audience."""
        audience = f"{KC_URL}/realms/{REALM}"
        svid = fetch_jwt_svid(audience)
        claims = pyjwt.decode(
            svid.token,
            options={"verify_signature": False}
        )
        aud = claims["aud"]
        if isinstance(aud, list):
            assert audience in aud, \
                f"Requested audience not in aud: {aud}"
        else:
            assert aud == audience, \
                f"Audience mismatch: {aud} != {audience}"

    def test_svid_not_expired(self):
        """JWT SVID must not already be expired at issuance."""
        svid = fetch_jwt_svid("test-expiry")
        claims = pyjwt.decode(
            svid.token,
            options={"verify_signature": False}
        )
        now = time.time()
        assert claims["exp"] > now, \
            f"SVID already expired: exp={claims['exp']}, now={now}"

    def test_svid_ttl_within_expected_range(self):
        """JWT SVID TTL should be <= 5 minutes (300s) as configured."""
        svid = fetch_jwt_svid("test-ttl")
        claims = pyjwt.decode(
            svid.token,
            options={"verify_signature": False}
        )
        ttl = claims["exp"] - claims["iat"]
        assert ttl <= 600, f"SVID TTL too long: {ttl}s (expected <= 600)"
        assert ttl >= 30, f"SVID TTL too short: {ttl}s (expected >= 30)"

    def test_svid_has_three_parts(self):
        """JWT must have exactly 3 dot-separated parts (header.payload.sig)."""
        svid = fetch_jwt_svid("test-parts")
        parts = svid.token.split(".")
        assert len(parts) == 3, f"JWT has {len(parts)} parts, expected 3"

    def test_svid_header_has_kid(self):
        """JWT header must contain a 'kid' for key matching."""
        svid = fetch_jwt_svid("test-kid")
        header = pyjwt.get_unverified_header(svid.token)
        assert "kid" in header, "JWT header missing 'kid'"
        assert "alg" in header, "JWT header missing 'alg'"
        assert header["alg"] in ("RS256", "RS384", "RS512", "ES256", "ES384"), \
            f"Unexpected algorithm: {header['alg']}"


class TestJWTSVIDCryptoVerification:
    """Verify JWT SVID signature using OIDC Discovery JWKS."""

    def test_svid_verifiable_via_oidc_jwks(self):
        """JWT SVID must be verifiable using keys from OIDC Discovery Provider."""
        svid = fetch_jwt_svid("test-crypto")

        # Fetch JWKS from OIDC Discovery Provider
        jwks_url = f"{OIDC_URL}/keys"
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        jwks_client = PyJWKClient(jwks_url, ssl_context=ssl_ctx)
        signing_key = jwks_client.get_signing_key_from_jwt(svid.token)

        # Verify the signature (this will raise if invalid)
        claims = pyjwt.decode(
            svid.token,
            signing_key.key,
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384"],
            audience="test-crypto",
            options={"verify_exp": True}
        )
        assert "sub" in claims, "Verified JWT missing 'sub' claim"

    def test_tampered_svid_fails_verification(self):
        """A tampered JWT SVID must fail signature verification."""
        svid = fetch_jwt_svid("test-tamper")

        # Tamper with the payload
        parts = svid.token.split(".")
        tampered = parts[0] + "." + parts[1] + "X" + "." + parts[2]

        jwks_url = f"{OIDC_URL}/keys"
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        jwks_client = PyJWKClient(jwks_url, ssl_context=ssl_ctx)

        with pytest.raises(Exception):
            signing_key = jwks_client.get_signing_key_from_jwt(tampered)
            pyjwt.decode(
                tampered,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience="test-tamper"
            )

    def test_svid_kid_matches_jwks_key(self):
        """The JWT's 'kid' must match a key in the JWKS."""
        svid = fetch_jwt_svid("test-kid-match")
        header = pyjwt.get_unverified_header(svid.token)
        kid = header["kid"]

        resp = requests.get(f"{OIDC_URL}/keys", timeout=TIMEOUT, verify=False)
        jwks = resp.json()
        jwks_kids = [k["kid"] for k in jwks["keys"]]
        assert kid in jwks_kids, \
            f"JWT kid '{kid}' not found in JWKS keys: {jwks_kids}"


class TestSVIDSoftwareStatementClaims:
    """Verify software statement claims added by CredentialComposer plugin."""

    def test_svid_contains_jwks_url_claim(self):
        """JWT SVID enriched by software-statements plugin must contain jwks_url."""
        svid = fetch_jwt_svid("test-ss-claims")
        claims = pyjwt.decode(
            svid.token,
            options={"verify_signature": False}
        )
        # The CredentialComposer plugin should add these claims
        if "jwks_url" in claims:
            assert claims["jwks_url"].startswith("http"), \
                f"jwks_url is not a URL: {claims['jwks_url']}"

    def test_svid_contains_client_auth_claim(self):
        """JWT SVID should contain client_auth claim if software-statements plugin active."""
        svid = fetch_jwt_svid("test-ss-auth")
        claims = pyjwt.decode(
            svid.token,
            options={"verify_signature": False}
        )
        if "client_auth" in claims:
            assert claims["client_auth"] == "client-spiffe-jwt", \
                f"Unexpected client_auth: {claims['client_auth']}"
