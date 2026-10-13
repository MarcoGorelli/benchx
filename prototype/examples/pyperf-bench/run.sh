#!/usr/bin/env bash
# Run the example suite with `bx bench`. See README.md for what each step does.
#
#   ./run.sh                 uses .benchx/config.json here (5 processes x 3 values)
#   ./run.sh --processes 20  extra arguments go to `bx bench` and win over the file
#
# Run it in an activated environment that has benchx installed with pyperf
# (from prototype/: `pip install -e . pyperf`, or `uv sync --extra test`).
# Results go to ./out, and the local store to ./out/home (BENCHX_HOME), so
# nothing outside this directory changes.
set -euo pipefail
cd "$(dirname "$0")"
export BENCHX_HOME="$PWD/out/home"
bx() { printf '\033[36m$ bx %s\033[0m\n' "$*" >&2; command bx "$@"; }

command bx --help >/dev/null 2>&1 && python -c "import pyperf" 2>/dev/null || {
  echo "missing dependencies: install benchx and pyperf into the active environment (see README.md)" >&2
  exit 1; }
rm -rf out; mkdir -p out

echo "== 1. all bench_* functions, precision from .benchx/config.json"
bx bench bench_suite.py --run-key example-all --out out/all "$@"

echo; echo "== 2. one function, an environment variable set for the workers, flags over the file"
bx bench bench_suite.py bench_env_var -e EXAMPLE_N=100000 --processes 3 --values 2 \
  --run-key example-env --out out/env

echo; echo "== 3. what is in the local store"
bx head -n 10

echo; echo "== 4. what was written"
find out/all out/env -type f | sort | sed 's/^/  /'
