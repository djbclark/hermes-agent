#!/usr/bin/env bash
# run-unit-tests.sh — GitHub Actions job body for hermes-agent unit tests (ci/base).
# Expects: repo already checked out at $GITHUB_WORKSPACE; env HERMES_TEST_WORKERS,
# HERMES_TEST_SLICE (I/N), TARGET_REF. No Hyades/libkrun workarounds (no fast-disk,
# portshim, or ulimit hack) — standard ubuntu-latest is enough.
set -euo pipefail
cd "${GITHUB_WORKSPACE:?}"

echo "BENCHSTEP start setup ref=${TARGET_REF:-unknown} slice=${HERMES_TEST_SLICE:-all}"
echo "ref ${TARGET_REF:-} is $(git rev-parse HEAD)"
git log -1 --format='%h %s'
nproc
free -h | head -2
df -hT "$GITHUB_WORKSPACE" || true

# ripgrep (some hermes tests shell out to rg)
RG_VERSION=15.1.0
RG_SHA256=1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599
RG_TARBALL=ripgrep-${RG_VERSION}-x86_64-unknown-linux-musl.tar.gz
curl -sSfL --retry 3 --retry-delay 5 -o "$RG_TARBALL" \
  "https://github.com/BurntSushi/ripgrep/releases/download/${RG_VERSION}/${RG_TARBALL}"
echo "${RG_SHA256}  ${RG_TARBALL}" | sha256sum -c -
tar -xzf "$RG_TARBALL"
sudo mv "ripgrep-${RG_VERSION}-x86_64-unknown-linux-musl/rg" /usr/local/bin/rg
rm -rf "$RG_TARBALL" "ripgrep-${RG_VERSION}-x86_64-unknown-linux-musl"
rg --version

# uv 0.12.13 pinned to match upstream tests.yml (CPython / SQLite story)
curl -LsSf https://astral.sh/uv/0.12.13/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version

# Upstream v0.22+ moved `dev` from an extra to [dependency-groups] and pins Python 3.14
# (tests.yml: uv sync --locked --python 3.14 --extra all --group dev ...); older trees use the
# 3.11 extra layout. Pick by the tree under test. Only extras the tree defines are passed.
if grep -q '^\[dependency-groups\]' pyproject.toml; then
  py=3.14; dev=(--group dev)
else
  py=3.11; dev=(--extra dev)
fi
extras=()
for e in all anthropic mistral fal modal daytona parallel-web bedrock; do
  grep -qE "^${e} = \[" pyproject.toml && extras+=(--extra "$e")
done
echo "BENCHSTEP uv python install $py"
uv python install "$py"
echo "BENCHSTEP uv sync (python $py, ${dev[*]}, ${#extras[@]} extras)"
uv sync --locked --python "$py" "${extras[@]}" "${dev[@]}"
uv cache prune --ci

echo "BENCHSTEP sqlite wal check"
.venv/bin/python -c "import sqlite3, sys, hermes_state_wal as w; v = w.is_sqlite_wal_reset_vulnerable(); print(f'Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}, WAL-reset vulnerable: {v}'); sys.exit(1 if v else 0)"

echo "BENCHSTEP run_tests workers=${HERMES_TEST_WORKERS:-4} slice=${HERMES_TEST_SLICE:-}"
# shellcheck disable=SC1091
source .venv/bin/activate

mkdir -p "$GITHUB_WORKSPACE/_ci_summary"
log="$GITHUB_WORKSPACE/_ci_summary/run.log"
set +e
scripts/run_tests.sh 2>&1 | tee "$log"
rc=${PIPESTATUS[0]}
set -e

summary="$GITHUB_WORKSPACE/_ci_summary/summary.txt"
{
  echo "slice=${HERMES_TEST_SLICE:-all}"
  echo "ref=${TARGET_REF:-}"
  echo "sha=$(git rev-parse HEAD)"
  echo "exit=$rc"
  # Prefer the runner's aggregate line if present.
  grep -E '[0-9]+ (files?|tests?) passed|[0-9]+ failed|[0-9]+ error' "$log" | tail -n 20 || true
  echo "-- FAILED/ERROR node ids --"
  grep -E '^(FAILED|ERROR) ' "$log" | sort -u || true
} > "$summary"
echo "BENCHSTEP summary"
cat "$summary"
echo "BENCHSTEP done rc=$rc"
exit "$rc"
