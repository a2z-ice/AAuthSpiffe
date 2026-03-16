# SPIFFE-MCP-OAuth Implementation Plan

## Progressive Deployment Guide — Canonical Phase Order

This document tracks the phased implementation of the SPIFFE/SPIRE + Keycloak + MCP OAuth
authentication system inside a Kind cluster on macOS.

---

## Phase 0: File Extraction (Completed)

**Goal:** Extract all configuration, manifests, scripts, and test files from the spec.

**Files created:**
- `infra/kind-config.yaml` — Kind cluster config with NodePort mappings
- `infra/spire-values.yaml` — SPIRE Helm chart values
- `docker/Dockerfile.keycloak-custom` — Multi-stage build for Keycloak + SPIFFE SPIs
- `docker/Dockerfile.spire-plugin` — SPIRE software-statements plugin image
- `manifests/postgres.yaml` — PostgreSQL StatefulSet + Service + Secret
- `manifests/realm-config.json` — Keycloak realm configuration (standalone)
- `manifests/keycloak.yaml` — ConfigMap (inlined realm), StatefulSet, NodePort Service
- `manifests/oidc-nodeport.yaml` — OIDC Discovery Provider NodePort
- `manifests/cluster-spiffe-ids.yaml` — ClusterSPIFFEID for mcp-client and mcp-server
- `manifests/mcp-client.yaml` — MCP client Deployment + ServiceAccount
- `manifests/mcp-server.yaml` — MCP server Deployment + ServiceAccount
- `manifests/test-runner.yaml` — Test runner Deployment + RBAC
- `scripts/test-spiffe-auth.sh` — Demo script (runs inside mcp-client pod)
- `scripts/run-all-tests.sh` — Full test suite orchestrator
- `scripts/continuous-validation.sh` — Lightweight health check
- `deploy-all.sh` — Full deployment script
- `teardown.sh` — Cluster teardown
- `tests/test-bootstrap.sh` — Test dependency installer
- `tests/test_01_infrastructure.py` — L1: Kind cluster, DNS, storage, CSI
- `tests/test_02_spire_components.py` — L2: SPIRE Server, Agent, OIDC, Controller
- `tests/test_03_keycloak.py` — L2: Keycloak health, realm, SPIFFE client, SPIs
- `tests/test_04_jwt_svid.py` — L3: JWT structure, claims, crypto verification
- `tests/test_05_auth_flows.py` — L3: Client credentials, negative cases, DCR
- `tests/test_06_e2e_flows.py` — L4: Full chain, introspection, trust chain
- `tests/test_07_chaos.py` — L5: Resilience, pod restarts, concurrent load
- `tests/test_08_security_hardening.py` — L5: No secrets, isolation, JWKS security
- `implementation-state.json` — Phase tracking state file

**Status:** COMPLETE

---

## Phase 1: Infrastructure

**Spec Sections:** 3, 4
**Prerequisite:** Docker, kind, kubectl, helm, jq installed

### Steps

1. Verify prerequisites:
   ```bash
   docker version && kind version && kubectl version --client && helm version && jq --version
   ```

2. Create Kind cluster:
   ```bash
   kind create cluster --config infra/kind-config.yaml
   ```

3. Verify cluster:
   ```bash
   kubectl get nodes --context kind-spiffe-mcp-demo
   ```

### Validation Gate
- 2 nodes in Ready state (control-plane + worker)
- NodePort mappings active (30080, 30443)

**Status:** NOT STARTED

---

## Phase 2: SPIRE Stack

**Spec Sections:** 5
**Prerequisite:** Phase 1 complete

### Steps

1. Add SPIRE Helm repo:
   ```bash
   helm repo add spiffe https://spiffe.github.io/helm-charts-hardened/ && helm repo update
   ```

2. Install SPIRE CRDs:
   ```bash
   helm upgrade --install --create-namespace -n spire-system spire-crds spire-crds \
     --repo https://spiffe.github.io/helm-charts-hardened/ --wait
   ```

3. Install SPIRE stack:
   ```bash
   helm upgrade --install -n spire-system spire spire \
     --repo https://spiffe.github.io/helm-charts-hardened/ \
     --values infra/spire-values.yaml --wait --timeout 300s
   ```

4. Apply ClusterSPIFFEIDs:
   ```bash
   kubectl apply -f manifests/cluster-spiffe-ids.yaml
   ```

### Validation Gate
- All pods Running in `spire-system`
- `spire-server healthcheck` returns healthy
- OIDC Discovery endpoint responds

**Status:** NOT STARTED

---

## Phase 3: Custom Builds

**Spec Sections:** 6
**Prerequisite:** Phase 2 complete

### Steps

1. Build custom Keycloak image:
   ```bash
   docker build -f docker/Dockerfile.keycloak-custom -t keycloak-spiffe:latest .
   ```

2. Build SPIRE plugin image:
   ```bash
   docker build -f docker/Dockerfile.spire-plugin -t spire-software-statements:latest .
   ```

3. Load images into Kind:
   ```bash
   kind load docker-image keycloak-spiffe:latest --name spiffe-mcp-demo
   kind load docker-image spire-software-statements:latest --name spiffe-mcp-demo
   ```

### Validation Gate
- `docker images | grep keycloak-spiffe` shows image
- `docker images | grep spire-software-statements` shows image

**Status:** NOT STARTED

---

## Phase 4: Keycloak + Data

**Spec Sections:** 7, 8, 10
**Prerequisite:** Phase 3 complete

### Steps

1. Deploy PostgreSQL:
   ```bash
   kubectl apply -f manifests/postgres.yaml
   kubectl -n mcp-demo wait --for=condition=ready pod -l app=postgres --timeout=120s
   ```

2. Deploy Keycloak:
   ```bash
   kubectl apply -f manifests/keycloak.yaml
   kubectl -n mcp-demo wait --for=condition=ready pod -l app=keycloak --timeout=300s
   ```

3. Deploy OIDC NodePort:
   ```bash
   kubectl apply -f manifests/oidc-nodeport.yaml
   ```

### Validation Gate
- `curl -s http://localhost:30080/health/ready` returns `{"status":"UP"}`
- Realm accessible at `http://localhost:30080/realms/mcp-demo`
- Admin console at `http://localhost:30080/admin` (admin/admin)

**Status:** NOT STARTED

---

## Phase 5: Workloads + Validation

**Spec Sections:** 5.5, 9, 13
**Prerequisite:** Phase 4 complete

### Steps

1. Deploy MCP workloads:
   ```bash
   kubectl apply -f manifests/mcp-client.yaml
   kubectl apply -f manifests/mcp-server.yaml
   kubectl -n mcp-demo wait --for=condition=ready pod -l app=mcp-client --timeout=120s
   ```

2. Run demo script:
   ```bash
   kubectl -n mcp-demo exec -it deploy/mcp-client -- bash /scripts/test-spiffe-auth.sh
   ```

3. Deploy test runner and run tests:
   ```bash
   kubectl apply -f manifests/test-runner.yaml
   ./scripts/run-all-tests.sh
   ```

### Validation Gate
- MCP client can fetch JWT SVID with SPIFFE ID `spiffe://example.org/ns/mcp-demo/sa/mcp-client`
- `test-spiffe-auth.sh` completes successfully
- All 73 tests pass

**Status:** NOT STARTED

---

## Test Matrix Summary

| Test File | Tests | Layer | Coverage |
|-----------|-------|-------|----------|
| `test_01_infrastructure.py` | 7 | L1 | Kind nodes, DNS, StorageClass, CoreDNS, NodePort, CSI |
| `test_02_spire_components.py` | 9 | L2 | SPIRE Server, Agent, OIDC Discovery, Controller Manager |
| `test_03_keycloak.py` | 10 | L2 | Health, PostgreSQL, Admin, Realm, SPIFFE client, SPIs |
| `test_04_jwt_svid.py` | 14 | L3 | JWT structure, claims, crypto, tamper detection |
| `test_05_auth_flows.py` | 10 | L3 | Client credentials, negative security tests, DCR |
| `test_06_e2e_flows.py` | 9 | L4 | Full chain, introspection, trust chain, zero-secrets |
| `test_07_chaos.py` | 8 | L5 | Agent/Keycloak/OIDC restart, concurrent load |
| `test_08_security_hardening.py` | 6 | L5 | No static secrets, isolation, JWKS security |
| **Total** | **73** | **L1-L5** | **Complete coverage** |
