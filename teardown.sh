#!/bin/bash
echo "Tearing down Kind cluster..."
kind delete cluster --name spiffe-mcp-demo
echo "Cleaning up docker images..."
docker rmi keycloak-spiffe:latest 2>/dev/null || true
docker rmi spire-software-statements:latest 2>/dev/null || true
echo "Done. No residual installations on host."
