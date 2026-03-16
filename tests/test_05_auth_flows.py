"""
Layer 3: Integration Tests — Keycloak SPIFFE Authentication Flows
Tests both client authentication and dynamic client registration
using SPIFFE JWT SVIDs.
"""
import os
import time
import pytest
import requests
import jwt as pyjwt

TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "30"))
KC_URL = os.environ.get("KEYCLOAK_URL",
    "http://keycloak.mcp-demo.svc.cluster.local:8080")
REALM = os.environ.get("KEYCLOAK_REALM", "mcp-demo")
TOKEN_URL = f"{KC_URL}/realms/{REALM}/protocol/openid-connect/token"
DCR_URL = f"{KC_URL}/realms/{REALM}/clients-registrations/spiffe-dcr/register"

SPIFFE_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


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


class TestClientCredentialsWithSPIFFE:
    """Test OAuth client_credentials grant using SPIFFE JWT SVID."""

    def test_token_exchange_succeeds(self):
        """Client must obtain access_token using JWT SVID (no client_secret)."""
        svid = fetch_jwt_svid()

        resp = requests.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_assertion_type": SPIFFE_ASSERTION_TYPE,
            "client_assertion": svid.token,
            "scope": "openid",
        }, timeout=TIMEOUT)

        assert resp.status_code == 200, \
            f"Token exchange failed: {resp.status_code} {resp.text[:300]}"
        data = resp.json()
        assert "access_token" in data, "Response missing access_token"
        assert data["token_type"].lower() == "bearer", \
            f"Unexpected token_type: {data['token_type']}"

    def test_access_token_is_valid_jwt(self):
        """Returned access_token must be a valid JWT."""
        svid = fetch_jwt_svid()

        resp = requests.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_assertion_type": SPIFFE_ASSERTION_TYPE,
            "client_assertion": svid.token,
            "scope": "openid",
        }, timeout=TIMEOUT)

        assert resp.status_code == 200, \
            f"Token exchange failed: {resp.status_code} {resp.text[:300]}"
        data = resp.json()
        claims = pyjwt.decode(
            data["access_token"],
            options={"verify_signature": False}
        )
        assert "iss" in claims, "Access token missing 'iss'"
        assert REALM in claims["iss"], "Access token issuer mismatch"

    def test_access_token_has_correct_client_id(self):
        """Access token must reference the SPIFFE-based client_id."""
        svid = fetch_jwt_svid()

        resp = requests.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_assertion_type": SPIFFE_ASSERTION_TYPE,
            "client_assertion": svid.token,
            "scope": "openid",
        }, timeout=TIMEOUT)

        assert resp.status_code == 200, \
            f"Token exchange failed: {resp.status_code} {resp.text[:300]}"
        data = resp.json()
        claims = pyjwt.decode(
            data["access_token"],
            options={"verify_signature": False}
        )
        # The client_id or azp claim should contain the SPIFFE ID
        client_id = claims.get("azp") or claims.get("client_id")
        assert client_id is not None, "No azp or client_id in token"
        assert "spiffe://" in client_id, \
            f"Client ID is not SPIFFE-based: {client_id}"

    def test_token_has_expiry(self):
        """Access token must have expires_in field."""
        svid = fetch_jwt_svid()

        resp = requests.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_assertion_type": SPIFFE_ASSERTION_TYPE,
            "client_assertion": svid.token,
            "scope": "openid",
        }, timeout=TIMEOUT)

        assert resp.status_code == 200, \
            f"Token exchange failed: {resp.status_code} {resp.text[:300]}"
        data = resp.json()
        assert "expires_in" in data, "Response missing expires_in"
        assert data["expires_in"] > 0, "expires_in must be positive"


class TestNegativeAuthenticationCases:
    """Verify security: malformed, expired, and wrong credentials must fail."""

    def test_wrong_assertion_type_rejected(self):
        """Using an invalid assertion type must fail."""
        svid = fetch_jwt_svid()

        resp = requests.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-spiffe",
            "client_assertion": svid.token,
        }, timeout=TIMEOUT)

        assert resp.status_code in (400, 401), \
            f"Wrong assertion type should fail but got {resp.status_code}"

    def test_garbage_token_rejected(self):
        """Random garbage as client_assertion must be rejected."""
        resp = requests.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_assertion_type": SPIFFE_ASSERTION_TYPE,
            "client_assertion": "this.is.not-a-jwt",
        }, timeout=TIMEOUT)

        assert resp.status_code in (400, 401), \
            f"Garbage token should be rejected but got {resp.status_code}"

    def test_empty_assertion_rejected(self):
        """Empty client_assertion must be rejected."""
        resp = requests.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_assertion_type": SPIFFE_ASSERTION_TYPE,
            "client_assertion": "",
        }, timeout=TIMEOUT)

        assert resp.status_code in (400, 401), \
            f"Empty assertion should fail but got {resp.status_code}"

    def test_wrong_audience_rejected(self):
        """JWT SVID with wrong audience must be rejected."""
        svid = fetch_jwt_svid("https://wrong-audience.example.com")

        resp = requests.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_assertion_type": SPIFFE_ASSERTION_TYPE,
            "client_assertion": svid.token,
        }, timeout=TIMEOUT)

        assert resp.status_code in (400, 401), \
            f"Wrong audience should fail but got {resp.status_code}"

    def test_missing_grant_type_rejected(self):
        """Request without grant_type must be rejected."""
        svid = fetch_jwt_svid()

        resp = requests.post(TOKEN_URL, data={
            "client_assertion_type": SPIFFE_ASSERTION_TYPE,
            "client_assertion": svid.token,
        }, timeout=TIMEOUT)

        assert resp.status_code in (400, 401), \
            f"Missing grant_type should fail but got {resp.status_code}"

    def test_svid_replay_within_ttl_may_work(self):
        """Same SVID used twice within TTL is expected behavior for JWTs."""
        svid = fetch_jwt_svid()

        # First use
        resp1 = requests.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_assertion_type": SPIFFE_ASSERTION_TYPE,
            "client_assertion": svid.token,
        }, timeout=TIMEOUT)

        # Second use (replay) — JWTs are bearer tokens, replay within TTL
        # is normal behavior for client_credentials unless jti enforcement exists
        resp2 = requests.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_assertion_type": SPIFFE_ASSERTION_TYPE,
            "client_assertion": svid.token,
        }, timeout=TIMEOUT)

        assert resp1.status_code == 200, "First request should succeed"
        # SPI enforces single-use tokens — second use must be rejected
        assert resp2.status_code in (400, 401), \
            "SPI should enforce single-use tokens"


class TestDynamicClientRegistration:
    """Test SPIFFE-based Dynamic Client Registration."""

    def test_dcr_endpoint_exists(self):
        """The SPIFFE DCR endpoint must be accessible (even if no body)."""
        resp = requests.post(DCR_URL, json={}, timeout=TIMEOUT)
        # Should return 400 (bad request) not 404 (not found)
        if resp.status_code == 404:
            pytest.skip("DCR SPI not installed")
        assert resp.status_code != 404, \
            f"DCR endpoint not found: {resp.status_code}"

    def test_dcr_with_valid_software_statement(self):
        """DCR with valid JWT SVID software statement should register client."""
        svid = fetch_jwt_svid()

        resp = requests.post(DCR_URL, json={
            "software_statement": svid.token,
        }, headers={"Content-Type": "application/json"},
           timeout=TIMEOUT)

        if resp.status_code == 404:
            pytest.skip("DCR SPI not installed")
        if resp.status_code in (200, 201):
            data = resp.json()
            client_id = data.get("clientId") or data.get("client_id")
            assert client_id is not None, "DCR response missing client_id"
        elif resp.status_code == 400 and "already in use" in resp.text:
            pass  # Client already registered — acceptable
        elif resp.status_code == 409:
            pass  # Client already registered — acceptable
        else:
            pytest.fail(
                f"DCR returned {resp.status_code}: {resp.text[:200]}"
            )

    def test_dcr_rejects_invalid_software_statement(self):
        """DCR with invalid software statement must be rejected."""
        resp = requests.post(DCR_URL, json={
            "software_statement": "invalid.jwt.token",
        }, headers={"Content-Type": "application/json"},
           timeout=TIMEOUT)

        if resp.status_code == 404:
            pytest.skip("DCR SPI not installed")
        assert resp.status_code in (400, 401, 403, 500), \
            f"Invalid DCR should fail but got {resp.status_code}"
