#!/usr/bin/env bash
# run-unit-tests.sh — Hyades job body for hermes-agent unit tests.
# Expects: target/ already checked out under $GITHUB_WORKSPACE; fast-disk.sh already run;
# env HERMES_TEST_WORKERS, TARGET_REF. Must start with cd "$PWD" (Hyades cwd quirk).
set -euo pipefail
cd "$PWD"
echo "BENCHSTEP start setup ref=${TARGET_REF:-unknown}"
echo "ref ${TARGET_REF:-} is $(git -C target rev-parse HEAD)"
git -C target log -1 --format='%h %s'
nproc
free -h | head -2
df -hT "$GITHUB_WORKSPACE" /mnt/fast 2>/dev/null || true

# ripgrep (some hermes tests shell out to rg)
RG_VERSION=15.1.0
RG_SHA256=1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599
RG_TARBALL=ripgrep-${RG_VERSION}-x86_64-unknown-linux-musl.tar.gz
curl -sSfL --retry 3 --retry-delay 5 -o "$RG_TARBALL" \
  "https://github.com/BurntSushi/ripgrep/releases/download/${RG_VERSION}/${RG_TARBALL}"
echo "${RG_SHA256}  ${RG_TARBALL}" | sha256sum -c -
tar -xzf "$RG_TARBALL"
mv "ripgrep-${RG_VERSION}-x86_64-unknown-linux-musl/rg" /usr/local/bin/rg
rm -rf "$RG_TARBALL" "ripgrep-${RG_VERSION}-x86_64-unknown-linux-musl"
rg --version

# uv 0.12.13 pinned to match GitHub tests.yml (CPython 3.11.16 / SQLite 3.53.1)
curl -LsSf https://astral.sh/uv/0.12.13/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version

cd "$GITHUB_WORKSPACE/target"
echo "BENCHSTEP uv python install"
uv python install 3.11
echo "BENCHSTEP uv sync"
uv sync --locked --python 3.11 \
  --extra all --extra dev --extra anthropic --extra mistral \
  --extra fal --extra modal --extra daytona --extra parallel-web --extra bedrock
uv cache prune --ci

echo "BENCHSTEP sqlite wal check"
.venv/bin/python -c "import sqlite3, sys, hermes_state_wal as w; v = w.is_sqlite_wal_reset_vulnerable(); print(f'Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}, WAL-reset vulnerable: {v}'); sys.exit(1 if v else 0)"

echo "BENCHSTEP run_tests workers=${HERMES_TEST_WORKERS:-16}"
# shellcheck disable=SC1091
source .venv/bin/activate
scripts/run_tests.sh
echo "BENCHSTEP done"
