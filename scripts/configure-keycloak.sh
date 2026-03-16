#!/bin/bash
set -euo pipefail

# Configure Keycloak: add SPIFFE SPI to client authentication flow
# Must run after Keycloak is ready and realm is imported

KC_URL="${KEYCLOAK_URL:-http://localhost:30080}"
REALM="mcp-demo"
JWKS_URL="${SPIFFE_JWKS_URL:-http://oidc-http-proxy.spire-system.svc.cluster.local/keys}"

echo "=== Configuring Keycloak for SPIFFE Authentication ==="

# Get admin token
echo "[1/5] Getting admin token..."
TOKEN=$(curl -sf "${KC_URL}/realms/master/protocol/openid-connect/token" \
  -d "grant_type=password" \
  -d "client_id=admin-cli" \
  -d "username=admin" \
  -d "password=admin" | jq -r '.access_token')

if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
  echo "ERROR: Failed to get admin token"
  exit 1
fi

AUTH="Authorization: Bearer ${TOKEN}"

# Copy clients flow and add SPIFFE execution
echo "[2/5] Creating SPIFFE client authentication flow..."
# Check if flow already exists
EXISTING=$(curl -sf -H "$AUTH" "${KC_URL}/admin/realms/${REALM}/authentication/flows" | jq -r '.[] | select(.alias=="spiffe-clients") | .id')
if [ -z "$EXISTING" ]; then
  curl -sf -X POST -H "$AUTH" -H "Content-Type: application/json" \
    "${KC_URL}/admin/realms/${REALM}/authentication/flows/clients/copy" \
    -d '{"newName":"spiffe-clients"}' > /dev/null
  echo "  Created spiffe-clients flow"
else
  echo "  Flow spiffe-clients already exists"
fi

# Add SPIFFE execution if not present
echo "[3/5] Adding SPIFFE SVID JWT authenticator..."
HAS_SPIFFE=$(curl -sf -H "$AUTH" "${KC_URL}/admin/realms/${REALM}/authentication/flows/spiffe-clients/executions" \
  | jq -r '.[] | select(.providerId=="client-spiffe-jwt") | .id')
if [ -z "$HAS_SPIFFE" ]; then
  curl -sf -X POST -H "$AUTH" -H "Content-Type: application/json" \
    "${KC_URL}/admin/realms/${REALM}/authentication/flows/spiffe-clients/executions/execution" \
    -d '{"provider":"client-spiffe-jwt"}' > /dev/null
  echo "  Added client-spiffe-jwt execution"
fi

# Set SPIFFE execution to ALTERNATIVE
SPIFFE_EXEC=$(curl -sf -H "$AUTH" "${KC_URL}/admin/realms/${REALM}/authentication/flows/spiffe-clients/executions" \
  | jq '.[] | select(.providerId=="client-spiffe-jwt")')
SPIFFE_ID=$(echo "$SPIFFE_EXEC" | jq -r '.id')
if [ "$(echo "$SPIFFE_EXEC" | jq -r '.requirement')" != "ALTERNATIVE" ]; then
  UPDATED=$(echo "$SPIFFE_EXEC" | jq '.requirement = "ALTERNATIVE"')
  curl -sf -X PUT -H "$AUTH" -H "Content-Type: application/json" \
    "${KC_URL}/admin/realms/${REALM}/authentication/flows/spiffe-clients/executions" \
    -d "$UPDATED" > /dev/null
  echo "  Set SPIFFE execution to ALTERNATIVE"
fi

# Bind realm to use spiffe-clients flow
echo "[4/5] Binding realm to SPIFFE-enabled flow..."
REALM_JSON=$(curl -sf -H "$AUTH" "${KC_URL}/admin/realms/${REALM}")
CURRENT_FLOW=$(echo "$REALM_JSON" | jq -r '.clientAuthenticationFlow')
if [ "$CURRENT_FLOW" != "spiffe-clients" ]; then
  UPDATED_REALM=$(echo "$REALM_JSON" | jq '.clientAuthenticationFlow = "spiffe-clients"')
  curl -sf -X PUT -H "$AUTH" -H "Content-Type: application/json" \
    "${KC_URL}/admin/realms/${REALM}" \
    -d "$UPDATED_REALM" > /dev/null
  echo "  Bound realm to spiffe-clients flow"
else
  echo "  Already bound"
fi

# Update realm and client JWKS URLs
echo "[5/5] Updating JWKS URLs..."
REALM_JSON=$(curl -sf -H "$AUTH" "${KC_URL}/admin/realms/${REALM}")
UPDATED_REALM=$(echo "$REALM_JSON" | jq --arg url "$JWKS_URL" '.attributes["spiffe.jwks.url"] = $url')
curl -sf -X PUT -H "$AUTH" -H "Content-Type: application/json" \
  "${KC_URL}/admin/realms/${REALM}" \
  -d "$UPDATED_REALM" > /dev/null

# Update SPIFFE client JWKS URL
CLIENT=$(curl -sf -H "$AUTH" "${KC_URL}/admin/realms/${REALM}/clients?clientId=spiffe://example.org/ns/mcp-demo/sa/mcp-client" | jq '.[0]')
CLIENT_UUID=$(echo "$CLIENT" | jq -r '.id')
UPDATED_CLIENT=$(echo "$CLIENT" | jq --arg url "$JWKS_URL" '
  .attributes["jwks.url"] = $url |
  .attributes["use.jwks.url"] = "true" |
  .attributes["issuer"] = "https://oidc-discovery.example.org"
')
curl -sf -X PUT -H "$AUTH" -H "Content-Type: application/json" \
  "${KC_URL}/admin/realms/${REALM}/clients/${CLIENT_UUID}" \
  -d "$UPDATED_CLIENT" > /dev/null

echo ""
echo "=== Keycloak SPIFFE Configuration Complete ==="
echo "  Flow: spiffe-clients (with client-spiffe-jwt ALTERNATIVE)"
echo "  JWKS URL: ${JWKS_URL}"
echo "  Realm bound to spiffe-clients flow"
