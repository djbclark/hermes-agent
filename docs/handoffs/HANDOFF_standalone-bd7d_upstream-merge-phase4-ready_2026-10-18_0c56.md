---
schema_version: 1
handoff_id: 0c56
parent_handoff_ids: [3d3a]
lineage: deterministic
chain: [standalone-bd7d]
repo: hermes-agent
workspace: upstream-merge
branch: chore/upstream-merge-2026-10
head_sha: e1811cf31b672e0055e51613931b8d07f8d84981
created_at: 2026-10-18T17:45:00-0400
writer: claude-code
---

# Handoff — Phase 4 READY (not promoted): upstream v0.22.2 merged in a pasture, gates green

## The Goal

Merge upstream tag `v0.22.2` (2026-10-15, first semver release after CalVer `v2026.9.24`) into the
fork, keeping every fork feature, in a cow pasture, and stop before promotion (playbook §6). Playbook:
`docs/UPSTREAM-MERGE-PLAYBOOK.md`. Decisions and test evidence:
`docs/UPSTREAM_MERGE_CONFLICT_MAP_2026-10-18_v0.22.2.md`.

## Where We Are

Pasture `~/.cow/pastures/hermes-agent/upstream-merge-2026-10`, branch `chore/upstream-merge-2026-10`,
pushed to `origin`. Live `~/.hermes/hermes-agent` untouched: `main` = `09750eb58f` (the rollback SHA),
gateway PID 20916 still running, live venv/config/state.db not touched. Pristine comparison pasture:
`~/.cow/pastures/hermes-agent/upstream-pristine-v0.22.2` (detached at `d923a5eb48`, `.venv` symlinked to
the merge pasture's 3.14 venv).

| Commit | What |
|---|---|
| `a9a4ed2a73` | Merge v0.22.2 (base `f97608f178` = v2026.9.24), 21 conflicted paths |
| `780f9cf9c6` | memory guard reads `[0]` of upstream's 4-tuple `_apply_batch_op` |
| `260adc20c4`, `35e95bf8b8` | test guard refuses mutating `launchctl` on non-`ai.hermes.*` hermes jobs; fork stop test mocks the new process scan |
| `123b0847cf` | retired `macos_only` mark → `platforms("macos")` |
| `08dda69363` | headless staged hint names `memory_pending.db`; write-approval/memory tests adapted to the journal |
| `5751eed1ff` | plain-`MemoryStore` inventory fallback (latent fork bug); resolution allowlist rows; MCP test stub |
| `e1ef4484e5` | no explicit `default_headers=None` from the keyless hook; e2e catalog/store tests know the fork |
| `d37fcf8faf` | `/aiuse` subprocess utf-8; provider tests know `opencode-free` / free-only policy |
| `e1811cf31b` | docs: conflict map, playbook, `docs/fork-feature-checklist.sh` |

Gates: (a) green after fixes; (b) 951 pass, 5 environmental (fail identically on pristine), 4 merge-caused
→ fixed; (c) local chunks + Hyades run 16 (merged vs pristine) — every merged-only failure traced to fork
code and fixed, remainder environmental; (d) SMOKE-OK on an isolated `HERMES_HOME`; (e) checklist
`missing=0`, `slack_native_slashes()` = 50 (at the cap).

## What We Tried (do not repeat)

- `cow-pasture scrub` on a pasture with copied venvs: forks `basename` per file over ~160k files (>12 min,
  adds load) and deleted `credentials.py` inside the copied venvs. Remove `.venv`/`venv` first; a
  Python fnmatch scrub over `git ls-files -o -i --exclude-standard -z` takes seconds.
- Gate (d) against the live home: a plain v0.22.2 launch migrates `config.yaml` 34 → 50 and would bump
  `state.db` 30 → 31 forward-only. Always a throwaway `HERMES_HOME` + `HERMES_DISABLE_LAZY_INSTALLS=1`.
- Passing merged-tree failure ids straight to pristine: fork-only params (`[opencode-free]`) make pytest
  abort with "not found" and run nothing; intersect with `--collect-only` first.
- Full local suite at `-n 4` on a starved box: m2 chunk took 60 min; the remaining 9 chunks were moved
  to Hyades (lead a4897a9d, djbclark's choice). Hyades jobs stop at the 75-min deadline (~94 %).

## Key Decisions (DECIDED, conservative)

1. Every fork feature kept; none retired (no upstream equivalent at v0.22.2). Upstream removed
   `opencode-free`; the fork keeps it and adapts upstream's "removed" test to assert it still resolves.
2. LSP: `ruff`, `marksman`, `taplo` added to upstream's `UNTRUSTED_SAFE_SERVERS` (static analysers,
   like yaml/bash LS). Remove them for upstream's stricter default — **operator call**.
3. Two `_spawn_typescript` definitions folded into one (upstream SDK pin when untrusted, fork TS 7
   `tsserver.js` injection when trusted).
4. Test guard `FORK(launchctl-guard)`: refuses mutating `launchctl` verbs only on hermes labels that are
   not upstream's `ai.hermes.*` (upstream tests mock those).
5. Gate (d) run isolated, not against "live config" as the playbook said; playbook updated.

## Evidence & Data

- Local logs: `~/.local/state/bigteam/adhoc-2026-10-18/hm-tests/` (gate_a/b, gc_*, gc_pristine,
  gc_merged_cand, fix*); CI logs: `.../hm-ci/job-669.log` (merged), `job-670.log` (pristine);
  run https://forgejo.mit.edu/djbclark/hermes-agent/actions/runs/16
- pm/promotion research: `~/.local/state/bigteam/adhoc-2026-10-18/hm-pm-findings.md`.

## Where We're Going

1. **Promotion needs a new procedure (playbook §6 is obsolete for v0.22.2).** The runtime moves to a
   pm-managed Python 3.14 env under `$HERMES_HOME/installs/<sha16>/…`; the first launch publishes
   `<checkout>/.hermes/bin/hermes` and rewrites `~/.local/bin/hermes` (used by
   `com.stayturgid.hermes-gateway` and `com.djbclark.hermes-dashboard`) unless `cli.expose_on_path:
   false`; `com.djbclark.hermes-memory-journal` runs `.venv/bin/python`; state.db 30 → 31 and config 34 → 50
   are forward-only, so `git reset` is no longer a rollback. Validate the candidate sequence in
   `hm-pm-findings.md` §4 on a scratch `HERMES_HOME` copy first; back up `state.db`, `config.yaml`,
   `~/.local/bin/hermes`.
2. Optional: one more Hyades merged-vs-pristine run at `e1811cf31b` to confirm the post-08dda69363 fixes
   suite-wide (they were verified locally per file).
3. Fix `check-gateway-fork-invariants.sh` per the conflict map's last section (pre-existing 2b bug).
4. Open: the memory guard does not journal upstream's new `patch` action; unpinned legacy remove records
   already in the live journal are refused on approve (by design upstream) — check the live journal
   before promoting; bundled-skills sync on first launch may overwrite fork-local skill edits.
