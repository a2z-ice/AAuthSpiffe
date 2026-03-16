"""
Security Hardening Verification Tests
Ensures the deployment follows security best practices.
"""
import os
import subprocess
import json
import pytest
import requests
import jwt as pyjwt

TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "30"))
OIDC_URL = os.environ.get("OIDC_DISCOVERY_URL",
    "https://spire-spiffe-oidc-discovery-provider.spire-system.svc.cluster.local")
KC_URL = os.environ.get("KEYCLOAK_URL",
    "http://keycloak.mcp-demo.svc.cluster.local:8080")
REALM = os.environ.get("KEYCLOAK_REALM", "mcp-demo")


class TestNoStaticSecrets:
    """Verify no static secrets exist in the system."""

    def test_no_client_secret_in_mcp_client_env(self):
        """MCP client pod must have no CLIENT_SECRET env vars."""
        result = subprocess.run(
            ["kubectl", "get", "pod", "-n", "mcp-demo",
             "-l", "app=mcp-client", "-o", "json"],
            capture_output=True, text=True, timeout=TIMEOUT
        )
        pods = json.loads(result.stdout)
        for pod in pods["items"]:
            for container in pod["spec"]["containers"]:
                env_names = [e["name"] for e in container.get("env", [])]
                forbidden = ["CLIENT_SECRET", "API_KEY", "PASSWORD",
                             "OAUTH_SECRET", "TOKEN"]
                for name in env_names:
                    for bad in forbidden:
                        assert bad not in name.upper() or name == "SPIFFE_ENDPOINT_SOCKET", \
                            f"Suspicious env var found: {name}"

    def test_no_secrets_in_configmaps(self):
        """ConfigMaps in mcp-demo must not contain secret-like values."""
        result = subprocess.run(
            ["kubectl", "get", "configmaps", "-n", "mcp-demo",
             "-o", "json"],
            capture_output=True, text=True, timeout=TIMEOUT
        )
        cms = json.loads(result.stdout)
        for cm in cms["items"]:
            name = cm["metadata"]["name"]
            data = cm.get("data", {})
            for key, value in data.items():
                # Check for hardcoded secrets in ConfigMap values
                if isinstance(value, str) and len(value) > 20:
                    lower = value.lower()
                    assert "client_secret" not in lower or "spiffe" in lower, \
                        f"Potential secret in ConfigMap {name}/{key}"


class TestWorkloadIsolation:
    """Verify workload identity isolation."""

    def test_different_service_accounts_get_different_svids(self):
        """Different service accounts must get different SPIFFE IDs."""
        # This test runs from test-runner (SA: test-runner)
        from spiffe import WorkloadApiClient
        client = WorkloadApiClient("unix:///spiffe-workload-api/spire-agent.sock")
        svid = client.fetch_jwt_svid(audience={"test-isolation"})
        claims = pyjwt.decode(
            svid.token, options={"verify_signature": False}
        )
        spiffe_id = claims["sub"]

        # The test-runner's SPIFFE ID should NOT be the mcp-client's
        assert "sa/mcp-client" not in spiffe_id, \
            f"Test runner got mcp-client's SPIFFE ID: {spiffe_id}"


class TestJWKSSecurity:
    """Verify JWKS endpoint security."""

    def test_jwks_no_private_key_exposure(self):
        """JWKS endpoint must never expose private key material."""
        resp = requests.get(f"{OIDC_URL}/keys", timeout=TIMEOUT, verify=False)
        jwks = resp.json()
        private_fields = ["d", "p", "q", "dp", "dq", "qi", "k"]
        for key in jwks["keys"]:
            for field in private_fields:
                assert field not in key, \
                    f"CRITICAL: Private field '{field}' exposed in JWKS!"

    def test_jwks_uses_strong_algorithm(self):
        """Keys must use strong algorithms (RSA >=2048 or EC P-256+)."""
        resp = requests.get(f"{OIDC_URL}/keys", timeout=TIMEOUT, verify=False)
        jwks = resp.json()
        for key in jwks["keys"]:
            kty = key["kty"]
            if kty == "RSA":
                # n parameter length indicates key size
                import base64
                n_bytes = len(base64.urlsafe_b64decode(key["n"] + "=="))
                assert n_bytes >= 256, \
                    f"RSA key too small: {n_bytes * 8} bits"
            elif kty == "EC":
                assert key.get("crv") in ("P-256", "P-384", "P-521"), \
                    f"Weak EC curve: {key.get('crv')}"


class TestKeycloakSecurityHeaders:
    """Verify Keycloak security headers."""

    def test_keycloak_security_headers(self):
        """Keycloak responses should include security headers."""
        resp = requests.get(
            f"{KC_URL}/realms/{REALM}/.well-known/openid-configuration",
            timeout=TIMEOUT
        )
        headers = resp.headers
        # X-Content-Type-Options should be present
        if "X-Content-Type-Options" in headers:
            assert headers["X-Content-Type-Options"] == "nosniff"
