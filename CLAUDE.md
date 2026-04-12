# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **specification-driven project** — the repository centers on a single document (`SPIFFE-MCP-OAuth-KindCluster-Spec.md`, ~1400 lines) that provides a complete implementation guide for deploying a SPIFFE/SPIRE + Keycloak + MCP OAuth authentication system inside a Kind (Kubernetes-in-Docker) cluster on macOS.

The system authenticates MCP (Model Context Protocol) OAuth clients using cryptographically verifiable SPIFFE identities instead of static client secrets, following IETF draft `draft-ietf-oauth-spiffe-client-auth` and Christian Posta's reference implementation.

## Architecture

**Core components (all run inside a Kind cluster named `spiffe-mcp-demo`):**

- **SPIRE Server/Agent** (Helm chart, namespace `spire-system`): Issues short-lived JWT SVIDs to workloads. Trust domain: `example.org`
- **SPIFFE OIDC Discovery Provider**: Serves JWKS so Keycloak can validate JWT SVIDs
- **Keycloak** (namespace `mcp-demo`): OAuth2/OIDC Authorization Server with two custom SPIs:
  - `spiffe-svid-client-authenticator` (Java) — validates JWT SVIDs as client assertions
  - `spiffe-dcr-keycloak` (Java) — Dynamic Client Registration using SPIFFE software statements
- **SPIRE software-statements plugin** (Go) — CredentialComposer that enriches JWT SVIDs with DCR claims
- **MCP Client/Server pods** (Python 3.12, namespace `mcp-demo`): Demo workloads
- **OIDC HTTP Proxy** (nginx, namespace `spire-system`): Terminates TLS from the OIDC Discovery Provider so Keycloak can reach JWKS over plain HTTP inside the cluster
- **PostgreSQL**: Keycloak backend datastore
- Services exposed via **NodePort** + Kind `extraPortMappings` (no ingress controller or port-forward)

**Two authentication flows:**
1. **Flow A (DCR)**: MCP client gets JWT SVID → POSTs to Keycloak's SPIFFE DCR endpoint → auto-registers as OAuth client
2. **Flow B (Token exchange)**: MCP client uses JWT SVID as `client_assertion` (type `urn:ietf:params:oauth:client-assertion-type:jwt-spiffe`) instead of `client_secret` to get access tokens

**Dependency chain:** Kind cluster → SPIRE CRDs → SPIRE Stack → PostgreSQL → Keycloak → ClusterSPIFFEIDs → MCP workloads → test script

## Key External Dependencies

- `github.com/christian-posta/spiffe-svid-client-authenticator` (Java/Maven)
- `github.com/christian-posta/spiffe-dcr-keycloak` (Java/Maven)
- `github.com/christian-posta/spire-software-statements` (Go 1.21+)

## Implementation: Phased Execution

The spec is designed to be executed in **5 ordered phases**, each completable in one session:

| Phase | Name | Spec Sections | Checkpoint |
|-------|------|---------------|------------|
| 1 | Infrastructure | 3, 4 | Kind cluster running with NodePort mappings |
| 2 | SPIRE Stack | 5 | All SPIRE pods Running, healthcheck passing |
| 3 | Custom Builds | 6 | Docker images built and loaded into Kind |
| 4 | Keycloak + Data | 7, 8, 10 | Keycloak admin accessible, realm imported |
| 5 | Workloads + Validation | 5.5, 9, 13 | MCP pods running, test-spiffe-auth.sh passes |

**State tracking:** Use `implementation-state.json` to persist progress between sessions. At session start, read it and run the validation gate for the last completed phase before continuing.

## Common Commands

```bash
# Prerequisites check
docker version && kind version && kubectl version --client && helm version && jq --version

# Cluster lifecycle
kind create cluster --config kind-config.yaml
kind delete cluster --name spiffe-mcp-demo

# SPIRE deployment
helm repo add spiffe https://spiffe.github.io/helm-charts-hardened/ && helm repo update
helm upgrade --install spire spiffe/spire -n spire-system --create-namespace -f spire-values.yaml

# Load custom images into Kind
kind load docker-image keycloak-spiffe:latest --name spiffe-mcp-demo

# Full deploy/teardown (once files are extracted from spec)
./deploy-all.sh
./teardown.sh

# Configure Keycloak SPIFFE auth flow (after Keycloak is ready)
./scripts/configure-keycloak.sh

# Health check — quick pass/fail for all components
./scripts/continuous-validation.sh

# Run full test suite (executes inside test-runner pod)
./scripts/run-all-tests.sh

# Run a single test file inside the test-runner pod
kubectl -n mcp-demo exec deploy/test-runner -- python3 -m pytest /tests/test_01_infrastructure.py -v

# Run the SPIFFE auth demo (inside mcp-client pod)
kubectl -n mcp-demo exec deploy/mcp-client -- bash /scripts/test-spiffe-auth.sh
```

## Operational Guidelines

**Context management:** Do NOT read the entire spec at once. Load only the current phase's sections using targeted line ranges.

**Command output:** Truncate Kubernetes output aggressively — use `| head -60`, `--tail=30`, `-o wide`, and `| grep <key>` instead of dumping full resources.

**Idempotency:** Always use `kubectl apply` (never `create`), `helm upgrade --install`, and `kind get clusters | grep -q ... || kind create cluster`.

**Error diagnosis — tiered approach (do not blindly retry):**
1. `kubectl get pods -n <ns> | grep -v Running`
2. `kubectl get events -n <ns> --sort-by=.lastTimestamp | tail -10`
3. `kubectl logs <pod> --tail=30 -n <ns>`
4. `kubectl describe <resource> -n <ns> | tail -40`

**Common failures:**

| Symptom | Fix |
|---------|-----|
| `ImagePullBackOff` on keycloak-spiffe | `kind load docker-image keycloak-spiffe:latest --name spiffe-mcp-demo` |
| SPIRE Agent `CrashLoopBackOff` | Wait 60s, check server is ready first |
| CSI Driver `FailedMount` | Verify SPIRE Agent DaemonSet is running on the node |
| Keycloak `Connection refused` to postgres | Wait for PostgreSQL readiness |

## Validation Gates

Run these before proceeding to the next phase:

- **Phase 1:** `kubectl get nodes --context kind-spiffe-mcp-demo` (2 nodes Ready)
- **Phase 2:** All pods Running in `spire-system` + `spire-server healthcheck` returns healthy
- **Phase 3:** `docker images | grep keycloak-spiffe` shows image exists
- **Phase 4:** `curl -s http://localhost:30080/health/ready` returns `{"status":"UP"}`
- **Phase 5:** MCP client can fetch JWT SVID with SPIFFE ID `spiffe://example.org/ns/mcp-demo/sa/mcp-client`

## Key Configuration Values

- Kind cluster name: `spiffe-mcp-demo`
- Trust domain: `example.org`
- SPIFFE ID format: `spiffe://example.org/ns/{namespace}/sa/{service-account}`
- Keycloak realm: `mcp-demo`
- JWT SVID TTL: 5 minutes
- Workload API socket: `unix:///spiffe-workload-api/spire-agent.sock`
- OIDC issuer: `https://oidc-discovery.example.org`
- Keycloak host access: `http://localhost:30080`
- OIDC Discovery host access: `http://localhost:30443`
- Namespaces: `spire-system`, `mcp-demo`

## Known Issues

- **Keycloak 26 SPI incompatibility**: The `spiffe-svid-client-authenticator` SPI (`client-spiffe-jwt`) cannot resolve clients with SPIFFE URI clientIds on Keycloak 26. The `resolveClientId` method needs updating.
- **SPIRE software-statements Go plugin**: Has build errors (missing `go.sum` entries) — was skipped during implementation.
- **Keycloak health endpoint**: Responds on management port 9000, not the main port 8080. Use `http://localhost:30080/health/ready` from host (NodePort routes to 9000).

## Test Suite

Tests run inside a `test-runner` pod (namespace `mcp-demo`) using pytest. Test files are organized in layers:

| File | Layer | What it validates |
|------|-------|-------------------|
| `test_01_infrastructure.py` | L1 | Kind cluster, DNS, storage, CSI driver |
| `test_02_spire_components.py` | L2 | SPIRE Server, Agent, OIDC, Controller |
| `test_03_keycloak.py` | L2 | Keycloak health, realm, SPIFFE client, SPIs |
| `test_04_jwt_svid.py` | L3 | JWT structure, claims, crypto verification |
| `test_05_auth_flows.py` | L3 | Client credentials, negative cases, DCR |
| `test_06_e2e_flows.py` | L4 | Full chain, introspection, trust chain |
| `test_07_chaos.py` | L5 | Resilience, pod restarts, concurrent load |
| `test_08_security_hardening.py` | L5 | No secrets, isolation, JWKS security |

Tests use `subprocess` to call `kubectl` and `requests` for HTTP assertions — they are not standard unit tests but in-cluster integration/e2e tests.

## Documentation Site

HTML docs are in `docs/` and deployed via GitHub Pages (`.github/workflows/static.yml`). Includes architecture diagrams, auth flow walkthroughs, deployment guide, and troubleshooting.
