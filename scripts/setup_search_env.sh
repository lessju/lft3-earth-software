#!/usr/bin/env bash
# Python environment for the GPU searches (hyperseti on CuPy, CUDA 13). Separate from the casperfpga venv
# (Python 3.8): CuPy for CUDA 13 needs Python >= 3.10. No root needed.
#   scripts/setup_search_env.sh [/opt/lft3arth/search_venv]
set -euo pipefail
VENV=${1:-/opt/lft3arth/search_venv}
REPO=$(cd "$(dirname "$0")/.." && pwd)
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
# hyperseti is not on PyPI; pinned to the commit tested on the Spark (2026-11-13). blimpy (a dependency) still
# imports pkg_resources, which setuptools >= 81 no longer ships.
"$VENV/bin/pip" install -q "setuptools<81" "git+https://github.com/UCBerkeleySETI/hyperseti@a15f7eb"
# hyperseti depends on the generic "cupy" (source) package; replace it with the CUDA 13 wheel. Both provide the
# module "cupy", so uninstall both and install the wheel alone.
"$VENV/bin/pip" uninstall -q -y cupy cupy-cuda13x
"$VENV/bin/pip" install -q cupy-cuda13x
"$VENV/bin/pip" install -q --no-deps -e "$REPO"     # lft3 itself (lft3-search, lft3.ring); deps already present
"$VENV/bin/pip" install -q pyyaml
"$VENV/bin/python" -c "import cupy, hyperseti, lft3; print('search env ok: cupy', cupy.__version__)"
echo "use: $VENV/bin/lft3-search --help"
