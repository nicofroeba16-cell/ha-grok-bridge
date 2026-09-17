#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
package_root="$(cd -- "$script_dir/.." && pwd)"
export PYTHONPATH="$package_root/src${PYTHONPATH:+:$PYTHONPATH}"
export GH_TOKEN="$(gh auth token -h github.com)"
exec /usr/bin/python3 -m worker_orchestrator.browser_wake_search run
