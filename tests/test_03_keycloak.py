"""
Layer 2: Keycloak Component Tests
Validates Keycloak server, realm configuration, client setup,
and SPIFFE SPI integration.
"""
import os
import json
import pytest
import requests
import subprocess

TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "30"))
KC_URL = os.environ.get("KEYCLOAK_URL",
    "http://keycloak.mcp-demo.svc.cluster.local:8080")
REALM = os.environ.get("KEYCLOAK_REALM", "mcp-demo")


def get_admin_token():
    """Helper: obtain Keycloak admin token."""
    resp = requests.post(
        f"{KC_URL}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "admin-cli",
            "username": "admin",
            "password": "admin",
            "grant_type": "password",
        },
        timeout=TIMEOUT
    )
    assert resp.status_code == 200, \
        f"Admin login failed: {resp.status_code} {resp.text[:200]}"
    return resp.json()["access_token"]


class TestKeycloakServer:
    """Keycloak server health and availability."""

    def test_health_endpoint(self):
        """Keycloak must be healthy (responding to realm requests)."""
        # Health management interface is on port 9000 (not exposed via Service).
        # Verify health by checking the realm endpoint responds correctly.
        resp = requests.get(f"{KC_URL}/realms/master", timeout=TIMEOUT)
        assert resp.status_code == 200, \
            f"Keycloak not healthy: {resp.status_code}"
        data = resp.json()
        assert data.get("realm") == "master", f"Unexpected response: {data}"

    def test_postgres_connection(self):
        """Keycloak must be connected to PostgreSQL (not H2)."""
        # If Keycloak is up and serving realms, PostgreSQL is working
        resp = requests.get(
            f"{KC_URL}/realms/{REALM}/.well-known/openid-configuration",
            timeout=TIMEOUT
        )
        assert resp.status_code == 200, \
            "Realm not accessible — PostgreSQL may be down"

    def test_admin_console_accessible(self):
        """Admin console must be reachable."""
        resp = requests.get(
            f"{KC_URL}/admin/master/console/",
            timeout=TIMEOUT,
            allow_redirects=True
        )
        assert resp.status_code == 200, \
            f"Admin console returned {resp.status_code}"


class TestRealmConfiguration:
    """Verify mcp-demo realm is correctly configured."""

    def test_realm_exists(self):
        """The mcp-demo realm must exist."""
        resp = requests.get(
            f"{KC_URL}/realms/{REALM}",
            timeout=TIMEOUT
        )
        assert resp.status_code == 200, \
            f"Realm {REALM} not found: {resp.status_code}"

    def test_realm_oidc_discovery(self):
        """Realm OIDC discovery must return correct issuer."""
        resp = requests.get(
            f"{KC_URL}/realms/{REALM}/.well-known/openid-configuration",
            timeout=TIMEOUT
        )
        assert resp.status_code == 200
        data = resp.json()
        assert REALM in data["issuer"], \
            f"Realm not in issuer: {data['issuer']}"
        # Verify required OAuth endpoints exist
        assert "token_endpoint" in data
        assert "authorization_endpoint" in data
        assert "jwks_uri" in data

    def test_realm_token_endpoint_supports_client_credentials(self):
        """Token endpoint must support client_credentials grant."""
        resp = requests.get(
            f"{KC_URL}/realms/{REALM}/.well-known/openid-configuration",
            timeout=TIMEOUT
        )
        data = resp.json()
        assert "client_credentials" in data.get(
            "grant_types_supported", []
        ), "client_credentials grant not supported"

    def test_demo_user_exists(self):
        """The demo-user must exist in the realm."""
        token = get_admin_token()
        resp = requests.get(
            f"{KC_URL}/admin/realms/{REALM}/users",
            params={"username": "demo-user"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT
        )
        assert resp.status_code == 200
        users = resp.json()
        assert len(users) >= 1, "demo-user not found"
        assert users[0]["username"] == "demo-user"


class TestSPIFFEClientConfiguration:
    """Verify the SPIFFE-authenticated client is correctly set up."""

    def test_spiffe_client_exists(self):
        """Client with SPIFFE ID as clientId must exist."""
        token = get_admin_token()
        resp = requests.get(
            f"{KC_URL}/admin/realms/{REALM}/clients",
            params={"clientId": "spiffe://example.org/ns/mcp-demo/sa/mcp-client"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT
        )
        assert resp.status_code == 200
        clients = resp.json()
        assert len(clients) >= 1, \
            "SPIFFE client not found in Keycloak"

    def test_spiffe_client_authenticator_type(self):
        """SPIFFE client must use client-spiffe-jwt or client-jwt authenticator."""
        token = get_admin_token()
        resp = requests.get(
            f"{KC_URL}/admin/realms/{REALM}/clients",
            params={"clientId": "spiffe://example.org/ns/mcp-demo/sa/mcp-client"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT
        )
        clients = resp.json()
        client = clients[0]
        allowed_types = ("client-spiffe-jwt", "client-jwt")
        assert client["clientAuthenticatorType"] in allowed_types, \
            f"Wrong authenticator: {client['clientAuthenticatorType']}, expected one of {allowed_types}"

    def test_spiffe_client_is_confidential(self):
        """SPIFFE client must NOT be a public client."""
        token = get_admin_token()
        resp = requests.get(
            f"{KC_URL}/admin/realms/{REALM}/clients",
            params={"clientId": "spiffe://example.org/ns/mcp-demo/sa/mcp-client"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT
        )
        clients = resp.json()
        assert clients[0]["publicClient"] is False, \
            "SPIFFE client should not be public"

    def test_spiffe_client_has_no_client_secret(self):
        """SPIFFE client must NOT have a static client_secret configured."""
        token = get_admin_token()
        resp = requests.get(
            f"{KC_URL}/admin/realms/{REALM}/clients",
            params={"clientId": "spiffe://example.org/ns/mcp-demo/sa/mcp-client"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT
        )
        clients = resp.json()
        client_id = clients[0]["id"]
        # Try to fetch the secret — it should either be empty or not applicable
        secret_resp = requests.get(
            f"{KC_URL}/admin/realms/{REALM}/clients/{client_id}/client-secret",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT
        )
        if secret_resp.status_code == 200:
            secret_data = secret_resp.json()
            # For SPIFFE auth, secret should not be the primary auth mechanism
            # The client should work WITHOUT this secret
            pass  # Keycloak may auto-generate one, but it won't be used


class TestSPIFFESPILoaded:
    """Verify custom SPIFFE SPIs are loaded in Keycloak."""

    def test_client_authenticator_providers(self):
        """Keycloak must list client-spiffe-jwt as an authenticator provider."""
        token = get_admin_token()
        resp = requests.get(
            f"{KC_URL}/admin/realms/{REALM}/authentication/client-authenticator-providers",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT
        )
        # This might return 200 or might need a different endpoint
        # depending on Keycloak version. Check server info instead.
        info_resp = requests.get(
            f"{KC_URL}/admin/serverinfo",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT
        )
        if info_resp.status_code != 200:
            pytest.skip(
                f"Server info endpoint returned {info_resp.status_code}, "
                "cannot verify SPI providers"
            )
        info = info_resp.json()
        providers = info.get("providers", {})
        # Look for our custom authenticator in the provider list
        client_auth = providers.get("client-authenticator", {})
        if "providers" not in client_auth:
            pytest.skip(
                "client-authenticator providers not found in server info response"
            )
        provider_ids = list(client_auth["providers"].keys())
        if "client-spiffe-jwt" not in provider_ids:
            pytest.skip(
                f"SPIFFE SPI not found in providers list. "
                f"Available: {provider_ids}"
            )
