# GitHub CI: pytest any ref (sharded)

CI for this fork’s unit suite on **free** `ubuntu-latest` runners lives on
branch `ci/base` (not on `main`). It is the GitHub counterpart of the Forgejo
Hyades flow on `ha/base` (`scripts/forgejo-test-ref`).

## Invoke

From a checkout that has `ci/base` (or after `git fetch origin ci/base`):

```bash
git fetch origin ci/base
git checkout ci/base -- scripts/github-test-ref
scripts/github-test-ref main
scripts/github-test-ref --no-wait main
scripts/github-test-ref snap/merged-72a95c5bb2 snap/pristine-v0.22.2
```

Each call builds an overlay commit (target tree + `ci/base` workflow/scripts,
other `.github/workflows/*` removed), pushes `ci/run/<shortsha>-<ts>`, and
waits for the 10-shard matrix. Two refs run concurrently (20 jobs = free-plan
cap).

## Layout on `ci/base`

1. `.github/workflows/ha-test-ref.yml` — `push: branches: ['ci/run/**']`, 10 shards
2. `.github/scripts/run-unit-tests.sh` — uv + `scripts/run_tests.sh` with
   `HERMES_TEST_SLICE=I/10` (Python 3.14 + `--group dev` when the tree has
   `[dependency-groups]`, else 3.11 + `--extra dev`)
3. `scripts/github-test-ref` — overlay builder / waiter

Logs: `~/.local/state/hermes-github-ci/`.
