---
schema_version: 1
handoff_id: 59b0
parent_handoff_ids: [42ff]
lineage: deterministic
chain: [standalone-f1f4]
repo: hermes-agent
workspace: main (live checkout; the merge pastures are gone)
branch: main
head_sha: 7d9775dd79
created_at: 2026-10-19T11:40:00-04:00
writer: claude-code
---
# Handoff — v0.22.2 post-promotion queue (a-i) worked to the end

Parent 42ff is in site-private (`memory/handoffs/hermes-agent/HANDOFF_standalone-f1f4_v0222-merge-gate-c_2026-10-18_42ff.md`);
it was written there because a commit on live `main` would have blocked the promotion fast-forward.
That constraint is gone, so this one lives in the repo.

## The Goal

Work the post-promotion queue a-i of parent 42ff item 00 (all approved by djbclark on 2026-10-18),
after v0.22.2 went live at 20:27 that day, and act on the one-offs relay about the merge-gate CI route.

## Where We Are

All nine items are closed out. Live `main` = `7d9775dd79` (clean, pushed). `~/.hermes` master =
`0ea5f6c` (pushed). Gateway healthy throughout (pid 942, `com.stayturgid.hermes-gateway`); nothing
was restarted.

1. **a. Desktop rebuild — was already done, now confirmed.** `source_completion.py --finish-update
   --desktop` through `bg` (rc=0) printed "Desktop app is up to date": the packaged app
   `apps/desktop/release/mac-arm64/Hermes.app` carries `install-stamp.json` commit
   `a6b187d8a65a...`, built 2026-10-18 20:25 — i.e. *inside* the promotion window, because the
   first launch's "interrupted source update" builds `apps/desktop` whenever a packaged app exists
   (`hermes_cli/source_build.py:87`). `/Applications/HermesDesktop.app` (2026-06-18) and
   `/Applications/Hermes.app` (a June "Hermes-Setup" stub) are NOT Hermes-managed: no install stamp,
   and `~/.hermes/desktop-installed-apps.json` does not exist, so `_refresh_installed_desktop_apps`
   owns nothing. Operator question if they care: should the installed app be adopted?
   **Side effect found and fixed:** that rebuild re-installed the lefthook `pre-commit` hook the
   promotion had removed (`.git/hooks/pre-commit` + `.git/info/lefthook.checksum`, both 11:04; the
   next commit in the live checkout ran lefthook). Both removed again; playbook §6's after-soak
   subsection now warns about it (`01a91eb684`). The fork-invariant script does not check for the
   hook — worth adding if it keeps coming back.
2. **b. Old-venv consumers — 2 of 7 moved; the other 5 cannot, with evidence.** `~/.hermes` commit
   `f92bb42`. `--run-module` takes a *dotted module only* (`runpy.run_module`; no path, no `-c`, no
   stdin), so only `memory-journal-consumer.sh` (module `cron.scripts.memory_journal_consumer`) and
   `ai-frontier-weekly-research.sh` (a plain CLI call) could move. Still on `venv/bin/python`:
   `memory-metrics-report.sh`, `memory-size-watchdog.sh`, `tcc-audit-watchdog.sh`,
   `gmail-index-sync.sh`, `provider-health-watchdog.sh`, each because it runs a file by path or a
   heredoc. Plus the xai-oauth-bridge (`~/ops/site-djbclark/roles/litellm/defaults/main.yml`
   `litellm_xai_bridge_python`): its script imports `hermes_cli.auth`, and the pm env path carries a
   per-sync uuid, so there is no stable interpreter to point it at. `venv/` stays. Full reasoning is
   in the slice report (Evidence 3) and now in playbook §7 item 3.
3. **c. `hermes pm gc` ran twice (rc 0) and freed nothing both times**, before and after the
   pastures went: `gc: removed 0, kept 9; removed 0 dependency generations, 0 PM runtime
   generations, 0 install state dir(s) of deleted checkouts`. `~/.hermes/installs` is still 5.1 GiB
   over 6 keys: `bde621504fe3e916` = live, `6246458f9c575320` = `~/src/hermes-worktrees/fix-config-bigint`
   (in use), and `6d80ff011ff73b5f` (the removed merge pasture), `38818021ae5bd156`,
   `68ac85a5e590e935`, `c7de12bddf5f0746` whose checkouts no longer exist (2.8 GiB). gc has no
   `--dry-run`; keys are `sha256(realpath(checkout))[:16]` (`pm/environments.py` `install_key`),
   which is how the mapping above was computed read-only. **Open:** either
   `collect_orphan_install_states` judges orphanhood by something else, or this is an upstream bug —
   it was not investigated, and nothing was deleted by hand (operator's call).
4. **d. Skill curator: no fix needed in code; the 400 is stale.** `~/.hermes/skills/.curator_state`
   still shows `last_run_at: 2026-10-13` with `llm: Model 'big-pickle' isn't available on custom …
   invalid model format. Expected format: modelType/model`. Live config is now `provider: xai-oauth` /
   `default: grok-4.7`, and stayturgid's `stayturgid_hermes_model` default is `grok-4.7` as well
   (`roles/control_node/defaults/main.yml:104`), so the curator has not run since the model changed.
   Next scheduled run is the test; no curator run was triggered (it costs an LLM call and prunes
   skills).
5. **e. Memory `patch` watch shipped + invariants tightened.** `~/.hermes` commit `01de18c`:
   `scripts/memory-patch-watch.sh` (pure sqlite, read-only `file:…?mode=ro`, de-duplicates the
   replayed assistant rows — a naive count is ~5x high) alerts only on (a) a growth patch that left a
   store at >= 85% (`MEMORY_QUEUE_PCT`) or (b) >= 20 patch ops in 7 days; thresholds overridable by
   env. Registered as Hermes cron job `f335bd14471f` "Memory patch watch", `0 9 * * 1`, `--no-agent`,
   next run 2026-10-26 09:00. Live data: **zero `patch` calls so far** (the action only exists since
   the promotion; `replace` baseline was 2-7 ops/week, with 17/53/12 in the July-August hygiene
   sweeps). Invariant script: check 6 now asserts the whole shim line
   (`exec <checkout>/.hermes/bin/hermes "$@"`), new check 7 greps `FORK(launchctl-guard)`, exactly one
   `def _spawn_typescript(`, `FORK(opencode-zen-keyless)`. Silent, rc 0, with and without
   `SKIP_LAUNCHCTL=1`.
6. **f. Playbook §6/§7 rewritten** (`7d9775dd79`, pushed, live fast-forwarded): driver-based recipe
   (prewarm → `promote-run.sh` through `bg` → go/no-go → auto-rollback), the three lessons the two
   rolled-back attempts paid for, the real timing table, the verified rollback, and §7's
   post-promotion list. The pre-v0.22.2 block was dropped (history: `a6b187d8a6`). The same commit
   fixes the conflict map's dangling "§6 rewrite note" sentence.
7. **g. Both merge pastures removed** (`cow remove hermes-agent/upstream-merge-2026-10 --force` and
   `…/upstream-pristine-v0.22.2 --force`): ~10 GiB. Checked first that the pasture HEAD
   `a6b187d8a6` is an ancestor of live `main` and that the only dirt was an untracked
   `apps/desktop/release-new/`. The temporary docs worktree `playbook-67` and its branch are gone too.
   **Deliberately NOT deleted** (deviation from 42ff item g): `~/.local/state/bigteam/adhoc-2026-10-18/hermes-agent-forgejo-mirror`
   (877 MiB). Its `ha/base` (`d47059b290`, the Hyades CI base) exists nowhere else — `ci/base`
   `92d25d0ccd` is already on GitHub — and the one-offs relay says "Forgejo stays usable until
   djbclark confirms its private repos can go". Delete it once he confirms.
8. **h. The two synced bundled skills committed** (`0ea5f6c`): youtube-content and
   hermes-agent-skill-authoring took upstream-only improvements (ruamel.yaml, pm-prepared
   interpreter, typing); nothing fork-specific was overwritten. Note `hermes skills list-modified`
   reports a *different* pair as user-modified (`hermes-agent`, `opencode`) — those are untouched.
9. **i. `/mcp` reconnect** not needed in this session (the `hermes` MCP tools answered).

## What We Tried

1. Mapping install keys to checkouts by hand (sha256 of the realpath) to predict what `pm gc` would
   remove — correct as a mapping, useless as a prediction: gc removed none of them.
2. `rg -rln PATTERN PATH` to search the ops tree: `-r` is rg's `--replace`, not grep's recursive, so
   the "hits" were lines with the pattern substituted. It sent this session looking for a var called
   `litellm_ln_python` that does not exist (it is `litellm_xai_bridge_python`). Rule added to
   `~/CLAUDE.md` item 1.
3. Verifying the migrated scripts by running them: refused on purpose — each sends Telegram/Discord
   traffic, spends an LLM call, or mutates the live journal/state.db. The slice used read-only probes
   (`hermes --run-module pydoc <module>`) instead; the first real run of each is still unobserved.

## Key Decisions

1. Three bigteam slices (claude Sonnet 5.5, `--effort high`, `--kind code`, $0.47 + $0.95 + $1.24)
   rather than inline work: context was already the binding constraint. Rejected: agy's pool (not in
   `route_agent.py plan`'s output for `code` today), gemini-flash (too weak for an unattended-script
   slice).
2. The `venv` migration stops where `--run-module` stops, instead of inventing wrapper modules inside
   the checkout for five scripts. Rejected: a `scripts/` package in the fork (more fork surface to
   carry through every upstream merge) — written up as the option in playbook §7 item 3 for when
   djbclark wants it.
3. Keep the Forgejo mirror (item 7 above) — the relay's "Forgejo stays usable" outranks 42ff's
   delete instruction, which predates it.
4. No hand-deletion of the three orphan `installs/<key>` dirs: gc declining to remove them is
   unexplained, and 2.8 GiB is not worth a guess at live runtime state.
5. The patch watch alerts on stdout and exits 0 (the `memory-size-watchdog.sh` convention), not 1.

## Evidence & Data

1. Slice reports (read in full, diffs reviewed line by line before each commit):
   `~/.local/state/bigteam/hermes-post-promo-2026-10-19/{venv-migrate,patch-watch,playbook-67}-report.md`;
   briefs and acp-run logs alongside; `CLAIM` closed with `DONE`.
2. Desktop: `apps/desktop/release/mac-arm64/Hermes.app/Contents/Resources/install-stamp.json`
   (commit `a6b187d8a6`, mtime 2026-10-18 20:25); run log
   `/private/tmp/claude-501/-Users-djbclark-src-hermes-worktrees/9fa67eec-*/scratchpad/desktop-rebuild.{log,rc}`.
3. `pm gc` output: `/tmp/gc2.log` (and the first run's in the same scratchpad as `pm-gc.log`).
4. Cron: `hermes cron create` printed `Created job: f335bd14471f`, next run 2026-10-26T09:00.
5. Tests: no test suite was run this session (docs, shell scripts and cron registration only). The
   invariant script and the new watch script were executed read-only against live state, both silent.

## Operator Feedback

1. The queue a-i itself was djbclark's, approved 2026-10-18 (parent 42ff item 00), including the
   single-run `BG_ALLOW_LOCAL_CI_ONLY=1` for the desktop build in this `ci`-mode repo.
2. Standing rules that shaped the work: never the live plist for xai-oauth-bridge (Ansible only);
   builds through `~/ops/site-private/bin/bg`; commit by path in a shared dirty tree.

## Where We're Going

1. **Next action, and it needs djbclark, not code:** three questions, each one line of evidence
   above — (a) delete `hermes-agent-forgejo-mirror` and with it `ha/base`? (b) remove the three
   orphan `installs/<key>` dirs (2.8 GiB) by hand, or investigate why `pm gc` keeps them? (c) accept
   the promotion so the two backups `~/.local/state/hermes-promote/v0.22.2-20261018-{1921,2019}` can
   go (they hold the only restorable 3.11 `venv/` and the pre-promotion `state.db`)?
2. Optional, after that: `hermes sessions optimize-storage` (~0.9 GiB of the 1.6 GiB `state.db`) —
   foreground, with the gateway/dashboard/journal stopped (it refuses while the store is held unless
   `--force`), back up `state.db` first. Recipe in playbook §7 item 6.
3. Watch the next skill-curator run (`~/.hermes/skills/.curator_state` `last_run_summary`): if it
   still says `big-pickle`, the curator is not reading the config default and that is a code fix.
4. Observed by a slice, not ours: Hermes cron job `39d3abfde579` "Graft Wiring Invariants" has
   `failure_streak: 34` in `~/.hermes/cron/jobs.json`. Also `8486ce778b40` "Provider Health
   Watchdog" is `enabled: false` — check `~/ops/site-private/memory/` before "repairing" it.
5. `~/.hermes` master still carries ~140 uncommitted paths under `skills/` plus `config.yaml` and the
   deleted `hindsight-*` scripts — the curator's and other sessions' state, deliberately untouched
   here. Someone who owns that repo should decide; do not `git add -A` there.
6. Next upstream merge: the playbook is now current, and gate (c) is
   `git fetch origin ci/base && git checkout origin/ci/base -- scripts/github-test-ref && scripts/github-test-ref <merged-ref> <pristine-ref>`
   (10 shards, ~9 min; fork main baseline 2026-10-19: 8m49s, 49726 passed / 20 failed, 17 of them
   failing on main itself and 3 CI self-tests).

## Detached jobs

none. The three slice records
(`~/.local/state/bigteam/hermes-post-promo-2026-10-19/jobs/{venv-migrate,patch-watch,playbook-67}.json`)
are finished, read, acted on and closed; so are the three inherited from 42ff
(`~/.local/state/bigteam/adhoc-2026-10-18/jobs/{hermes-merge-v0.22.2,hm-promo-rehearsal,grok-unreserve}.json`).
Nothing of this session's work depends on a `run_in_background` task surviving it.

## Quick Start

```bash
git -C ~/.hermes/hermes-agent log --oneline -3 && git -C ~/.hermes/hermes-agent status -s
git -C ~/.hermes log --oneline -3
launchctl list | rg hermes            # gateway/dashboard/bridge up, memory-journal last exit 0
bash ~/.hermes/scripts/check-gateway-fork-invariants.sh && echo invariants-ok
PATCH_WATCH_VERBOSE=1 bash ~/.hermes/scripts/memory-patch-watch.sh   # silent = healthy
dust -d 1 -n 8 ~/.hermes/installs     # 5.1 GiB; see Where We Are item 3
gsed -n '255,300p' ~/.hermes/hermes-agent/docs/UPSTREAM-MERGE-PLAYBOOK.md   # the new §6
```
