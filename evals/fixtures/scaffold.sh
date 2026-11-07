#!/bin/bash
# Shared scaffold for behavioral cases: writes the fixtures into the run's
# empty workspace (the current directory).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$here/make_inventory.py" inventory_service.py
cp "$here/small_util.py" small_util.py
