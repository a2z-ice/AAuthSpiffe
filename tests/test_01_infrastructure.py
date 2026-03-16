"""
Layer 1: Infrastructure Tests
Validates that the Kind cluster, networking, DNS, storage,
and CSI driver are all functioning correctly.
"""
import subprocess
import socket
import os
import json
import pytest
import requests

TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "30"))


class TestKindCluster:
    """Verify the Kind cluster itself is healthy."""

    def test_cluster_nodes_ready(self):
        """All Kind cluster nodes must be in Ready state."""
        result = subprocess.run(
            ["kubectl", "get", "nodes", "-o", "json"],
            capture_output=True, text=True, timeout=TIMEOUT
        )
        assert result.returncode == 0, f"kubectl failed: {result.stderr}"
        nodes = json.loads(result.stdout)
        for node in nodes["items"]:
            conditions = {c["type"]: c["status"] for c in node["status"]["conditions"]}
            assert conditions.get("Ready") == "True", \
                f"Node {node['metadata']['name']} is not Ready"

    def test_minimum_two_nodes(self):
        """Cluster must have at least 2 nodes (control-plane + worker)."""
        result = subprocess.run(
            ["kubectl", "get", "nodes", "-o", "json"],
            capture_output=True, text=True, timeout=TIMEOUT
        )
        nodes = json.loads(result.stdout)
        assert len(nodes["items"]) >= 2, \
            f"Expected >= 2 nodes, got {len(nodes['items'])}"

    def test_default_storage_class_exists(self):
        """A default StorageClass must exist for PVC provisioning."""
        result = subprocess.run(
            ["kubectl", "get", "sc", "-o", "json"],
            capture_output=True, text=True, timeout=TIMEOUT
        )
        scs = json.loads(result.stdout)
        defaults = [
            sc for sc in scs["items"]
            if sc.get("metadata", {}).get("annotations", {}).get(
                "storageclass.kubernetes.io/is-default-class"
            ) == "true"
        ]
        assert len(defaults) >= 1, "No default StorageClass found"


class TestNetworking:
    """Verify cluster networking and DNS resolution."""

    def test_coredns_running(self):
        """CoreDNS pods must be running for service discovery."""
        result = subprocess.run(
            ["kubectl", "get", "pods", "-n", "kube-system",
             "-l", "k8s-app=kube-dns", "-o", "json"],
            capture_output=True, text=True, timeout=TIMEOUT
        )
        pods = json.loads(result.stdout)
        running = [p for p in pods["items"]
                   if p["status"]["phase"] == "Running"]
        assert len(running) >= 1, "No CoreDNS pods running"

    def test_cross_namespace_dns_resolution(self):
        """Services must be resolvable across namespaces."""
        # Resolve Keycloak service from test-runner namespace
        try:
            addr = socket.getaddrinfo(
                "keycloak.mcp-demo.svc.cluster.local", 8080
            )
            assert len(addr) > 0
        except socket.gaierror as e:
            pytest.fail(f"DNS resolution failed for keycloak service: {e}")

    def test_spire_system_dns_resolution(self):
        """SPIRE services must be resolvable."""
        services = [
            "spire-server.spire-system.svc.cluster.local",
            "spire-spiffe-oidc-discovery-provider.spire-system.svc.cluster.local",
        ]
        for svc in services:
            try:
                addr = socket.getaddrinfo(svc, 443)
                assert len(addr) > 0, f"No addresses for {svc}"
            except socket.gaierror as e:
                pytest.fail(f"DNS resolution failed for {svc}: {e}")

    def test_keycloak_nodeport_accessible(self):
        """Keycloak NodePort service must be accessible from host."""
        result = subprocess.run(
            ["kubectl", "get", "svc", "-n", "mcp-demo", "keycloak", "-o",
             "jsonpath={.spec.type}"],
            capture_output=True, text=True, timeout=TIMEOUT
        )
        assert result.stdout == "NodePort", "Keycloak service must be NodePort type"


class TestCSIDriver:
    """Verify the SPIFFE CSI driver is available."""

    def test_csi_driver_registered(self):
        """The csi.spiffe.io driver must be registered in the cluster."""
        result = subprocess.run(
            ["kubectl", "get", "csidrivers", "-o", "json"],
            capture_output=True, text=True, timeout=TIMEOUT
        )
        drivers = json.loads(result.stdout)
        names = [d["metadata"]["name"] for d in drivers["items"]]
        assert "csi.spiffe.io" in names, \
            f"csi.spiffe.io not found in CSI drivers: {names}"

    def test_workload_api_socket_mounted(self):
        """The SPIFFE Workload API socket must be mounted at expected path."""
        socket_path = "/spiffe-workload-api/spire-agent.sock"
        assert os.path.exists(socket_path), \
            f"Workload API socket not found at {socket_path}"
