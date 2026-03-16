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
