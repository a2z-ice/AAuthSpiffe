"""
Layer 2: SPIRE Component Tests
Validates SPIRE Server, Agent, OIDC Discovery Provider,
and Controller Manager are healthy and correctly configured.
"""
import subprocess
import json
import os
import pytest
import requests
import jwt  # PyJWT

TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "30"))
OIDC_URL = os.environ.get("OIDC_DISCOVERY_URL",
    "https://spire-spiffe-oidc-discovery-provider.spire-system.svc.cluster.local")


class TestSPIREServer:
    """SPIRE Server health and configuration tests."""

    def test_server_pod_running(self):
        """SPIRE Server pod must be in Running state."""
        result = subprocess.run(
            ["kubectl", "get", "pods", "-n", "spire-system",
             "-l", "app.kubernetes.io/name=server", "-o", "json"],
            capture_output=True, text=True, timeout=TIMEOUT
        )
        pods = json.loads(result.stdout)
        assert len(pods["items"]) >= 1, "No SPIRE server pods found"
        for pod in pods["items"]:
            assert pod["status"]["phase"] == "Running", \
                f"SPIRE server pod is {pod['status']['phase']}"

    def test_server_container_ready(self):
        """All containers in SPIRE Server pod must be ready."""
        result = subprocess.run(
            ["kubectl", "get", "pods", "-n", "spire-system",
             "-l", "app.kubernetes.io/name=server", "-o", "json"],
            capture_output=True, text=True, timeout=TIMEOUT
        )
        pods = json.loads(result.stdout)
        for pod in pods["items"]:
            for cs in pod["status"].get("containerStatuses", []):
                assert cs["ready"] is True, \
                    f"Container {cs['name']} not ready in SPIRE server"

    def test_trust_domain_configured(self):
        """SPIRE Server must be configured with trust domain example.org."""
        # Verify trust domain via JWT SVID subject claim
        from spiffe import WorkloadApiClient
        client = WorkloadApiClient("unix:///spiffe-workload-api/spire-agent.sock")
        svid = client.fetch_jwt_svid(audience={"test-trust-domain"})
        spiffe_id = str(svid.spiffe_id)
        assert "example.org" in spiffe_id, \
            f"Trust domain not in SPIFFE ID: {spiffe_id}"


class TestSPIREAgent:
    """SPIRE Agent health tests."""

    def test_agent_daemonset_running(self):
        """SPIRE Agent DaemonSet must have desired number of pods running."""
        result = subprocess.run(
            ["kubectl", "get", "ds", "-n", "spire-system",
             "-l", "app.kubernetes.io/name=agent", "-o", "json"],
            capture_output=True, text=True, timeout=TIMEOUT
        )
        ds_list = json.loads(result.stdout)
        assert len(ds_list["items"]) >= 1, "No SPIRE agent DaemonSet found"
        for ds in ds_list["items"]:
            desired = ds["status"]["desiredNumberScheduled"]
            ready = ds["status"]["numberReady"]
            assert ready == desired, \
                f"SPIRE Agent: {ready}/{desired} ready"

    def test_agent_can_issue_svid(self):
        """Agent must be able to issue a JWT SVID via Workload API."""
        from spiffe import WorkloadApiClient
        client = WorkloadApiClient("unix:///spiffe-workload-api/spire-agent.sock")
        svid = client.fetch_jwt_svid(audience={"test-validation"})
        assert svid is not None, "Failed to fetch JWT SVID"
        assert svid.spiffe_id is not None, "SVID has no SPIFFE ID"
        assert svid.token is not None, "SVID has no token"
        assert len(svid.token) > 50, "SVID token suspiciously short"


class TestOIDCDiscoveryProvider:
    """OIDC Discovery Provider tests."""

    def test_openid_configuration_endpoint(self):
        """/.well-known/openid-configuration must return valid JSON."""
        resp = requests.get(
            f"{OIDC_URL}/.well-known/openid-configuration",
            timeout=TIMEOUT, verify=False
        )
        assert resp.status_code == 200, \
            f"OIDC discovery returned {resp.status_code}"
        data = resp.json()
        required_keys = ["issuer", "jwks_uri", "id_token_signing_alg_values_supported"]
        for key in required_keys:
            assert key in data, f"Missing '{key}' in OIDC discovery response"

    def test_jwks_endpoint_returns_keys(self):
        """/keys endpoint must return at least one signing key."""
        resp = requests.get(f"{OIDC_URL}/keys", timeout=TIMEOUT, verify=False)
        assert resp.status_code == 200, \
            f"JWKS endpoint returned {resp.status_code}"
        jwks = resp.json()
        assert "keys" in jwks, "JWKS response missing 'keys' field"
        assert len(jwks["keys"]) >= 1, "JWKS contains no keys"
        # Verify key structure
        for key in jwks["keys"]:
            assert "kty" in key, "Key missing 'kty' field"
            assert "kid" in key, "Key missing 'kid' field"
            assert key["kty"] in ("RSA", "EC"), \
                f"Unexpected key type: {key['kty']}"

    def test_jwks_keys_are_public_only(self):
        """JWKS must only expose public keys, never private."""
        resp = requests.get(f"{OIDC_URL}/keys", timeout=TIMEOUT, verify=False)
        jwks = resp.json()
        private_fields = ["d", "p", "q", "dp", "dq", "qi"]
        for key in jwks["keys"]:
            for field in private_fields:
                assert field not in key, \
                    f"JWKS key {key.get('kid')} exposes private field '{field}'!"


class TestControllerManager:
    """SPIRE Controller Manager tests."""

    def test_controller_manager_running(self):
        """Controller Manager must be running as sidecar in SPIRE Server pod."""
        result = subprocess.run(
            ["kubectl", "get", "pods", "-n", "spire-system",
             "-l", "app.kubernetes.io/name=server", "-o", "json"],
            capture_output=True, text=True, timeout=TIMEOUT
        )
        pods = json.loads(result.stdout)
        assert len(pods["items"]) >= 1, "No SPIRE server pods found"
        for pod in pods["items"]:
            container_statuses = pod["status"].get("containerStatuses", [])
            assert len(container_statuses) >= 2, \
                f"SPIRE server should have >= 2 containers (server + controller-manager), got {len(container_statuses)}"
            all_ready = all(cs["ready"] for cs in container_statuses)
            assert all_ready, "Not all containers ready in SPIRE server pod"

    def test_cluster_spiffe_id_crds_exist(self):
        """ClusterSPIFFEID CRDs must be registered."""
        result = subprocess.run(
            ["kubectl", "get", "crd", "clusterspiffeids.spire.spiffe.io",
             "-o", "json"],
            capture_output=True, text=True, timeout=TIMEOUT
        )
        assert result.returncode == 0, \
            "ClusterSPIFFEID CRD not found"

    def test_mcp_client_spiffe_id_registered(self):
        """ClusterSPIFFEID for mcp-client must be applied."""
        result = subprocess.run(
            ["kubectl", "get", "clusterspiffeids", "mcp-client-id",
             "-o", "json"],
            capture_output=True, text=True, timeout=TIMEOUT
        )
        assert result.returncode == 0, \
            "mcp-client-id ClusterSPIFFEID not found"
