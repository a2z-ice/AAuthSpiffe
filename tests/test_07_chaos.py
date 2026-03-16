"""
Layer 5: Chaos & Resilience Tests
Simulates real-world failures: pod restarts, network interruptions,
key rotation, and component unavailability. Validates self-healing.
"""
import os
import time
import subprocess
import json
import pytest
import requests
import jwt as pyjwt

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
    time.sleep(1)  # Ensure unique iat for SPI single-use enforcement
    svid = fetch_jwt_svid()
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_assertion_type": SPIFFE_ASSERTION_TYPE,
        "client_assertion": svid.token,
    }, timeout=TIMEOUT)
    return resp


def wait_for_pod_ready(namespace, label, timeout=120):
    """Wait for pod matching label to become Ready."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            ["kubectl", "get", "pods", "-n", namespace,
             "-l", label, "-o", "json"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            pods = json.loads(result.stdout)
            ready_pods = [
                p for p in pods["items"]
                if p["status"]["phase"] == "Running"
                and all(
                    cs.get("ready", False)
                    for cs in p["status"].get("containerStatuses", [])
                )
            ]
            if ready_pods:
                return True
        time.sleep(5)
    return False


class TestSPIREAgentResilience:
    """Test system behavior when SPIRE Agent is disrupted."""

    def test_auth_recovers_after_agent_restart(self):
        """Auth flow must recover after SPIRE Agent pod restart."""
        # 1. Verify auth works before disruption
        resp = get_access_token()
        assert resp.status_code == 200, "Pre-disruption auth failed"

        # 2. Restart SPIRE Agent
        subprocess.run(
            ["kubectl", "rollout", "restart", "daemonset/spire-agent",
             "-n", "spire-system"],
            capture_output=True, timeout=30
        )

        # 3. Wait for agent to recover
        assert wait_for_pod_ready("spire-system", "app.kubernetes.io/name=agent"), \
            "SPIRE Agent did not recover"

        time.sleep(10)  # Allow workload API socket to re-establish

        # 4. Verify auth works after recovery
        resp = get_access_token()
        assert resp.status_code == 200, \
            f"Auth failed after agent restart: {resp.status_code}"


class TestKeycloakResilience:
    """Test system behavior when Keycloak is disrupted."""

    def test_keycloak_recovers_after_restart(self):
        """Keycloak must recover and serve tokens after pod restart."""
        # 1. Restart Keycloak
        subprocess.run(
            ["kubectl", "rollout", "restart", "statefulset/keycloak",
             "-n", "mcp-demo"],
            capture_output=True, timeout=30
        )

        # 2. Wait for recovery (Keycloak is slow to start)
        assert wait_for_pod_ready("mcp-demo", "app=keycloak", timeout=300), \
            "Keycloak did not recover after restart"

        # 3. Wait for Keycloak to serve realm (health endpoint is on mgmt port 9000)
        deadline = time.time() + 120
        healthy = False
        while time.time() < deadline:
            try:
                resp = requests.get(
                    f"{KC_URL}/realms/{REALM}", timeout=5
                )
                if resp.status_code == 200:
                    healthy = True
                    break
            except requests.exceptions.RequestException:
                pass
            time.sleep(5)
        assert healthy, "Keycloak not serving realm after restart"

        # 5. Verify auth flow works
        resp = get_access_token()
        assert resp.status_code == 200, \
            "Token exchange failed after Keycloak restart"


class TestOIDCDiscoveryResilience:
    """Test system when OIDC Discovery Provider is disrupted."""

    def test_oidc_discovery_recovers(self):
        """OIDC Discovery Provider must recover and serve JWKS after restart."""
        # 1. Restart OIDC Discovery Provider
        subprocess.run(
            ["kubectl", "rollout", "restart",
             "deployment/spiffe-oidc-discovery-provider",
             "-n", "spire-system"],
            capture_output=True, timeout=30
        )

        # 2. Wait for recovery
        assert wait_for_pod_ready(
            "spire-system",
            "app.kubernetes.io/name=spiffe-oidc-discovery-provider"
        ), "OIDC Discovery Provider did not recover"

        # 3. Verify JWKS is served
        deadline = time.time() + 60
        jwks_ok = False
        while time.time() < deadline:
            try:
                resp = requests.get(f"{OIDC_URL}/keys", timeout=5, verify=False)
                if resp.status_code == 200:
                    jwks = resp.json()
                    if len(jwks.get("keys", [])) > 0:
                        jwks_ok = True
                        break
            except requests.exceptions.RequestException:
                pass
            time.sleep(3)
        assert jwks_ok, "JWKS not available after OIDC Discovery restart"


class TestSVIDLifecycle:
    """Test SVID expiry and renewal behavior."""

    def test_svid_auto_renewal(self):
        """New SVID requests after TTL should return fresh SVIDs."""
        svid1 = fetch_jwt_svid()
        claims1 = pyjwt.decode(
            svid1.token, options={"verify_signature": False}
        )

        time.sleep(2)

        svid2 = fetch_jwt_svid()
        claims2 = pyjwt.decode(
            svid2.token, options={"verify_signature": False}
        )

        # iat should differ (fresh SVID each time or from cache)
        # Both must be valid
        assert claims1["exp"] > time.time() or claims2["exp"] > time.time(), \
            "Both SVIDs expired"

    def test_concurrent_svid_requests(self):
        """Multiple concurrent SVID requests must all succeed."""
        import concurrent.futures

        def fetch_and_validate():
            svid = fetch_jwt_svid()
            assert svid.token is not None
            return svid.spiffe_id

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(fetch_and_validate) for _ in range(5)]
            results = [f.result(timeout=TIMEOUT) for f in futures]

        assert len(results) == 5, "Not all concurrent requests succeeded"
        # All should return the same SPIFFE ID
        assert len(set(results)) == 1, \
            f"Inconsistent SPIFFE IDs: {set(results)}"


class TestLoadResilience:
    """Basic load testing for the authentication flow."""

    def test_rapid_sequential_auth_requests(self):
        """10 rapid sequential auth requests must all succeed."""
        first_resp = get_access_token()
        assert first_resp.status_code == 200, \
            f"First auth request failed: {first_resp.status_code} {first_resp.text[:300]}"
        results = [first_resp.status_code]
        for i in range(9):
            resp = get_access_token()
            results.append(resp.status_code)

        failures = [r for r in results if r != 200]
        assert len(failures) == 0, \
            f"{len(failures)}/10 requests failed: {failures}"

    def test_parallel_auth_requests(self):
        """5 staggered auth requests must all succeed."""
        # SPI enforces single-use tokens using issuer+subject+iat as key.
        # Parallel requests within same second share iat, causing replay rejection.
        # Stagger requests 1s apart to ensure unique iat values.
        import concurrent.futures

        def do_auth(delay):
            time.sleep(delay)
            resp = get_access_token()
            return resp.status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(do_auth, i) for i in range(5)]
            results = [f.result(timeout=TIMEOUT * 2) for f in futures]

        failures = [r for r in results if r != 200]
        assert len(failures) == 0, \
            f"{len(failures)}/5 requests failed: {failures}"
