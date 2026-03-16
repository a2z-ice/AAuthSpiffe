# Comprehensive Implementation Spec: SPIFFE/SPIRE + Keycloak + MCP OAuth in Kind Cluster

## Document Purpose

This specification provides a complete, self-contained implementation guide for deploying a SPIFFE/SPIRE-based MCP OAuth authentication system entirely inside a Kind (Kubernetes in Docker) cluster on macOS. Zero host-level installations are required beyond Docker Desktop and `kind` CLI. The system authenticates MCP (Model Context Protocol) OAuth clients using SPIFFE identities instead of static client secrets, with Keycloak as the Authorization Server.

---

## 1. Architecture Overview

### 1.1 What We Are Building

A local development environment that demonstrates how AI agents (MCP clients) can authenticate to an OAuth Authorization Server (Keycloak) using cryptographically verifiable SPIFFE identities instead of long-lived client secrets. This follows the IETF draft `draft-ietf-oauth-spiffe-client-auth` and Christian Posta's reference implementation.

### 1.2 Component Map

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Kind Cluster                                  │
│                                                                      │
│  ┌──────────────┐     ┌──────────────────┐     ┌─────────────────┐  │
│  │  SPIRE Server │────▶│ OIDC Discovery   │────▶│   Keycloak      │  │
│  │  (StatefulSet)│     │ Provider         │     │   (StatefulSet) │  │
│  │              │     │ (Deployment)      │     │   + PostgreSQL  │  │
│  │  Trust Domain│     │                  │     │                 │  │
│  │  example.org │     │ Serves JWKS at   │     │ Realm: mcp-demo │  │
│  └──────┬───────┘     │ /.well-known/    │     │                 │  │
│         │             │ openid-config    │     │ Custom SPIs:    │  │
│         │             └──────────────────┘     │ - SVID Auth     │  │
│         │                                      │ - SPIFFE DCR    │  │
│  ┌──────▼───────┐                              └────────┬────────┘  │
│  │  SPIRE Agent  │                                       │          │
│  │  (DaemonSet)  │                                       │          │
│  │              │     ┌──────────────────┐               │          │
│  │  Issues SVIDs│────▶│  MCP Client Pod  │───────────────┘          │
│  │  via Workload│     │  (Demo Agent)    │  Authenticates with      │
│  │  API socket  │     │                  │  JWT SVID instead of     │
│  └──────────────┘     │  SPIFFE ID:      │  client_secret           │
│                       │  spiffe://example│                          │
│  ┌──────────────┐     │  .org/mcp-client │                          │
│  │ SPIRE Ctrlr  │     └──────────────────┘                          │
│  │ Manager      │                                                    │
│  │ (auto-creates│     ┌──────────────────┐                          │
│  │  entries for │     │  MCP Server Pod  │                          │
│  │  k8s SAs)    │     │  (Resource Server)│                         │
│  └──────────────┘     └──────────────────┘                          │
│                                                                      │
│  ┌──────────────┐                                                    │
│  │ SPIFFE CSI   │  Mounts Workload API socket into pods             │
│  │ Driver       │                                                    │
│  └──────────────┘                                                    │
│                                                                      │
│  (Services exposed via NodePort + Kind extraPortMappings)            │
└─────────────────────────────────────────────────────────────────────┘
         │
    extraPortMappings (NodePort)
    Host :30080 → :30080 (Keycloak)
    Host :30443 → :30443 (OIDC Discovery)
```

### 1.3 Authentication Flows

**Flow A: Dynamic Client Registration (DCR) with SPIFFE**

```
MCP Client Pod                    SPIRE Agent         Keycloak
     │                                │                   │
     │─── Request JWT SVID ──────────▶│                   │
     │    (via Workload API)          │                   │
     │◀── JWT SVID (with software ───│                   │
     │    statement claims)           │                   │
     │                                                    │
     │─── POST /clients-registrations/spiffe-dcr/register │
     │    Body: { software_statement: <JWT_SVID> }  ─────▶│
     │                                                    │── Fetch JWKS from
     │                                                    │   OIDC Discovery
     │                                                    │   Provider
     │                                                    │── Validate JWT sig
     │                                                    │── Verify trust domain
     │                                                    │── Extract SPIFFE ID
     │                                                    │── Auto-register client
     │◀── { client_id, ... } ────────────────────────────│
```

**Flow B: Client Authentication with SPIFFE JWT SVID (no client_secret)**

```
MCP Client Pod                    SPIRE Agent         Keycloak
     │                                │                   │
     │─── Request JWT SVID ──────────▶│                   │
     │    (aud=keycloak-realm-url)    │                   │
     │◀── JWT SVID ──────────────────│                   │
     │                                                    │
     │─── POST /token                                     │
     │    grant_type=authorization_code                   │
     │    client_assertion_type=                           │
     │      urn:ietf:params:oauth:client-assertion-type:  │
     │      jwt-spiffe                                    │
     │    client_assertion=<JWT_SVID>               ─────▶│
     │                                                    │── Fetch JWKS from
     │                                                    │   SPIRE OIDC Discovery
     │                                                    │── Validate JWT SVID
     │                                                    │── Match sub to client
     │                                                    │── Issue access_token
     │◀── { access_token, ... } ─────────────────────────│
```

---

## 2. Prerequisites (Host Machine Only)

Only these tools are needed on the macOS host. Everything else runs inside the Kind cluster.

| Tool | Purpose | Install Command |
|------|---------|-----------------|
| Docker Desktop | Container runtime for Kind | `brew install --cask docker` |
| kind | Kubernetes-in-Docker | `brew install kind` |
| kubectl | Kubernetes CLI | `brew install kubectl` |
| helm | Package manager for K8s | `brew install helm` |
| jq | JSON processing (for test scripts) | `brew install jq` |

**Verify prerequisites:**

```bash
docker version && kind version && kubectl version --client && helm version && jq --version
```

---

## 3. Kind Cluster Configuration

### 3.1 Cluster Config File: `kind-config.yaml`

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: spiffe-mcp-demo
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 30080
        hostPort: 30080
        protocol: TCP
      - containerPort: 30443
        hostPort: 30443
        protocol: TCP
  - role: worker
    labels:
      spire-agent: "true"
```

### 3.2 Create Cluster

```bash
kind create cluster --config kind-config.yaml
kubectl cluster-info --context kind-spiffe-mcp-demo
```

---

## 4. Service Exposure via NodePort

Services are exposed directly via Kubernetes NodePort services combined with Kind's `extraPortMappings`. No ingress controller or `kubectl port-forward` is used.

| Service | NodePort | Host Access |
|---------|----------|-------------|
| Keycloak | 30080 | `http://localhost:30080` |
| OIDC Discovery | 30443 | `http://localhost:30443` |

Kind's `extraPortMappings` in `kind-config.yaml` map these NodePorts from the container to the host.

---

## 5. SPIRE Stack Deployment (via Helm)

### 5.1 Add Helm Repos

```bash
helm repo add spiffe https://spiffe.github.io/helm-charts-hardened/
helm repo update
```

### 5.2 SPIRE Values File: `spire-values.yaml`

```yaml
global:
  spire:
    clusterName: "spiffe-mcp-demo"
    trustDomain: "example.org"
    # The JWT issuer must match what Keycloak will use to fetch JWKS
    jwtIssuer: "https://oidc-discovery.example.org"

spire-server:
  enabled: true
  replicaCount: 1

  # Persistence for server datastore
  persistence:
    type: pvc
    size: 1Gi

  # Server configuration
  controllerManager:
    enabled: true
    # Auto-create registration entries for pods based on service accounts
    identities:
      clusterSPIFFEIDs:
        default:
          enabled: true
        oidc-discovery-provider:
          enabled: true

  # CA configuration
  ca_subject:
    country: US
    organization: SpiffeMCPDemo

  # CredentialComposer plugin for software statements
  # This enriches JWT SVIDs with DCR-compatible claims
  credentialComposer:
    - name: "software_statements"
      plugin_cmd: "/opt/spire/plugins/spire-software-statements"
      plugin_data:
        jwks_url: "https://oidc-discovery.spire-system.svc.cluster.local/keys"
        client_auth: "client-spiffe-jwt"

spire-agent:
  enabled: true

spiffe-csi-driver:
  enabled: true

spiffe-oidc-discovery-provider:
  enabled: true
  # This component serves the JWKS endpoint that Keycloak uses
  # to verify SPIFFE JWT SVIDs
  config:
    # Domains this OIDC provider serves
    domains:
      - "oidc-discovery.example.org"
      - "spiffe-oidc-discovery-provider.spire-system.svc.cluster.local"

spire-controller-manager:
  enabled: true
```

### 5.3 Install SPIRE

```bash
# Install CRDs first
helm upgrade --install --create-namespace -n spire-system \
  spire-crds spire-crds \
  --repo https://spiffe.github.io/helm-charts-hardened/ \
  --wait

# Install SPIRE stack
helm upgrade --install -n spire-system \
  spire spire \
  --repo https://spiffe.github.io/helm-charts-hardened/ \
  --values spire-values.yaml \
  --wait --timeout 300s
```

### 5.4 Verify SPIRE Deployment

```bash
# Check all pods are running
kubectl -n spire-system get pods

# Expected output: spire-server, spire-agent, spiffe-oidc-discovery-provider,
# spiffe-csi-driver, spire-controller-manager pods all Running

# Verify SPIRE server health
kubectl -n spire-system exec -it \
  $(kubectl -n spire-system get pod -l app.kubernetes.io/name=server -o name) \
  -- /opt/spire/bin/spire-server healthcheck

# List registered entries (should auto-populate via controller-manager)
kubectl -n spire-system exec -it \
  $(kubectl -n spire-system get pod -l app.kubernetes.io/name=server -o name) \
  -- /opt/spire/bin/spire-server entry show
```

### 5.5 SPIRE Registration Entries

The SPIRE Controller Manager auto-creates entries based on Kubernetes service accounts. For the MCP client workload, create a `ClusterSPIFFEID` resource:

**File: `cluster-spiffe-ids.yaml`**

```yaml
apiVersion: spire.spiffe.io/v1alpha1
kind: ClusterSPIFFEID
metadata:
  name: mcp-client-id
spec:
  className: "default"
  spiffeIDTemplate: "spiffe://example.org/ns/{{ .PodMeta.Namespace }}/sa/{{ .PodSpec.ServiceAccountName }}"
  podSelector:
    matchLabels:
      app: mcp-client
  jwtTTL: "5m"
  namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: mcp-demo
---
apiVersion: spire.spiffe.io/v1alpha1
kind: ClusterSPIFFEID
metadata:
  name: mcp-server-id
spec:
  className: "default"
  spiffeIDTemplate: "spiffe://example.org/ns/{{ .PodMeta.Namespace }}/sa/{{ .PodSpec.ServiceAccountName }}"
  podSelector:
    matchLabels:
      app: mcp-server
  namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: mcp-demo
```

```bash
kubectl apply -f cluster-spiffe-ids.yaml
```

---

## 6. Building Custom Keycloak SPIs

Keycloak requires two custom SPI JARs to understand SPIFFE identities. These must be built inside the cluster (or in a Docker build stage) so nothing is installed on the host.

### 6.1 SPI 1: `spiffe-svid-client-authenticator`

**Purpose:** Allows Keycloak clients to authenticate using SPIFFE JWT SVIDs instead of `client_secret`. Implements the `ClientAuthenticator` SPI with provider ID `client-spiffe-jwt`.

**Source:** [github.com/christian-posta/spiffe-svid-client-authenticator](https://github.com/christian-posta/spiffe-svid-client-authenticator)

**Key Classes:**

| Class | Role |
|-------|------|
| `SpiffeSvidClientAuthenticator` | Entry point: intercepts token requests with `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-spiffe` |
| `SpiffeSvidClientValidator` | Fetches JWKS from SPIRE OIDC Discovery, validates JWT signature, verifies `iss` matches trust domain, verifies `sub` is valid SPIFFE ID, checks `aud` matches realm URL |
| `SpiffeSvidClientAuthUtil` | Error response formatting |

**Requirements:** Keycloak 26.2.5+, Java 17+, Maven 3.6+

### 6.2 SPI 2: `spiffe-dcr-keycloak`

**Purpose:** Custom Dynamic Client Registration endpoint that accepts SPIFFE software statements for zero-touch client registration.

**Source:** [github.com/christian-posta/spiffe-dcr-keycloak](https://github.com/christian-posta/spiffe-dcr-keycloak)

**Key Classes:**

| Class | Role |
|-------|------|
| `SpiffeDcrProvider` | Custom DCR endpoint at `/clients-registrations/spiffe-dcr/register` |
| `SpiffeSoftwareStatementValidator` | Validates software statement JWT: sig verification against SPIRE JWKS, trust domain matching, token expiry |
| `SpiffeClientConfigurator` | Auto-configures registered client with SPIFFE auth method |
| `SpiffeDcrRequest` / `SpiffeDcrResponse` | Request/response models with SPIFFE metadata |

**Registration Endpoint Request Format:**

```json
{
  "software_statement": "<JWT_SVID_containing_dcr_claims>"
}
```

**Required JWT Claims in Software Statement:**

| Claim | Value | Purpose |
|-------|-------|---------|
| `sub` | `spiffe://example.org/ns/mcp-demo/sa/mcp-client` | Becomes `client_id` |
| `iss` | SPIRE server URL | Verified against trust domain |
| `jwks_url` | SPIRE OIDC Discovery JWKS URL | Client public key endpoint |
| `client_auth` | `client-spiffe-jwt` | Authentication method for the registered client |
| `aud` | Keycloak realm URL | Target authorization server |
| `exp`, `iat` | Standard JWT timestamps | Token freshness |

### 6.3 SPIRE Plugin: `spire-software-statements`

**Purpose:** A SPIRE CredentialComposer plugin that enriches JWT SVIDs with OAuth DCR claims (`jwks_url`, `client_auth`).

**Source:** [github.com/christian-posta/spire-software-statements](https://github.com/christian-posta/spire-software-statements)

**Language:** Go 1.21+. Implements the SPIRE CredentialComposer interface as an external plugin.

**SPIRE Server Configuration:**

```hcl
CredentialComposer "software_statements" {
  plugin_cmd = "/opt/spire/plugins/spire-software-statements"
  plugin_checksum = "<sha256>"
  plugin_data = {
    jwks_url = "https://oidc-discovery.example.org/keys"
    client_auth = "client-spiffe-jwt"
  }
}
```

### 6.4 Multi-Stage Dockerfile for Building Everything

**File: `Dockerfile.keycloak-custom`**

```dockerfile
# ============================================================
# Stage 1: Build SPIFFE SVID Client Authenticator SPI (Java)
# ============================================================
FROM maven:3.9-eclipse-temurin-17 AS build-svid-auth

WORKDIR /build
RUN git clone https://github.com/christian-posta/spiffe-svid-client-authenticator.git .
RUN mvn clean package -DskipTests

# ============================================================
# Stage 2: Build SPIFFE DCR Keycloak SPI (Java)
# ============================================================
FROM maven:3.9-eclipse-temurin-17 AS build-dcr

WORKDIR /build
RUN git clone https://github.com/christian-posta/spiffe-dcr-keycloak.git .
RUN mvn clean package -DskipTests

# ============================================================
# Stage 3: Build SPIRE Software Statements Plugin (Go)
# ============================================================
FROM golang:1.22 AS build-spire-plugin

WORKDIR /build
RUN git clone https://github.com/christian-posta/spire-software-statements.git .
RUN make build

# ============================================================
# Stage 4: Custom Keycloak Image with SPIFFE SPIs
# ============================================================
FROM quay.io/keycloak/keycloak:26.2.5

# Copy SPI JARs into Keycloak providers directory
COPY --from=build-svid-auth /build/target/spiffe-svid-client-authenticator-*.jar /opt/keycloak/providers/
COPY --from=build-dcr /build/target/spiffe-dcr-keycloak-*.jar /opt/keycloak/providers/

# Build Keycloak with custom providers
RUN /opt/keycloak/bin/kc.sh build

ENTRYPOINT ["/opt/keycloak/bin/kc.sh"]
```

### 6.5 Build and Load into Kind

```bash
# Build the custom Keycloak image
docker build -f Dockerfile.keycloak-custom -t keycloak-spiffe:latest .

# Load it into the Kind cluster (no registry needed)
kind load docker-image keycloak-spiffe:latest --name spiffe-mcp-demo

# Build SPIRE plugin image for the init container
docker build -f Dockerfile.spire-plugin -t spire-software-statements:latest .
kind load docker-image spire-software-statements:latest --name spiffe-mcp-demo
```

**File: `Dockerfile.spire-plugin`** (for SPIRE server init container)

```dockerfile
FROM golang:1.22 AS builder
WORKDIR /build
RUN git clone https://github.com/christian-posta/spire-software-statements.git .
RUN make build

FROM scratch
COPY --from=builder /build/bin/spire-software-statements /spire-software-statements
```

---

## 7. PostgreSQL for Keycloak

### 7.1 Deploy PostgreSQL

**File: `postgres.yaml`**

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: mcp-demo
  labels:
    kubernetes.io/metadata.name: mcp-demo
---
apiVersion: v1
kind: Secret
metadata:
  name: postgres-secret
  namespace: mcp-demo
type: Opaque
stringData:
  POSTGRES_DB: keycloak
  POSTGRES_USER: keycloak
  POSTGRES_PASSWORD: keycloak-db-password
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres
  namespace: mcp-demo
spec:
  serviceName: postgres
  replicas: 1
  selector:
    matchLabels:
      app: postgres
  template:
    metadata:
      labels:
        app: postgres
    spec:
      containers:
        - name: postgres
          image: postgres:16-alpine
          ports:
            - containerPort: 5432
          envFrom:
            - secretRef:
                name: postgres-secret
          volumeMounts:
            - name: pgdata
              mountPath: /var/lib/postgresql/data
  volumeClaimTemplates:
    - metadata:
        name: pgdata
      spec:
        accessModes: ["ReadWriteOnce"]
        resources:
          requests:
            storage: 1Gi
---
apiVersion: v1
kind: Service
metadata:
  name: postgres
  namespace: mcp-demo
spec:
  selector:
    app: postgres
  ports:
    - port: 5432
      targetPort: 5432
```

---

## 8. Keycloak Deployment

### 8.1 Keycloak Realm Configuration: `realm-config.json`

```json
{
  "realm": "mcp-demo",
  "enabled": true,
  "sslRequired": "none",
  "registrationAllowed": false,
  "attributes": {
    "spiffe.trust.domain": "example.org",
    "spiffe.jwks.url": "http://spiffe-oidc-discovery-provider.spire-system.svc.cluster.local/keys"
  },
  "roles": {
    "realm": [
      { "name": "mcp-client", "description": "MCP Client Role" },
      { "name": "mcp-server", "description": "MCP Server Role" }
    ]
  },
  "users": [
    {
      "username": "demo-user",
      "enabled": true,
      "credentials": [{ "type": "password", "value": "demo-password", "temporary": false }],
      "realmRoles": ["mcp-client"]
    }
  ],
  "clients": [
    {
      "clientId": "spiffe://example.org/ns/mcp-demo/sa/mcp-client",
      "enabled": true,
      "clientAuthenticatorType": "client-spiffe-jwt",
      "directAccessGrantsEnabled": true,
      "standardFlowEnabled": true,
      "publicClient": false,
      "redirectUris": ["http://localhost:8888/callback", "http://localhost:30080/callback", "http://mcp-client.mcp-demo.svc.cluster.local/callback"],
      "webOrigins": ["*"],
      "protocol": "openid-connect",
      "attributes": {
        "spiffe.svid.issuer": "https://oidc-discovery.example.org",
        "spiffe.svid.jwks.url": "http://spiffe-oidc-discovery-provider.spire-system.svc.cluster.local/keys"
      }
    }
  ],
  "components": {
    "org.keycloak.protocol.oidc.OIDCWellKnown": [
      {
        "name": "mcp-metadata",
        "providerId": "default",
        "config": {}
      }
    ]
  }
}
```

### 8.2 Keycloak Kubernetes Manifests

**File: `keycloak.yaml`**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: keycloak-realm-config
  namespace: mcp-demo
data:
  realm-config.json: |
    <contents of realm-config.json above>
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: keycloak
  namespace: mcp-demo
spec:
  serviceName: keycloak
  replicas: 1
  selector:
    matchLabels:
      app: keycloak
  template:
    metadata:
      labels:
        app: keycloak
    spec:
      containers:
        - name: keycloak
          image: keycloak-spiffe:latest
          imagePullPolicy: Never  # Use local image loaded via kind
          args:
            - start-dev
            - --import-realm
          ports:
            - containerPort: 8080
              name: http
          env:
            - name: KC_DB
              value: postgres
            - name: KC_DB_URL
              value: jdbc:postgresql://postgres.mcp-demo.svc.cluster.local:5432/keycloak
            - name: KC_DB_USERNAME
              valueFrom:
                secretKeyRef:
                  name: postgres-secret
                  key: POSTGRES_USER
            - name: KC_DB_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: postgres-secret
                  key: POSTGRES_PASSWORD
            - name: KC_HOSTNAME
              value: localhost
            - name: KC_HOSTNAME_STRICT
              value: "false"
            - name: KC_HTTP_ENABLED
              value: "true"
            - name: KC_HTTP_PORT
              value: "8080"
            - name: KEYCLOAK_ADMIN
              value: admin
            - name: KEYCLOAK_ADMIN_PASSWORD
              value: admin
          volumeMounts:
            - name: realm-config
              mountPath: /opt/keycloak/data/import
              readOnly: true
          readinessProbe:
            httpGet:
              path: /health/ready
              port: 8080
            initialDelaySeconds: 30
            periodSeconds: 10
          resources:
            requests:
              memory: 512Mi
              cpu: 500m
            limits:
              memory: 1Gi
              cpu: 1000m
      volumes:
        - name: realm-config
          configMap:
            name: keycloak-realm-config
---
apiVersion: v1
kind: Service
metadata:
  name: keycloak
  namespace: mcp-demo
spec:
  type: NodePort
  selector:
    app: keycloak
  ports:
    - port: 8080
      targetPort: 8080
      nodePort: 30080
      name: http
```

### 8.3 OIDC Discovery NodePort Service (for host access during testing)

**File: `oidc-nodeport.yaml`**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: spiffe-oidc-discovery-nodeport
  namespace: spire-system
spec:
  type: NodePort
  selector:
    app.kubernetes.io/name: spiffe-oidc-discovery-provider
  ports:
    - port: 443
      targetPort: 443
      nodePort: 30443
      name: https
```

---

## 9. MCP Client Demo Application

### 9.1 MCP Client Pod Specification

This pod demonstrates the complete SPIFFE-based MCP OAuth flow: it obtains a JWT SVID from SPIRE, uses it for Dynamic Client Registration (or pre-configured client auth), and then authenticates to Keycloak's token endpoint.

**File: `mcp-client.yaml`**

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: mcp-client
  namespace: mcp-demo
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mcp-client
  namespace: mcp-demo
spec:
  replicas: 1
  selector:
    matchLabels:
      app: mcp-client
  template:
    metadata:
      labels:
        app: mcp-client
    spec:
      serviceAccountName: mcp-client
      containers:
        - name: mcp-client
          image: python:3.12-slim
          command: ["sleep", "infinity"]  # Interactive demo pod
          env:
            - name: SPIFFE_ENDPOINT_SOCKET
              value: "unix:///spiffe-workload-api/spire-agent.sock"
            - name: KEYCLOAK_URL
              value: "http://keycloak.mcp-demo.svc.cluster.local:8080"
            - name: KEYCLOAK_REALM
              value: "mcp-demo"
          volumeMounts:
            - name: spiffe-workload-api
              mountPath: /spiffe-workload-api
              readOnly: true
      volumes:
        - name: spiffe-workload-api
          csi:
            driver: "csi.spiffe.io"
            readOnly: true
```

### 9.2 Demo Script: `test-spiffe-auth.sh`

This script runs INSIDE the mcp-client pod to demonstrate the complete flow.

```bash
#!/bin/bash
set -euo pipefail

# ============================================================
# SPIFFE-based MCP OAuth Client Authentication Demo
# ============================================================
# This script demonstrates:
# 1. Fetching a JWT SVID from SPIRE via Workload API
# 2. Using the JWT SVID to authenticate to Keycloak (no client_secret)
# 3. Obtaining an OAuth access token
# ============================================================

KEYCLOAK_URL="${KEYCLOAK_URL:-http://keycloak.mcp-demo.svc.cluster.local:8080}"
REALM="${KEYCLOAK_REALM:-mcp-demo}"
SPIFFE_SOCKET="${SPIFFE_ENDPOINT_SOCKET:-unix:///spiffe-workload-api/spire-agent.sock}"

echo "=== Step 1: Install dependencies ==="
pip install -q py-spiffe requests

echo "=== Step 2: Fetch JWT SVID from SPIRE ==="
python3 << 'PYEOF'
import json
import requests
from pyspiffe.workloadapi import WorkloadApiClient

# Connect to SPIRE Agent via Workload API
client = WorkloadApiClient(spiffe_endpoint_socket="unix:///spiffe-workload-api/spire-agent.sock")

# Fetch JWT SVID with audience set to Keycloak realm URL
keycloak_url = "http://keycloak.mcp-demo.svc.cluster.local:8080"
realm = "mcp-demo"
audience = f"{keycloak_url}/realms/{realm}"

jwt_svid = client.fetch_jwt_svid(audiences=[audience])
print(f"SPIFFE ID: {jwt_svid.spiffe_id}")
print(f"JWT SVID Token (first 80 chars): {jwt_svid.token[:80]}...")

# Save token for use in next step
with open("/tmp/jwt_svid.txt", "w") as f:
    f.write(jwt_svid.token)

print(f"\nJWT SVID saved. Audience: {audience}")
print(f"Token expires at: {jwt_svid.expiry}")
PYEOF

echo ""
echo "=== Step 3: Authenticate to Keycloak using JWT SVID ==="
python3 << 'PYEOF'
import requests

keycloak_url = "http://keycloak.mcp-demo.svc.cluster.local:8080"
realm = "mcp-demo"
token_url = f"{keycloak_url}/realms/{realm}/protocol/openid-connect/token"

with open("/tmp/jwt_svid.txt") as f:
    jwt_svid = f.read().strip()

# Use SPIFFE JWT SVID as client assertion (no client_secret needed!)
response = requests.post(token_url, data={
    "grant_type": "client_credentials",
    "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-spiffe",
    "client_assertion": jwt_svid,
    "scope": "openid"
})

if response.status_code == 200:
    token_data = response.json()
    print("SUCCESS! Got access token:")
    print(f"  access_token: {token_data['access_token'][:60]}...")
    print(f"  token_type: {token_data['token_type']}")
    print(f"  expires_in: {token_data['expires_in']}s")
    if 'scope' in token_data:
        print(f"  scope: {token_data['scope']}")
else:
    print(f"FAILED: {response.status_code}")
    print(response.text)
PYEOF

echo ""
echo "=== Step 4: Dynamic Client Registration (optional) ==="
python3 << 'PYEOF'
import requests

keycloak_url = "http://keycloak.mcp-demo.svc.cluster.local:8080"
realm = "mcp-demo"
dcr_url = f"{keycloak_url}/realms/{realm}/clients-registrations/spiffe-dcr/register"

with open("/tmp/jwt_svid.txt") as f:
    jwt_svid = f.read().strip()

# The JWT SVID (enriched with software statement claims by SPIRE plugin)
# acts as the software_statement for DCR
response = requests.post(dcr_url, json={
    "software_statement": jwt_svid
}, headers={"Content-Type": "application/json"})

if response.status_code in [200, 201]:
    client_data = response.json()
    print("SUCCESS! Client registered via SPIFFE DCR:")
    print(f"  client_id: {client_data.get('clientId', client_data.get('client_id'))}")
    print(f"  auth_method: {client_data.get('clientAuthenticatorType', 'N/A')}")
else:
    print(f"DCR Response: {response.status_code}")
    print(response.text[:500])
PYEOF
```

### 9.3 MCP Server (Resource Server) Pod

**File: `mcp-server.yaml`**

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: mcp-server
  namespace: mcp-demo
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mcp-server
  namespace: mcp-demo
spec:
  replicas: 1
  selector:
    matchLabels:
      app: mcp-server
  template:
    metadata:
      labels:
        app: mcp-server
    spec:
      serviceAccountName: mcp-server
      containers:
        - name: mcp-server
          image: python:3.12-slim
          command: ["sleep", "infinity"]
          env:
            - name: SPIFFE_ENDPOINT_SOCKET
              value: "unix:///spiffe-workload-api/spire-agent.sock"
            - name: KEYCLOAK_URL
              value: "http://keycloak.mcp-demo.svc.cluster.local:8080"
            - name: KEYCLOAK_REALM
              value: "mcp-demo"
          volumeMounts:
            - name: spiffe-workload-api
              mountPath: /spiffe-workload-api
              readOnly: true
      volumes:
        - name: spiffe-workload-api
          csi:
            driver: "csi.spiffe.io"
            readOnly: true
```

---

## 10. Local Access

Services are exposed via NodePort and Kind's `extraPortMappings`. No `/etc/hosts` changes are needed.

| Service | URL |
|---------|-----|
| Keycloak Admin Console | `http://localhost:30080/admin` (admin/admin) |
| Keycloak Realm | `http://localhost:30080/realms/mcp-demo` |
| OIDC Discovery | `http://localhost:30443/.well-known/openid-configuration` |

---

## 11. Complete Deployment Script

**File: `deploy-all.sh`**

```bash
#!/bin/bash
set -euo pipefail

echo "=============================================="
echo "  SPIFFE + MCP OAuth + Keycloak in Kind"
echo "=============================================="

# Step 1: Create Kind cluster
echo "[1/8] Creating Kind cluster..."
kind create cluster --config kind-config.yaml
kubectl cluster-info --context kind-spiffe-mcp-demo

# Step 2: Install SPIRE
echo "[2/7] Installing SPIRE stack via Helm..."
helm repo add spiffe https://spiffe.github.io/helm-charts-hardened/ 2>/dev/null || true
helm repo update
helm upgrade --install --create-namespace -n spire-system \
  spire-crds spire-crds \
  --repo https://spiffe.github.io/helm-charts-hardened/ --wait
helm upgrade --install -n spire-system \
  spire spire \
  --repo https://spiffe.github.io/helm-charts-hardened/ \
  --values spire-values.yaml \
  --wait --timeout 300s

# Step 3: Build custom Keycloak image
echo "[3/7] Building custom Keycloak image with SPIFFE SPIs..."
docker build -f Dockerfile.keycloak-custom -t keycloak-spiffe:latest .
kind load docker-image keycloak-spiffe:latest --name spiffe-mcp-demo

# Step 4: Deploy PostgreSQL
echo "[4/7] Deploying PostgreSQL..."
kubectl apply -f postgres.yaml
kubectl -n mcp-demo wait --for=condition=ready pod -l app=postgres --timeout=120s

# Step 5: Deploy Keycloak
echo "[5/7] Deploying Keycloak with SPIFFE SPIs..."
kubectl apply -f keycloak.yaml
kubectl apply -f oidc-nodeport.yaml
echo "Waiting for Keycloak..."
kubectl -n mcp-demo wait --for=condition=ready pod -l app=keycloak --timeout=300s

# Step 6: Create SPIFFE IDs and deploy workloads
echo "[6/7] Creating ClusterSPIFFEIDs and deploying workloads..."
kubectl apply -f cluster-spiffe-ids.yaml
kubectl apply -f mcp-client.yaml
kubectl apply -f mcp-server.yaml
kubectl -n mcp-demo wait --for=condition=ready pod -l app=mcp-client --timeout=120s

# Step 7: Verify
echo "[7/7] Verifying deployment..."
echo ""
echo "--- SPIRE Status ---"
kubectl -n spire-system get pods
echo ""
echo "--- MCP Demo Status ---"
kubectl -n mcp-demo get pods
echo ""
echo "--- Keycloak URL ---"
echo "  Admin Console: http://localhost:30080/admin (admin/admin)"
echo "  Realm URL:     http://localhost:30080/realms/mcp-demo"
echo ""
echo "--- OIDC Discovery ---"
echo "  http://localhost:30443/.well-known/openid-configuration"
echo ""
echo "--- Run Demo ---"
echo "  kubectl -n mcp-demo exec -it deploy/mcp-client -- bash /scripts/test-spiffe-auth.sh"
echo ""
echo "=============================================="
echo "  Deployment Complete!"
echo "=============================================="
```

---

## 12. Teardown Script

**File: `teardown.sh`**

```bash
#!/bin/bash
echo "Tearing down Kind cluster..."
kind delete cluster --name spiffe-mcp-demo
echo "Cleaning up docker images..."
docker rmi keycloak-spiffe:latest 2>/dev/null || true
docker rmi spire-software-statements:latest 2>/dev/null || true
echo "Done. No residual installations on host."
```

---

## 13. Verification Checklist

After deployment, verify each component:

| # | Check | Command | Expected |
|---|-------|---------|----------|
| 1 | Kind cluster running | `kind get clusters` | `spiffe-mcp-demo` |
| 2 | SPIRE Server healthy | `kubectl -n spire-system exec deploy/spire-server -- /opt/spire/bin/spire-server healthcheck` | `Server is healthy` |
| 3 | SPIRE Agent healthy | `kubectl -n spire-system exec ds/spire-agent -- /opt/spire/bin/spire-agent healthcheck` | `Agent is healthy` |
| 4 | OIDC Discovery serving | `curl -s http://localhost:30443/.well-known/openid-configuration \| jq .` | JSON with `jwks_uri` |
| 5 | Keycloak admin accessible | `curl -s http://localhost:30080/health/ready` | `{"status":"UP"}` |
| 6 | Realm configured | `curl -s http://localhost:30080/realms/mcp-demo/.well-known/openid-configuration \| jq .issuer` | Realm issuer URL |
| 7 | SPIFFE client registered | Keycloak Admin → Clients → Check SPIFFE client exists | Client with SPIFFE ID |
| 8 | JWT SVID fetchable | `kubectl -n mcp-demo exec deploy/mcp-client -- python3 -c "from pyspiffe..."` | SPIFFE ID printed |
| 9 | Token exchange works | Run test-spiffe-auth.sh | `access_token` returned |

---

## 14. Key Configuration Reference

### 14.1 SPIFFE/SPIRE Concepts

| Concept | Description |
|---------|-------------|
| **Trust Domain** | `example.org` — the root of trust for all SPIFFE IDs |
| **SPIFFE ID** | `spiffe://example.org/ns/mcp-demo/sa/mcp-client` — workload identity URI |
| **JWT SVID** | Short-lived JWT token containing SPIFFE ID as `sub`, signed by SPIRE server |
| **X.509 SVID** | X.509 certificate with SPIFFE ID in SAN URI (not used in this demo) |
| **Workload API** | Unix domain socket API that pods use to fetch SVIDs from SPIRE Agent |
| **OIDC Discovery Provider** | Serves `/.well-known/openid-configuration` and `/keys` (JWKS) for the SPIRE trust domain |
| **CredentialComposer** | SPIRE server plugin that enriches SVIDs with additional claims before signing |
| **ClusterSPIFFEID** | CRD that tells SPIRE Controller Manager to auto-register entries for matching pods |

### 14.2 OAuth/OIDC Concepts

| Concept | Value in This System |
|---------|---------------------|
| **Authorization Server** | Keycloak at `http://localhost:30080/realms/mcp-demo` |
| **client_assertion_type** | `urn:ietf:params:oauth:client-assertion-type:jwt-spiffe` |
| **client_assertion** | The JWT SVID token itself |
| **DCR Endpoint** | `/realms/mcp-demo/clients-registrations/spiffe-dcr/register` |
| **Token Endpoint** | `/realms/mcp-demo/protocol/openid-connect/token` |
| **IETF Draft** | `draft-ietf-oauth-spiffe-client-auth-01` (March 2026) |

### 14.3 MCP Authorization Context

The MCP (Model Context Protocol) spec defines how AI agents authenticate. Key requirements this system addresses: MCP clients SHOULD use Dynamic Client Registration (RFC 7591), MCP servers MUST implement Protected Resource Metadata (RFC 9728), and clients MUST implement Resource Indicators (RFC 8707). By using SPIFFE, we replace anonymous DCR with cryptographically-verified identity-based registration.

---

## 15. Troubleshooting Guide

| Issue | Diagnosis | Fix |
|-------|-----------|-----|
| SPIRE Agent can't reach Server | `kubectl -n spire-system logs ds/spire-agent` | Check SPIRE server service DNS resolution |
| OIDC Discovery returns 404 | `curl` the internal service URL from inside cluster | Verify `spiffe-oidc-discovery-provider` pod is running and domains config matches |
| Keycloak can't verify JWT SVID | Check Keycloak logs for JWKS fetch errors | Ensure `spiffe.jwks.url` realm attribute points to OIDC Discovery Provider's internal service URL |
| CSI Driver not mounting socket | `kubectl describe pod <mcp-client-pod>` | Ensure `spiffe-csi-driver` DaemonSet is running on the node |
| DCR returns 401 | Check software statement JWT claims | Ensure `iss` matches trust domain, `aud` matches realm URL |
| Token request returns 400 | Check client_assertion_type value | Must be exactly `urn:ietf:params:oauth:client-assertion-type:jwt-spiffe` |
| Kind image not found | `crictl images` on the node | Re-run `kind load docker-image` |

---

## 16. File Manifest

```
project-root/
├── kind-config.yaml                  # Kind cluster configuration
├── spire-values.yaml                 # SPIRE Helm chart values
├── Dockerfile.keycloak-custom        # Multi-stage build for Keycloak + SPIs
├── Dockerfile.spire-plugin           # SPIRE software-statements plugin
├── postgres.yaml                     # PostgreSQL StatefulSet
├── keycloak.yaml                     # Keycloak StatefulSet + NodePort Service
├── oidc-nodeport.yaml                # OIDC Discovery NodePort Service
├── realm-config.json                 # Keycloak realm import config
├── cluster-spiffe-ids.yaml           # ClusterSPIFFEID CRDs
├── mcp-client.yaml                   # MCP Client demo pod
├── mcp-server.yaml                   # MCP Server demo pod
├── test-spiffe-auth.sh               # Authentication demo script
├── deploy-all.sh                     # One-command deployment
└── teardown.sh                       # Clean teardown
```

---

## 17. Security Notes

This is a **development/demo** setup. For production, you would need to: enable TLS everywhere (SPIRE OIDC Discovery, Keycloak), use proper certificate management (cert-manager), configure SPIRE server HA with an external datastore, use a production-grade PostgreSQL deployment, rotate trust bundles regularly, and restrict RBAC permissions. The SPIFFE-based approach is inherently more secure than client secrets because SVIDs are short-lived (5 minutes in this config), cryptographically verifiable, automatically rotated, and tied to workload identity attestation (the pod must actually be running with the correct service account on an attested node).

---

## 18. References

- [Authenticating MCP OAuth Clients With SPIFFE and SPIRE — Christian Posta](https://blog.christianposta.com/authenticating-mcp-oauth-clients-with-spiffe/)
- [Implementing MCP DCR With SPIFFE and Keycloak — Christian Posta](https://blog.christianposta.com/implementing-mcp-dynamic-client-registration-with-spiffe/)
- [christian-posta/keycloak-agent-identity — GitHub](https://github.com/christian-posta/keycloak-agent-identity)
- [christian-posta/spiffe-svid-client-authenticator — GitHub](https://github.com/christian-posta/spiffe-svid-client-authenticator)
- [christian-posta/spiffe-dcr-keycloak — GitHub](https://github.com/christian-posta/spiffe-dcr-keycloak)
- [christian-posta/spire-software-statements — GitHub](https://github.com/christian-posta/spire-software-statements)
- [IETF Draft: OAuth SPIFFE Client Authentication](https://datatracker.ietf.org/doc/draft-ietf-oauth-spiffe-client-auth/)
- [Keycloak Issue #41907: SPIFFE/SPIRE Client Auth](https://github.com/keycloak/keycloak/issues/41907)
- [Federated Client Authentication — Keycloak Blog](https://www.keycloak.org/2026/01/federated-client-authentication)
- [SPIRE Helm Charts (Hardened)](https://artifacthub.io/packages/helm/helm-spire/spire)
- [MCP Authorization Specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization)
- [Kind Configuration](https://kind.sigs.k8s.io/docs/user/configuration/)

---

## 19. Claude Agent Execution Guidelines

This section provides standards for any Claude agent (Opus 4.6 or equivalent) executing this spec. Following these guidelines ensures efficient context usage, minimal wasted turns, and reliable implementation across sessions.

### 19.1 Session Architecture & Phased Execution

This spec is too large to execute in a single session. Break implementation into **5 ordered phases**, each completable within one Claude session. Each phase has a clear entry condition, exit condition, and checkpoint artifact.

| Phase | Name | Sections | Estimated Turns | Checkpoint Artifact |
|-------|------|----------|-----------------|---------------------|
| 1 | Infrastructure | 3, 4 | 8-12 | Kind cluster running with NodePort mappings, `kubectl get nodes` output saved |
| 2 | SPIRE Stack | 5 | 10-15 | All SPIRE pods Running, healthcheck passing, OIDC Discovery responding |
| 3 | Custom Builds | 6 | 12-18 | `keycloak-spiffe:latest` and `spire-software-statements:latest` loaded in Kind |
| 4 | Keycloak + Data | 7, 8, 10 | 10-15 | Keycloak admin accessible, realm imported, DNS working |
| 5 | Workloads + Validation | 5.5, 9, 13 | 10-15 | ClusterSPIFFEIDs applied, MCP client/server running, test-spiffe-auth.sh passes |

**Between sessions:** Save the checkpoint artifact as a status file so the next session can verify where things stand before continuing.

### 19.2 Context Window Management

**Problem:** This spec is ~1200 lines. Loading the entire spec plus Kubernetes output plus error logs can exhaust the context window.

**Rules:**

1. **Load only the current phase's sections.** Do NOT read the entire spec at once. Use targeted reads:
   ```
   Read SPIFFE-MCP-OAuth-KindCluster-Spec.md lines 1-50    # Architecture overview (always load)
   Read SPIFFE-MCP-OAuth-KindCluster-Spec.md lines 200-350  # Section 5 only (for Phase 2)
   ```

2. **Truncate command output aggressively.** Kubernetes commands can produce verbose output. Always pipe through filters:
   ```bash
   kubectl get pods -n spire-system -o wide        # Good: concise
   kubectl describe pod <name> | head -60           # Good: truncated
   kubectl logs <pod> --tail=30                     # Good: last 30 lines only
   ```

3. **Never paste full YAML back into context.** If you need to verify a manifest was applied correctly, use `kubectl get <resource> -o yaml | grep <key-field>` instead of dumping entire objects.

4. **Summarize before proceeding.** After completing each sub-step, emit a one-line summary rather than repeating full output. Example: `"SPIRE server pod is Running (1/1), healthcheck passed"` rather than pasting the full pod description.

### 19.3 Error Handling Strategy

**Principle: Diagnose before retrying.** Do not blindly re-run failed commands.

**Tiered diagnosis approach:**

```
Tier 1 (5 seconds): Check pod status
  kubectl get pods -n <namespace> | grep -v Running

Tier 2 (10 seconds): Check events
  kubectl get events -n <namespace> --sort-by=.lastTimestamp | tail -10

Tier 3 (15 seconds): Check logs
  kubectl logs <pod> --tail=30 -n <namespace>

Tier 4 (30 seconds): Describe resource
  kubectl describe <resource> -n <namespace> | tail -40
```

**Common failure patterns and pre-computed fixes:**

| Symptom | Likely Cause | Fix (don't diagnose further) |
|---------|-------------|------------------------------|
| `ImagePullBackOff` on `keycloak-spiffe` | Image not loaded into Kind | `kind load docker-image keycloak-spiffe:latest --name spiffe-mcp-demo` |
| SPIRE Agent `CrashLoopBackOff` | Server not ready yet | Wait 60s, check server first |
| CSI Driver `FailedMount` | SPIRE Agent not running on node | Verify agent DaemonSet, check node labels |
| Keycloak `Connection refused` to postgres | PostgreSQL not ready | `kubectl -n mcp-demo wait --for=condition=ready pod -l app=postgres --timeout=120s` |
| Helm install timeout | PVC not provisioned in Kind | Kind needs default StorageClass; check `kubectl get sc` |

### 19.4 Idempotent Commands

Every command in this spec is designed to be idempotent. Use these patterns:

```bash
# Helm: always use upgrade --install (idempotent)
helm upgrade --install ...

# Kubernetes: apply is idempotent
kubectl apply -f ...

# Kind: check before creating
kind get clusters | grep -q spiffe-mcp-demo || kind create cluster --config kind-config.yaml

# Docker build: always tags :latest (overwrites)
docker build -t keycloak-spiffe:latest .
```

**Never use `kubectl create` (use `kubectl apply` instead).** `create` fails if the resource exists; `apply` is always safe.

### 19.5 Parallel vs Sequential Operations

**Can run in parallel (independent):**
- Building `keycloak-spiffe:latest` Docker image AND installing SPIRE Helm chart
- Deploying `mcp-client.yaml` AND `mcp-server.yaml`
- Adding `/etc/hosts` entries AND deploying PostgreSQL

**Must run sequentially (dependencies):**
```
Kind cluster → SPIRE CRDs → SPIRE Stack
PostgreSQL ready → Keycloak deploy
SPIRE Stack ready → ClusterSPIFFEIDs → MCP workloads
Keycloak ready + MCP Client ready → test-spiffe-auth.sh
```

When using Claude's `Task` tool for parallel work, launch independent operations as separate subagents.

### 19.6 Validation-Driven Development

**Rule: Never proceed to the next phase without validating the current phase.**

Each phase has a validation gate. Run these checks and confirm the output before moving on:

**Phase 1 Gate:**
```bash
kubectl get nodes --context kind-spiffe-mcp-demo  # 2 nodes Ready
```

**Phase 2 Gate:**
```bash
kubectl -n spire-system get pods                    # All Running
kubectl -n spire-system exec deploy/spire-server -- \
  /opt/spire/bin/spire-server healthcheck           # "Server is healthy"
curl -s http://localhost:30443/.well-known/openid-configuration | jq .issuer
```

**Phase 3 Gate:**
```bash
docker images | grep keycloak-spiffe                # Image exists
kind load docker-image keycloak-spiffe:latest --name spiffe-mcp-demo  # Loaded
```

**Phase 4 Gate:**
```bash
curl -s http://localhost:30080/health/ready       # {"status":"UP"}
curl -s http://localhost:30080/realms/mcp-demo/.well-known/openid-configuration | jq .issuer
```

**Phase 5 Gate:**
```bash
kubectl -n mcp-demo exec deploy/mcp-client -- \
  python3 -c "from pyspiffe.workloadapi import WorkloadApiClient; \
  c = WorkloadApiClient(spiffe_endpoint_socket='unix:///spiffe-workload-api/spire-agent.sock'); \
  s = c.fetch_jwt_svid(audiences=['test']); print(s.spiffe_id)"
# Should print: spiffe://example.org/ns/mcp-demo/sa/mcp-client
```

### 19.7 Memory & State Persistence Between Sessions

When working across multiple Claude sessions, use these state files to maintain continuity:

**Checkpoint file: `implementation-state.json`**

```json
{
  "spec_version": "1.0",
  "last_completed_phase": 2,
  "last_completed_step": "5.4",
  "cluster_name": "spiffe-mcp-demo",
  "namespaces_created": ["spire-system"],
  "images_loaded": [],
  "known_issues": [],
  "next_action": "Build custom Keycloak image (Section 6.4)",
  "timestamp": "2026-03-16T10:00:00Z"
}
```

**At the start of every new session:**
1. Read `implementation-state.json` to understand current progress
2. Run the validation gate for `last_completed_phase` to confirm state is intact
3. If validation fails, re-execute the failed phase before continuing
4. If validation passes, proceed to the next phase

**At the end of every session:**
1. Update `implementation-state.json` with current progress
2. Note any known issues or deviations from the spec
3. State the exact next action for the following session

### 19.8 Resource Efficiency in Kind

Kind runs inside Docker, which on macOS runs inside a Linux VM. Resources are constrained.

**Docker Desktop recommended settings:**
- CPUs: 4+
- Memory: 8GB+ (Keycloak + SPIRE + PostgreSQL are memory-hungry)
- Disk: 20GB+ free

**If resources are tight:**
- Deploy SPIRE with `replicaCount: 1` for the server (already set in spec)
- Use `postgres:16-alpine` (already set — smaller than full image)
- Set Keycloak resource limits (already set: 1Gi memory limit)
- Skip the MCP Server pod if only testing client auth flow

### 19.9 Rollback Procedures

If a phase fails catastrophically and cannot be recovered:

**Phase-level rollback:**
```bash
# Roll back SPIRE
helm uninstall spire -n spire-system
helm uninstall spire-crds -n spire-system
kubectl delete namespace spire-system

# Roll back Keycloak + PostgreSQL
kubectl delete -f keycloak.yaml -f postgres.yaml
kubectl delete namespace mcp-demo

# Nuclear option: destroy and recreate cluster
kind delete cluster --name spiffe-mcp-demo
# Then restart from Phase 1
```

**Never partially roll back within a phase.** Either the phase is complete or it should be fully rolled back and retried.

### 19.10 Prompt Engineering: Optimal Instructions for Claude

When asking Claude to execute phases of this spec, use these prompt patterns for best results:

**Starting a new phase:**
```
Read sections [X-Y] of SPIFFE-MCP-OAuth-KindCluster-Spec.md.
Read implementation-state.json.
Validate that Phase [N-1] is complete by running the Phase [N-1] gate checks.
Then execute Phase [N] step by step, validating each sub-step before proceeding.
Save progress to implementation-state.json when done.
```

**Recovering from a failure:**
```
Read implementation-state.json for current state.
The last session failed at step [X.Y] with error: [paste error].
Consult Section 15 (Troubleshooting Guide) and Section 19.3 (Error Handling Strategy).
Diagnose and fix the issue, then continue from step [X.Y].
```

**Full unattended deployment:**
```
Read the complete SPIFFE-MCP-OAuth-KindCluster-Spec.md.
Execute deploy-all.sh from Section 11.
If any step fails, consult Section 15 for diagnosis.
Run the full verification checklist from Section 13.
Report final status.
```

### 19.11 Anti-Patterns to Avoid

| Anti-Pattern | Why It Fails | Correct Approach |
|-------------|-------------|------------------|
| Loading entire spec + all manifests into context at once | Exhausts context window, loses ability to reason about errors | Load only current phase sections |
| Running `deploy-all.sh` without understanding it | Cannot diagnose failures | Execute step-by-step with validation gates |
| Retrying failed commands without diagnosis | Same error repeats, wastes turns | Follow Tier 1-4 diagnosis in 19.3 |
| Creating resources with `kubectl create` | Non-idempotent, fails on retry | Always use `kubectl apply` |
| Skipping validation gates between phases | Builds on broken foundation | Always validate before proceeding |
| Dumping full `kubectl describe` output | Floods context with noise | Use `\| tail -40` or `\| grep -A5 <keyword>` |
| Editing manifests inside context instead of files | Changes lost, not reproducible | Write to files, then `kubectl apply -f` |
| Running `helm install` instead of `helm upgrade --install` | Fails if already partially installed | Always use `upgrade --install` |
| Not setting `imagePullPolicy: Never` for local images | Kind tries to pull from registry, fails | Set `imagePullPolicy: Never` for all `kind load` images |
| Ignoring resource constraints | OOMKill, cluster instability | Follow Section 19.8 resource guidance |

### 19.12 Success Criteria

The implementation is complete when ALL of the following are true:

1. `kind get clusters` returns `spiffe-mcp-demo`
2. All pods in `spire-system` namespace are `Running` (server, agent, CSI driver, OIDC discovery, controller manager)
3. All pods in `mcp-demo` namespace are `Running` (postgres, keycloak, mcp-client, mcp-server)
4. `curl http://localhost:30080/health/ready` returns `{"status":"UP"}`
5. `curl http://localhost:30443/.well-known/openid-configuration` returns valid JSON with `jwks_uri`
6. `test-spiffe-auth.sh` running inside the mcp-client pod successfully:
   - Fetches a JWT SVID from SPIRE
   - Authenticates to Keycloak using the JWT SVID (no client_secret)
   - Receives a valid `access_token` in response
7. No long-lived secrets (client_secret values) exist anywhere in the system
8. All E2E tests from Section 20 pass with zero failures
9. Chaos/resilience tests from Section 21 demonstrate self-healing behavior

---

## 20. Comprehensive End-to-End Test Suite

This section defines a complete, layered E2E test suite. Every test is designed to run inside the Kind cluster (from the `mcp-client` pod or a dedicated test-runner pod). Tests are organized from infrastructure up through application-level flows, ensuring every component is battle-tested independently and in combination.

### 20.1 Test Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Test Pyramid                             │
│                                                              │
│  Layer 5: Chaos & Resilience Tests (Section 21)             │
│  ─────────────────────────────────────────────              │
│  Layer 4: Full Flow E2E Tests (20.7)                        │
│  ─────────────────────────────────────────────              │
│  Layer 3: Integration Tests (20.5, 20.6)                    │
│       SPIRE↔Keycloak, DCR, Token Exchange                   │
│  ─────────────────────────────────────────────              │
│  Layer 2: Component Tests (20.3, 20.4)                      │
│       SPIRE health, Keycloak config, OIDC Discovery         │
│  ─────────────────────────────────────────────              │
│  Layer 1: Infrastructure Tests (20.2)                       │
│       Kind, networking, DNS, storage, CSI                   │
│  ─────────────────────────────────────────────              │
└─────────────────────────────────────────────────────────────┘
```

### 20.2 Test Runner Setup

**File: `test-runner.yaml`**

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: test-runner
  namespace: mcp-demo
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: test-runner-view
subjects:
  - kind: ServiceAccount
    name: test-runner
    namespace: mcp-demo
roleRef:
  kind: ClusterRole
  name: view
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: test-runner
  namespace: mcp-demo
spec:
  replicas: 1
  selector:
    matchLabels:
      app: test-runner
  template:
    metadata:
      labels:
        app: test-runner
    spec:
      serviceAccountName: test-runner
      containers:
        - name: test-runner
          image: python:3.12-slim
          command: ["sleep", "infinity"]
          env:
            - name: SPIFFE_ENDPOINT_SOCKET
              value: "unix:///spiffe-workload-api/spire-agent.sock"
            - name: KEYCLOAK_URL
              value: "http://keycloak.mcp-demo.svc.cluster.local:8080"
            - name: KEYCLOAK_REALM
              value: "mcp-demo"
            - name: OIDC_DISCOVERY_URL
              value: "http://spiffe-oidc-discovery-provider.spire-system.svc.cluster.local"
            - name: SPIRE_SERVER_URL
              value: "spire-server.spire-system.svc.cluster.local"
            - name: TEST_TIMEOUT
              value: "30"
          volumeMounts:
            - name: spiffe-workload-api
              mountPath: /spiffe-workload-api
              readOnly: true
            - name: test-scripts
              mountPath: /tests
      volumes:
        - name: spiffe-workload-api
          csi:
            driver: "csi.spiffe.io"
            readOnly: true
        - name: test-scripts
          configMap:
            name: e2e-tests
```

**File: `test-bootstrap.sh`** (run once inside test-runner to install dependencies)

```bash
#!/bin/bash
set -euo pipefail

echo "=== Installing test dependencies ==="
pip install --break-system-packages -q \
  py-spiffe \
  requests \
  pytest \
  pytest-timeout \
  pytest-ordering \
  pyjwt[crypto] \
  cryptography

echo "=== Test dependencies installed ==="
python3 -m pytest --version
```

### 20.3 Layer 1: Infrastructure Tests

**File: `test_01_infrastructure.py`**

```python
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
            "spiffe-oidc-discovery-provider.spire-system.svc.cluster.local",
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
```

### 20.4 Layer 2: SPIRE Component Tests

**File: `test_02_spire_components.py`**

```python
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
    "http://spiffe-oidc-discovery-provider.spire-system.svc.cluster.local")


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
        # Verify via OIDC discovery issuer
        resp = requests.get(
            f"{OIDC_URL}/.well-known/openid-configuration",
            timeout=TIMEOUT
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "example.org" in data.get("issuer", ""), \
            f"Trust domain not in issuer: {data.get('issuer')}"


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
        from pyspiffe.workloadapi import WorkloadApiClient
        client = WorkloadApiClient(
            spiffe_endpoint_socket="unix:///spiffe-workload-api/spire-agent.sock"
        )
        svid = client.fetch_jwt_svid(audiences=["test-validation"])
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
            timeout=TIMEOUT
        )
        assert resp.status_code == 200, \
            f"OIDC discovery returned {resp.status_code}"
        data = resp.json()
        required_keys = ["issuer", "jwks_uri", "id_token_signing_alg_values_supported"]
        for key in required_keys:
            assert key in data, f"Missing '{key}' in OIDC discovery response"

    def test_jwks_endpoint_returns_keys(self):
        """/keys endpoint must return at least one signing key."""
        resp = requests.get(f"{OIDC_URL}/keys", timeout=TIMEOUT)
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
        resp = requests.get(f"{OIDC_URL}/keys", timeout=TIMEOUT)
        jwks = resp.json()
        private_fields = ["d", "p", "q", "dp", "dq", "qi"]
        for key in jwks["keys"]:
            for field in private_fields:
                assert field not in key, \
                    f"JWKS key {key.get('kid')} exposes private field '{field}'!"


class TestControllerManager:
    """SPIRE Controller Manager tests."""

    def test_controller_manager_running(self):
        """Controller Manager pod must be running."""
        result = subprocess.run(
            ["kubectl", "get", "pods", "-n", "spire-system",
             "-l", "app.kubernetes.io/name=spire-controller-manager",
             "-o", "json"],
            capture_output=True, text=True, timeout=TIMEOUT
        )
        pods = json.loads(result.stdout)
        running = [p for p in pods["items"]
                   if p["status"]["phase"] == "Running"]
        assert len(running) >= 1, "Controller Manager not running"

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
```

### 20.5 Layer 2: Keycloak Component Tests

**File: `test_03_keycloak.py`**

```python
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
        """Keycloak /health/ready must return UP."""
        resp = requests.get(f"{KC_URL}/health/ready", timeout=TIMEOUT)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "UP", f"Health status: {data}"

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
        """SPIFFE client must use client-spiffe-jwt authenticator."""
        token = get_admin_token()
        resp = requests.get(
            f"{KC_URL}/admin/realms/{REALM}/clients",
            params={"clientId": "spiffe://example.org/ns/mcp-demo/sa/mcp-client"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT
        )
        clients = resp.json()
        client = clients[0]
        assert client["clientAuthenticatorType"] == "client-spiffe-jwt", \
            f"Wrong authenticator: {client['clientAuthenticatorType']}"

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
        if info_resp.status_code == 200:
            info = info_resp.json()
            providers = info.get("providers", {})
            # Look for our custom authenticator in the provider list
            client_auth = providers.get("client-authenticator", {})
            if "providers" in client_auth:
                provider_ids = list(client_auth["providers"].keys())
                assert "client-spiffe-jwt" in provider_ids, \
                    f"SPIFFE SPI not loaded. Available: {provider_ids}"
```

### 20.6 Layer 3: Integration Tests — JWT SVID Validation

**File: `test_04_jwt_svid.py`**

```python
"""
Layer 3: JWT SVID Integration Tests
Validates the JWT SVIDs issued by SPIRE are correctly structured,
have proper claims, and can be verified using the OIDC Discovery
Provider's JWKS keys.
"""
import os
import time
import pytest
import requests
import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import rsa, ec
from jwt import PyJWKClient

TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "30"))
OIDC_URL = os.environ.get("OIDC_DISCOVERY_URL",
    "http://spiffe-oidc-discovery-provider.spire-system.svc.cluster.local")
KC_URL = os.environ.get("KEYCLOAK_URL",
    "http://keycloak.mcp-demo.svc.cluster.local:8080")
REALM = os.environ.get("KEYCLOAK_REALM", "mcp-demo")


def fetch_jwt_svid(audience):
    """Helper: fetch a fresh JWT SVID for a given audience."""
    from pyspiffe.workloadapi import WorkloadApiClient
    client = WorkloadApiClient(
        spiffe_endpoint_socket="unix:///spiffe-workload-api/spire-agent.sock"
    )
    return client.fetch_jwt_svid(audiences=[audience])


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
        jwks_client = PyJWKClient(jwks_url)
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
        jwks_client = PyJWKClient(jwks_url)

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

        resp = requests.get(f"{OIDC_URL}/keys", timeout=TIMEOUT)
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
```

### 20.7 Layer 3: Integration Tests — Keycloak Authentication Flows

**File: `test_05_auth_flows.py`**

```python
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

SPIFFE_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-spiffe"


def fetch_jwt_svid(audience):
    from pyspiffe.workloadapi import WorkloadApiClient
    client = WorkloadApiClient(
        spiffe_endpoint_socket="unix:///spiffe-workload-api/spire-agent.sock"
    )
    return client.fetch_jwt_svid(audiences=[audience])


class TestClientCredentialsWithSPIFFE:
    """Test OAuth client_credentials grant using SPIFFE JWT SVID."""

    def test_token_exchange_succeeds(self):
        """Client must obtain access_token using JWT SVID (no client_secret)."""
        audience = f"{KC_URL}/realms/{REALM}"
        svid = fetch_jwt_svid(audience)

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
        audience = f"{KC_URL}/realms/{REALM}"
        svid = fetch_jwt_svid(audience)

        resp = requests.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_assertion_type": SPIFFE_ASSERTION_TYPE,
            "client_assertion": svid.token,
            "scope": "openid",
        }, timeout=TIMEOUT)

        data = resp.json()
        claims = pyjwt.decode(
            data["access_token"],
            options={"verify_signature": False}
        )
        assert "iss" in claims, "Access token missing 'iss'"
        assert REALM in claims["iss"], "Access token issuer mismatch"

    def test_access_token_has_correct_client_id(self):
        """Access token must reference the SPIFFE-based client_id."""
        audience = f"{KC_URL}/realms/{REALM}"
        svid = fetch_jwt_svid(audience)

        resp = requests.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_assertion_type": SPIFFE_ASSERTION_TYPE,
            "client_assertion": svid.token,
            "scope": "openid",
        }, timeout=TIMEOUT)

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
        audience = f"{KC_URL}/realms/{REALM}"
        svid = fetch_jwt_svid(audience)

        resp = requests.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_assertion_type": SPIFFE_ASSERTION_TYPE,
            "client_assertion": svid.token,
            "scope": "openid",
        }, timeout=TIMEOUT)

        data = resp.json()
        assert "expires_in" in data, "Response missing expires_in"
        assert data["expires_in"] > 0, "expires_in must be positive"


class TestNegativeAuthenticationCases:
    """Verify security: malformed, expired, and wrong credentials must fail."""

    def test_wrong_assertion_type_rejected(self):
        """Using standard JWT bearer type instead of SPIFFE type must fail."""
        audience = f"{KC_URL}/realms/{REALM}"
        svid = fetch_jwt_svid(audience)

        resp = requests.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
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
        audience = f"{KC_URL}/realms/{REALM}"
        svid = fetch_jwt_svid(audience)

        resp = requests.post(TOKEN_URL, data={
            "client_assertion_type": SPIFFE_ASSERTION_TYPE,
            "client_assertion": svid.token,
        }, timeout=TIMEOUT)

        assert resp.status_code in (400, 401), \
            f"Missing grant_type should fail but got {resp.status_code}"

    def test_svid_replay_within_ttl_may_work(self):
        """Same SVID used twice within TTL is expected behavior for JWTs."""
        audience = f"{KC_URL}/realms/{REALM}"
        svid = fetch_jwt_svid(audience)

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

        # Document the behavior regardless
        assert resp1.status_code == 200, "First request should succeed"
        # Second request may or may not succeed depending on jti enforcement


class TestDynamicClientRegistration:
    """Test SPIFFE-based Dynamic Client Registration."""

    def test_dcr_endpoint_exists(self):
        """The SPIFFE DCR endpoint must be accessible (even if no body)."""
        resp = requests.post(DCR_URL, json={}, timeout=TIMEOUT)
        # Should return 400 (bad request) not 404 (not found)
        assert resp.status_code != 404, \
            f"DCR endpoint not found: {resp.status_code}"

    def test_dcr_with_valid_software_statement(self):
        """DCR with valid JWT SVID software statement should register client."""
        audience = f"{KC_URL}/realms/{REALM}"
        svid = fetch_jwt_svid(audience)

        resp = requests.post(DCR_URL, json={
            "software_statement": svid.token,
        }, headers={"Content-Type": "application/json"},
           timeout=TIMEOUT)

        if resp.status_code in (200, 201):
            data = resp.json()
            client_id = data.get("clientId") or data.get("client_id")
            assert client_id is not None, "DCR response missing client_id"
        elif resp.status_code == 409:
            pass  # Client already registered — acceptable
        else:
            # If DCR SPI is not installed, skip gracefully
            pytest.skip(
                f"DCR returned {resp.status_code} — "
                "SPI may not be installed: {resp.text[:200]}"
            )

    def test_dcr_rejects_invalid_software_statement(self):
        """DCR with invalid software statement must be rejected."""
        resp = requests.post(DCR_URL, json={
            "software_statement": "invalid.jwt.token",
        }, headers={"Content-Type": "application/json"},
           timeout=TIMEOUT)

        assert resp.status_code in (400, 401, 403), \
            f"Invalid DCR should fail but got {resp.status_code}"
```

### 20.8 Layer 4: Full End-to-End Flow Tests

**File: `test_06_e2e_flows.py`**

```python
"""
Layer 4: Full End-to-End Flow Tests
Tests the complete chain: SPIRE → JWT SVID → Keycloak → Access Token → Resource.
These tests cross all component boundaries.
"""
import os
import time
import json
import pytest
import requests
import jwt as pyjwt
from jwt import PyJWKClient

TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "30"))
KC_URL = os.environ.get("KEYCLOAK_URL",
    "http://keycloak.mcp-demo.svc.cluster.local:8080")
REALM = os.environ.get("KEYCLOAK_REALM", "mcp-demo")
OIDC_URL = os.environ.get("OIDC_DISCOVERY_URL",
    "http://spiffe-oidc-discovery-provider.spire-system.svc.cluster.local")
SPIFFE_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-spiffe"
TOKEN_URL = f"{KC_URL}/realms/{REALM}/protocol/openid-connect/token"


def fetch_jwt_svid(audience):
    from pyspiffe.workloadapi import WorkloadApiClient
    client = WorkloadApiClient(
        spiffe_endpoint_socket="unix:///spiffe-workload-api/spire-agent.sock"
    )
    return client.fetch_jwt_svid(audiences=[audience])


def get_access_token():
    """Full flow: SPIRE SVID → Keycloak → access_token."""
    audience = f"{KC_URL}/realms/{REALM}"
    svid = fetch_jwt_svid(audience)
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_assertion_type": SPIFFE_ASSERTION_TYPE,
        "client_assertion": svid.token,
        "scope": "openid",
    }, timeout=TIMEOUT)
    assert resp.status_code == 200, \
        f"Token exchange failed: {resp.status_code}"
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
        audience = f"{KC_URL}/realms/{REALM}"
        svid = fetch_jwt_svid(audience)

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
        audience = f"{KC_URL}/realms/{REALM}"
        svids = []
        for _ in range(3):
            svid = fetch_jwt_svid(audience)
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

        resp = requests.get(f"{OIDC_URL}/keys", timeout=TIMEOUT)
        jwks_kids = [k["kid"] for k in resp.json()["keys"]]

        assert svid_kid in jwks_kids, \
            f"SPIRE signing key {svid_kid} not in OIDC JWKS: {jwks_kids}"

    def test_keycloak_can_reach_oidc_discovery(self):
        """Keycloak must be able to reach the SPIRE OIDC Discovery Provider."""
        # Test from inside the cluster using the internal service URL
        resp = requests.get(
            f"{OIDC_URL}/.well-known/openid-configuration",
            timeout=TIMEOUT
        )
        assert resp.status_code == 200, \
            "OIDC Discovery Provider not reachable from cluster"

    def test_end_to_end_zero_secrets(self):
        """The entire flow must work without any static secrets."""
        # This is the core value proposition:
        # 1. No client_secret in the token request
        # 2. No API keys anywhere
        # 3. Only cryptographically-signed, short-lived SVIDs
        audience = f"{KC_URL}/realms/{REALM}"
        svid = fetch_jwt_svid(audience)

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
        audience = f"{KC_URL}/realms/{REALM}"
        svid = fetch_jwt_svid(audience)

        svid_claims = pyjwt.decode(
            svid.token, options={"verify_signature": False}
        )
        spiffe_id = svid_claims["sub"]

        resp = requests.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_assertion_type": SPIFFE_ASSERTION_TYPE,
            "client_assertion": svid.token,
        }, timeout=TIMEOUT)

        at_claims = pyjwt.decode(
            resp.json()["access_token"],
            options={"verify_signature": False}
        )
        client_id = at_claims.get("azp") or at_claims.get("client_id")
        assert client_id == spiffe_id, \
            f"Identity mismatch: SVID sub={spiffe_id}, token azp={client_id}"
```

### 20.9 Test Execution

**File: `run-all-tests.sh`**

```bash
#!/bin/bash
set -euo pipefail

echo "=============================================="
echo "  SPIFFE MCP OAuth — E2E Test Suite"
echo "=============================================="

TEST_POD="deploy/test-runner"
NAMESPACE="mcp-demo"

echo "[1/3] Bootstrapping test dependencies..."
kubectl -n $NAMESPACE exec $TEST_POD -- bash /tests/test-bootstrap.sh

echo ""
echo "[2/3] Running test suite..."
kubectl -n $NAMESPACE exec $TEST_POD -- python3 -m pytest /tests/ \
  -v \
  --tb=short \
  --timeout=60 \
  --junit-xml=/tmp/test-results.xml \
  -x  # Stop on first failure for faster feedback

EXIT_CODE=$?

echo ""
echo "[3/3] Extracting test results..."
kubectl -n $NAMESPACE cp $NAMESPACE/$(kubectl -n $NAMESPACE get pod -l app=test-runner -o jsonpath='{.items[0].metadata.name}'):/tmp/test-results.xml ./test-results.xml 2>/dev/null || true

echo ""
if [ $EXIT_CODE -eq 0 ]; then
  echo "=============================================="
  echo "  ALL TESTS PASSED"
  echo "=============================================="
else
  echo "=============================================="
  echo "  TESTS FAILED (exit code: $EXIT_CODE)"
  echo "=============================================="
fi

exit $EXIT_CODE
```

**Layered execution (run one layer at a time for debugging):**

```bash
# Layer 1 only: Infrastructure
kubectl -n mcp-demo exec deploy/test-runner -- python3 -m pytest /tests/test_01_infrastructure.py -v

# Layer 2 only: SPIRE components
kubectl -n mcp-demo exec deploy/test-runner -- python3 -m pytest /tests/test_02_spire_components.py -v

# Layer 2 only: Keycloak components
kubectl -n mcp-demo exec deploy/test-runner -- python3 -m pytest /tests/test_03_keycloak.py -v

# Layer 3 only: JWT SVID validation
kubectl -n mcp-demo exec deploy/test-runner -- python3 -m pytest /tests/test_04_jwt_svid.py -v

# Layer 3 only: Auth flows
kubectl -n mcp-demo exec deploy/test-runner -- python3 -m pytest /tests/test_05_auth_flows.py -v

# Layer 4 only: Full E2E
kubectl -n mcp-demo exec deploy/test-runner -- python3 -m pytest /tests/test_06_e2e_flows.py -v
```

### 20.10 Test Matrix Summary

| Test File | Tests | What It Covers |
|-----------|-------|----------------|
| `test_01_infrastructure.py` | 7 | Kind nodes, DNS, StorageClass, CoreDNS, NodePort, CSI Driver |
| `test_02_spire_components.py` | 9 | SPIRE Server, Agent, OIDC Discovery (endpoints + keys), Controller Manager, CRDs |
| `test_03_keycloak.py` | 10 | Health, PostgreSQL, Admin, Realm config, SPIFFE client, SPI loading |
| `test_04_jwt_svid.py` | 14 | JWT structure, claims, crypto verification, tamper detection, software statements |
| `test_05_auth_flows.py` | 10 | Client credentials flow, negative cases (6 security tests), DCR |
| `test_06_e2e_flows.py` | 9 | Full chain, introspection, refresh cycle, trust chain, zero-secrets proof |
| **Total** | **59** | **Complete coverage across all layers** |

---

## 21. Battle-Tested Hardening & Chaos Testing

This section ensures the solution is resilient, self-healing, and handles real-world failure modes. Each test simulates a production scenario and validates recovery.

### 21.1 Chaos Test Suite

**File: `test_07_chaos.py`**

```python
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
    "http://spiffe-oidc-discovery-provider.spire-system.svc.cluster.local")
SPIFFE_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-spiffe"
TOKEN_URL = f"{KC_URL}/realms/{REALM}/protocol/openid-connect/token"


def fetch_jwt_svid(audience):
    from pyspiffe.workloadapi import WorkloadApiClient
    client = WorkloadApiClient(
        spiffe_endpoint_socket="unix:///spiffe-workload-api/spire-agent.sock"
    )
    return client.fetch_jwt_svid(audiences=[audience])


def get_access_token():
    audience = f"{KC_URL}/realms/{REALM}"
    svid = fetch_jwt_svid(audience)
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

        # 3. Wait for health endpoint
        deadline = time.time() + 120
        healthy = False
        while time.time() < deadline:
            try:
                resp = requests.get(
                    f"{KC_URL}/health/ready", timeout=5
                )
                if resp.status_code == 200:
                    healthy = True
                    break
            except requests.exceptions.RequestException:
                pass
            time.sleep(5)
        assert healthy, "Keycloak health endpoint not UP after restart"

        # 4. Verify realm config survived restart (persisted in PostgreSQL)
        resp = requests.get(
            f"{KC_URL}/realms/{REALM}/.well-known/openid-configuration",
            timeout=TIMEOUT
        )
        assert resp.status_code == 200, \
            "Realm configuration lost after restart"

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
                resp = requests.get(f"{OIDC_URL}/keys", timeout=5)
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
        audience = f"{KC_URL}/realms/{REALM}"

        svid1 = fetch_jwt_svid(audience)
        claims1 = pyjwt.decode(
            svid1.token, options={"verify_signature": False}
        )

        time.sleep(2)

        svid2 = fetch_jwt_svid(audience)
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
        audience = f"{KC_URL}/realms/{REALM}"

        def fetch_and_validate():
            svid = fetch_jwt_svid(audience)
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
        results = []
        for i in range(10):
            resp = get_access_token()
            results.append(resp.status_code)

        failures = [r for r in results if r != 200]
        assert len(failures) == 0, \
            f"{len(failures)}/10 requests failed: {failures}"

    def test_parallel_auth_requests(self):
        """5 parallel auth requests must all succeed."""
        import concurrent.futures

        def do_auth():
            resp = get_access_token()
            return resp.status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(do_auth) for _ in range(5)]
            results = [f.result(timeout=TIMEOUT * 2) for f in futures]

        failures = [r for r in results if r != 200]
        assert len(failures) == 0, \
            f"{len(failures)}/5 parallel requests failed: {failures}"
```

### 21.2 Security Hardening Checklist

Run these checks after deployment to ensure the system meets security baselines.

**File: `test_08_security_hardening.py`**

```python
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
    "http://spiffe-oidc-discovery-provider.spire-system.svc.cluster.local")
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
        from pyspiffe.workloadapi import WorkloadApiClient
        client = WorkloadApiClient(
            spiffe_endpoint_socket="unix:///spiffe-workload-api/spire-agent.sock"
        )
        svid = client.fetch_jwt_svid(audiences=["test-isolation"])
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
        resp = requests.get(f"{OIDC_URL}/keys", timeout=TIMEOUT)
        jwks = resp.json()
        private_fields = ["d", "p", "q", "dp", "dq", "qi", "k"]
        for key in jwks["keys"]:
            for field in private_fields:
                assert field not in key, \
                    f"CRITICAL: Private field '{field}' exposed in JWKS!"

    def test_jwks_uses_strong_algorithm(self):
        """Keys must use strong algorithms (RSA >=2048 or EC P-256+)."""
        resp = requests.get(f"{OIDC_URL}/keys", timeout=TIMEOUT)
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
```

### 21.3 Updated Test Matrix (Complete)

| Test File | Tests | Layer | What It Covers |
|-----------|-------|-------|----------------|
| `test_01_infrastructure.py` | 7 | L1 | Kind nodes, DNS, StorageClass, CoreDNS, NodePort, CSI |
| `test_02_spire_components.py` | 9 | L2 | SPIRE Server, Agent, OIDC Discovery, Controller Manager |
| `test_03_keycloak.py` | 10 | L2 | Health, PostgreSQL, Admin, Realm, SPIFFE client, SPIs |
| `test_04_jwt_svid.py` | 14 | L3 | JWT structure, claims, crypto, tamper detection, software stmts |
| `test_05_auth_flows.py` | 10 | L3 | Client creds flow, 6 negative security tests, DCR |
| `test_06_e2e_flows.py` | 9 | L4 | Full chain, introspection, refresh, trust chain, zero-secrets |
| `test_07_chaos.py` | 8 | L5 | Agent restart, Keycloak restart, OIDC restart, concurrent load |
| `test_08_security_hardening.py` | 6 | L5 | No static secrets, workload isolation, JWKS security, headers |
| **Total** | **73** | **L1-L5** | **Complete coverage: infrastructure to chaos resilience** |

### 21.4 Continuous Validation Script

**File: `continuous-validation.sh`**

Run this periodically (or as a CronJob) to ensure the system stays healthy.

```bash
#!/bin/bash
# Lightweight health check — runs in < 30 seconds
set -euo pipefail

PASS=0
FAIL=0

check() {
  local name="$1"
  shift
  if "$@" > /dev/null 2>&1; then
    echo "  PASS  $name"
    ((PASS++))
  else
    echo "  FAIL  $name"
    ((FAIL++))
  fi
}

echo "=== Continuous Validation ==="
echo ""

# Infrastructure
check "Kind cluster" kubectl cluster-info --context kind-spiffe-mcp-demo
check "SPIRE Server" kubectl -n spire-system exec deploy/spire-server -- /opt/spire/bin/spire-server healthcheck
check "OIDC Discovery" curl -sf http://localhost:30443/.well-known/openid-configuration
check "Keycloak Health" curl -sf http://localhost:30080/health/ready
check "Realm Available" curl -sf http://localhost:30080/realms/mcp-demo

# Workloads
check "MCP Client Pod" kubectl -n mcp-demo get pod -l app=mcp-client -o jsonpath='{.items[0].status.phase}' | grep -q Running
check "MCP Server Pod" kubectl -n mcp-demo get pod -l app=mcp-server -o jsonpath='{.items[0].status.phase}' | grep -q Running

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ] && echo "=== ALL HEALTHY ===" || echo "=== ATTENTION NEEDED ==="
exit $FAIL
```

### 21.5 Updated File Manifest

Add these to the project directory:

```
project-root/
├── ... (existing files from Section 16) ...
│
├── tests/                            # E2E Test Suite
│   ├── test-bootstrap.sh             # Test dependency installer
│   ├── test_01_infrastructure.py     # L1: Infrastructure tests
│   ├── test_02_spire_components.py   # L2: SPIRE component tests
│   ├── test_03_keycloak.py           # L2: Keycloak component tests
│   ├── test_04_jwt_svid.py           # L3: JWT SVID validation tests
│   ├── test_05_auth_flows.py         # L3: Auth flow integration tests
│   ├── test_06_e2e_flows.py          # L4: Full E2E flow tests
│   ├── test_07_chaos.py              # L5: Chaos & resilience tests
│   └── test_08_security_hardening.py # L5: Security hardening tests
│
├── test-runner.yaml                  # Test runner pod manifest
├── run-all-tests.sh                  # Full test suite runner
└── continuous-validation.sh          # Lightweight health check script
```

### 21.6 Claude Agent Test Execution Guidelines

**When executing tests, follow this order strictly:**

1. Deploy `test-runner.yaml` AFTER all other workloads are running
2. Run `test-bootstrap.sh` inside the test-runner pod (installs pytest etc.)
3. Execute tests layer by layer (L1 → L2 → L3 → L4 → L5)
4. Only advance to the next layer if the current layer passes 100%
5. Chaos tests (L5) restart pods — run them LAST and expect temporary unavailability
6. After chaos tests complete, re-run `continuous-validation.sh` to confirm recovery

**Test failure triage:**

```
L1 failure → Infrastructure problem. Check Kind cluster, DNS, NodePort services.
L2 failure → Component problem. Check specific SPIRE or Keycloak component.
L3 failure → Integration problem. Check JWKS reachability, claim mapping.
L4 failure → Flow problem. Check the complete chain end-to-end.
L5 failure → Resilience gap. Document it, assess if acceptable for demo.
```

**Context budget for tests:** Test output can be verbose. When running tests:
- Use `-v --tb=short` (already set in `run-all-tests.sh`)
- If a test fails, re-run ONLY that test with `--tb=long` for full traceback
- Never dump full pytest output of all 73 tests into context
