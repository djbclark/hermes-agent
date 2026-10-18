---
schema_version: 1
handoff_id: e4b7
parent_handoff_ids: [c91e]
lineage: deterministic
chain: [standalone-bd7d]
repo: hermes-agent
workspace: live-checkout
branch: main
head_sha: d1afc717c7
created_at: 2026-09-21T11:20:00-0400
writer: claude-code
---

# Handoff — post-merge cleanup: clone un-shallowed, workspaces removed, disk reclaimed

## The Goal

Close every loose end left by the v2026.9.14 merge (`3d3a`) and the memory-guard forward-port
(`c91e`): stale workspaces, the shallow clone that made merge-base empty, the lagging dev venv,
and the watchdog/playbook gaps.

## Where We Are

Done and live. Fork `main` = `d1afc717c7` (no code change in this handoff; docs only).

1. **Dev venv rebuilt.** `~/.hermes/hermes-agent/.venv` was mcp 1.28.1 without xdist/timeout;
   now `uv sync --frozen --all-extras --no-extra matrix` + `pytest-xdist pytest-timeout`
   (mcp 2.0.0). Cost ~14 MB of real disk (uv clones from its cache). 225 fork tests pass in it.
2. **Clone un-shallowed.** `git fetch --unshallow origin` then `upstream` cleared 19 of 20
   boundaries; the last (fork commit `9b56cd21c1`, parent present locally) was removed after
   `git rev-list --objects --missing=print --all` reported 0 missing and `git fsck` was clean.
   `git merge-base main upstream/main` now returns `345cd2b057` (v2026.9.14). `main` is 46 ahead /
   5040 behind the newest upstream. Lesson: while shallow, `git merge-base --is-ancestor` gave
   false "not merged" answers (e.g. `1dacb0cdbd`), so branch/pasture safety checks were only
   trustworthy after un-shallowing.
3. **Workspaces removed.** Worktrees `upstream-merge-2026-09` and `memory-guard-forward-port`
   (npm-noise `package-lock.json` change discarded), local branches `chore/upstream-merge-2026-09`,
   `chore/memory-guard-forward-port`, `fix/builtin-memory-capacity-guard`,
   `fix/truncation-combined-83714` (all in `main`), obsolete `.git/shallow*` files, and 564 MB of
   aborted-fetch `tmp_pack_*` debris (Aug 13).
4. **Cow pastures.** Ten stale hermes pastures (nine 2026-08-24 fork-feature clones plus
   `upstream-pristine`) removed with `cow-pasture remove` after checking each HEAD was reachable
   from a live-repo branch. The `aiuse` probe pasture belongs to another project and was left.
   Trial of the new workflow: a `--no-symlink` pasture of the live checkout has an independent
   `.venv`, `uv sync` inside it leaves the live venv untouched, tests import the pasture's code,
   and create+remove changed free space by ~0.06 GB (copy-on-write is effectively free).
5. **Watchdog** `check-gateway-fork-invariants.sh` (`~/.hermes` `a81c24f`) now also checks the MCP
   tolerant-list module/hook/tests and that `.git/shallow` has not reappeared.
6. **Docs.** `docs/UPSTREAM-MERGE-PLAYBOOK.md` rewritten for pastures, the un-shallow repair,
   the venv rebuild, the test-order quirk, cleanup and a disk section; skill pointer updated.

Disk: free space went 67.0 GB → ~71.2 GB after cleanup (pastures 3.6, worktrees 0.8). A later
~6 GB drop during the pasture trial was NOT the pasture (removal returned ~0.06 GB); cause not
identified (uv cache, TMPDIR and my scratch dirs were unchanged).

## What We Tried

- `git --shallow-file=/dev/null ...` to test history without the boundary: not a git option
  (errored silently into a `grep -c`, printing a false "0"). Use move-aside + `rev-list --missing`.
- Wrapper `cow-pasture create ... --no-symlink`: rejected (usage). Use bare `cow create` then
  `cow-pasture scrub`.
- `cow stats` "On disk" is du-style and misleading for savings; judge with `df`.

## Left for the operator (not done, by design)

- Seven local branches with commits not in `main` (some not on origin): `backup/pre-upstream-sync-20260811`,
  `fix/cron-provider-status-memory`, `fix/empty-tool-calls-guard`, `fix/memory-capacity-guard-v2`,
  `fix/opencode-zen-custom-fallback-headers`, `fix/opencode-zen-upstream-clean`, plus
  `feature/gateway-aiuse-command` (16 unique commits) and `feat/send-message-to-child`. Review before pruning.
- `hindsight-retention-pilot` plugin is not in `plugins.enabled` (never was); enabling it is a choice.
- graft 0.18.0 writes hook timeouts in ms (upstream trailhq/Graft#283); the four-second ask floor stays.
