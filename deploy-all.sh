#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=============================================="
echo "  SPIFFE + MCP OAuth + Keycloak in Kind"
echo "=============================================="

# Step 1: Create Kind cluster
echo "[1/8] Creating Kind cluster..."
kind create cluster --config "$SCRIPT_DIR/infra/kind-config.yaml"
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
  --values "$SCRIPT_DIR/infra/spire-values.yaml" \
  --wait --timeout 300s

# Step 3: Build custom Keycloak image
echo "[3/7] Building custom Keycloak image with SPIFFE SPIs..."
docker build -f "$SCRIPT_DIR/docker/Dockerfile.keycloak-custom" -t keycloak-spiffe:latest "$SCRIPT_DIR"
kind load docker-image keycloak-spiffe:latest --name spiffe-mcp-demo

# Step 4: Deploy PostgreSQL
echo "[4/7] Deploying PostgreSQL..."
kubectl apply -f "$SCRIPT_DIR/manifests/postgres.yaml"
kubectl -n mcp-demo wait --for=condition=ready pod -l app=postgres --timeout=120s

# Step 5: Deploy Keycloak
echo "[5/7] Deploying Keycloak with SPIFFE SPIs..."
kubectl apply -f "$SCRIPT_DIR/manifests/keycloak.yaml"
kubectl apply -f "$SCRIPT_DIR/manifests/oidc-nodeport.yaml"
echo "Waiting for Keycloak..."
kubectl -n mcp-demo wait --for=condition=ready pod -l app=keycloak --timeout=300s

# Step 6: Create SPIFFE IDs and deploy workloads
echo "[6/7] Creating ClusterSPIFFEIDs and deploying workloads..."
kubectl apply -f "$SCRIPT_DIR/manifests/cluster-spiffe-ids.yaml"
kubectl apply -f "$SCRIPT_DIR/manifests/mcp-client.yaml"
kubectl apply -f "$SCRIPT_DIR/manifests/mcp-server.yaml"
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
