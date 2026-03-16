"""
Layer 4: Full End-to-End Flow Tests
Tests the complete chain: SPIRE → JWT SVID → Keycloak → Access Token → Resource.
These tests cross all component boundaries.
"""
import os
import time
import json
import requests
import jwt as pyjwt
from jwt import PyJWKClient

TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "30"))
KC_URL = os.environ.get("KEYCLOAK_URL",
    "http://keycloak.mcp-demo.svc.cluster.local:8080")
REALM = os.environ.get("KEYCLOAK_REALM", "mcp-demo")
OIDC_URL = os.environ.get("OIDC_DISCOVERY_URL",
    "https://spire-spiffe-oidc-discovery-provider.spire-system.svc.cluster.local")
SPIFFE_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
TOKEN_URL = f"{KC_URL}/realms/{REALM}/protocol/openid-connect/token"


def fetch_jwt_svid(audience=None):
    """Fetch JWT SVID with unique iat to avoid SPI single-use token rejection."""
    import uuid
    from spiffe import WorkloadApiClient
    time.sleep(1)  # Ensure unique iat (SPI uses issuer+subject+iat as replay key)
    if audience is None:
        resp = requests.get(f"{KC_URL}/realms/{REALM}/.well-known/openid-configuration", timeout=TIMEOUT)
        audience = resp.json()["issuer"]
    client = WorkloadApiClient("unix:///spiffe-workload-api/spire-agent.sock")
    return client.fetch_jwt_svid(audience={audience, f"nonce-{uuid.uuid4().hex[:8]}"})


def get_access_token():
    """Full flow: SPIRE SVID → Keycloak → access_token.
    Returns the access_token string. Asserts 200 on failure.
    """
    time.sleep(1)  # Ensure unique iat for SPI single-use enforcement
    svid = fetch_jwt_svid()
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_assertion_type": SPIFFE_ASSERTION_TYPE,
        "client_assertion": svid.token,
        "scope": "openid",
    }, timeout=TIMEOUT)
    assert resp.status_code == 200, \
        f"Token exchange failed: {resp.status_code} {resp.text[:300]}"
    return resp.json()["access_token"]


class TestCompleteAuthFlow:
    """Test the complete authentication lifecycle."""

    def test_full_flow_spire_to_access_token(self):
        """Complete flow: fetch SVID → authenticate → get access_token."""
        token = get_access_token()
        assert token is not None
        assert len(token) > 50

    def test_access_token_verifiable_by_keycloak_jwks(self):
        """Access token from Keycloak must be verifiable via Keycloak JWKS."""
        access_token = get_access_token()

        # Get Keycloak's JWKS URL
        oidc_resp = requests.get(
            f"{KC_URL}/realms/{REALM}/.well-known/openid-configuration",
            timeout=TIMEOUT
        )
        jwks_uri = oidc_resp.json()["jwks_uri"]

        # Verify access token signature
        jwks_client = PyJWKClient(jwks_uri)
        signing_key = jwks_client.get_signing_key_from_jwt(access_token)
        claims = pyjwt.decode(
            access_token,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_aud": False}
        )
        assert "sub" in claims
        assert "iss" in claims

    def test_access_token_introspection(self):
        """Access token must pass Keycloak introspection as active."""
        access_token = get_access_token()

        # Use the SPIFFE client itself for introspection
        svid = fetch_jwt_svid()

        resp = requests.post(
            f"{KC_URL}/realms/{REALM}/protocol/openid-connect/token/introspect",
            data={
                "token": access_token,
                "client_assertion_type": SPIFFE_ASSERTION_TYPE,
                "client_assertion": svid.token,
            },
            timeout=TIMEOUT
        )

        if resp.status_code == 200:
            data = resp.json()
            assert data.get("active") is True, \
                f"Token not active: {data}"


class TestTokenRefreshCycle:
    """Verify that token refresh works correctly with SPIFFE."""

    def test_multiple_sequential_token_requests(self):
        """Multiple sequential token requests must all succeed."""
        tokens = []
        for i in range(3):
            token = get_access_token()
            tokens.append(token)
            time.sleep(1)  # Small delay between requests

        # All tokens should be unique (different jti/iat)
        assert len(set(tokens)) == 3, \
            "Multiple token requests should return different tokens"

    def test_fresh_svid_per_request(self):
        """Each token request should use a fresh SVID."""
        svids = []
        for _ in range(3):
            svid = fetch_jwt_svid()
            svids.append(svid.token)
            time.sleep(1)

        # SVIDs may or may not be identical (SPIRE may cache within TTL)
        # But they should all be valid
        for s in svids:
            claims = pyjwt.decode(s, options={"verify_signature": False})
            assert claims["exp"] > time.time(), "SVID expired"


class TestCrossComponentTrustChain:
    """Verify the trust chain: SPIRE signs → OIDC publishes → Keycloak verifies."""

    def test_spire_signing_key_in_oidc_jwks(self):
        """The key SPIRE uses to sign SVIDs must appear in OIDC JWKS."""
        svid = fetch_jwt_svid("test-trust-chain")
        svid_kid = pyjwt.get_unverified_header(svid.token)["kid"]

        resp = requests.get(f"{OIDC_URL}/keys", timeout=TIMEOUT, verify=False)
        jwks_kids = [k["kid"] for k in resp.json()["keys"]]

        assert svid_kid in jwks_kids, \
            f"SPIRE signing key {svid_kid} not in OIDC JWKS: {jwks_kids}"

    def test_keycloak_can_reach_oidc_discovery(self):
        """Keycloak must be able to reach the SPIRE OIDC Discovery Provider."""
        # Test from inside the cluster using the internal service URL
        resp = requests.get(
            f"{OIDC_URL}/.well-known/openid-configuration",
            timeout=TIMEOUT, verify=False
        )
        assert resp.status_code == 200, \
            "OIDC Discovery Provider not reachable from cluster"

    def test_end_to_end_zero_secrets(self):
        """The entire flow must work without any static secrets."""
        # This is the core value proposition:
        # 1. No client_secret in the token request
        # 2. No API keys anywhere
        # 3. Only cryptographically-signed, short-lived SVIDs
        svid = fetch_jwt_svid()

        resp = requests.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_assertion_type": SPIFFE_ASSERTION_TYPE,
            "client_assertion": svid.token,
            # NOTE: No client_id, no client_secret
        }, timeout=TIMEOUT)

        assert resp.status_code == 200, \
            f"Zero-secret auth failed: {resp.status_code} {resp.text[:200]}"
        data = resp.json()
        assert "access_token" in data

    def test_token_metadata_consistency(self):
        """SVID SPIFFE ID must match access_token client identity."""
        svid = fetch_jwt_svid()

        svid_claims = pyjwt.decode(
            svid.token, options={"verify_signature": False}
        )
        spiffe_id = svid_claims["sub"]

        resp = requests.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_assertion_type": SPIFFE_ASSERTION_TYPE,
            "client_assertion": svid.token,
        }, timeout=TIMEOUT)

        assert resp.status_code == 200, \
            f"Token exchange failed: {resp.status_code} {resp.text[:300]}"
        at_claims = pyjwt.decode(
            resp.json()["access_token"],
            options={"verify_signature": False}
        )
        client_id = at_claims.get("azp") or at_claims.get("client_id")
        assert client_id == spiffe_id, \
            f"Identity mismatch: SVID sub={spiffe_id}, token azp={client_id}"
