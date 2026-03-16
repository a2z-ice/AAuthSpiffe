#!/bin/bash
set -euo pipefail

echo "=== Installing test dependencies ==="
pip install --break-system-packages -q \
  spiffe \
  requests \
  pytest \
  pytest-timeout \
  pytest-ordering \
  pyjwt[crypto] \
  cryptography

echo "=== Test dependencies installed ==="
python3 -m pytest --version
