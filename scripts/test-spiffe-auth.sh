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
pip install --break-system-packages -q spiffe requests

echo "=== Step 2: Fetch JWT SVID from SPIRE ==="
python3 << 'PYEOF'
import json
import requests
from spiffe import WorkloadApiClient

# Connect to SPIRE Agent via Workload API
client = WorkloadApiClient("unix:///spiffe-workload-api/spire-agent.sock")

# Fetch JWT SVID with audience set to Keycloak realm URL
keycloak_url = "http://keycloak.mcp-demo.svc.cluster.local:8080"
realm = "mcp-demo"
audience = f"{keycloak_url}/realms/{realm}"

jwt_svid = client.fetch_jwt_svid(audience={audience})
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
