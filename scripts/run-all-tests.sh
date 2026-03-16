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
