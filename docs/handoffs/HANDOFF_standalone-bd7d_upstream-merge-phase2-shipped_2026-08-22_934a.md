---
schema_version: 1
handoff_id: 934a
parent_handoff_ids: [7895]
lineage: deterministic
chain: [standalone-bd7d]
repo: hermes-agent
workspace: upstream-merge
branch: main
head_sha: c4fc31970412d58acfd51a4f387276f09a77e022
created_at: 2026-08-22T21:15:00-0400
writer: claude-code
---

# Handoff — Phase 2 SHIPPED: upstream v2026.8.19 merged, deps upgraded, live gateway promoted

## The Goal

Direct continuation of `7895`. That handoff ended with Phase 1 recon
done and Phase 2 (the actual merge) deliberately deferred to a fresh
session. This session WAS that fresh session: it executed the graft +
merge against `v2026.8.19`, resolved all conflicts, verified with the
full test suite, then (new operator request mid-session) upgraded all
pinned dependencies, and promoted everything to the live checkout with
a gateway restart. **The task is COMPLETE and live.** This handoff
exists to record how, what judgment calls were made, and the small
follow-ups left.

## Where We Are

**Done, verified, live, pushed.** `main` = `c4fc319704` on both the
live checkout and `origin` (djbclark/hermes-agent). The gateway was
restarted at 21:01 EDT and runs the merged code + upgraded deps under
launchd (PID was 38744; telegram/signal/discord all connected;
end-to-end smoke `hermes -z` returned the expected literal through the
live ClinePass main loop on the NEW openai SDK).

Commit stack this session (worktree `chore/upstream-merge-2026-08`,
then ff'd into live `main`, branch also pushed to origin):

| Commit | What |
|---|---|
| `22fedc8a36` | Merge upstream `v2026.8.19` (base `c0106e50e7` via graft) — 8 conflicts resolved |
| `17fea82039` | Post-merge reconciliation (zen gate admits verified-keyless catalog; 2 upstream tests adapted to fork policy) |
| `97bc41b455` | chore(deps): all pins → newest aged releases; 3 pin mirrors synced; package-lock regenerated |
| `c4fc319704` | Merge of main's handoff-doc commits into the branch (so live could ff) |

Tree is clean except the intentionally-untracked
`UPSTREAM_MERGE_CONFLICT_MAP.md` still sitting in the worktree
(`~/.hermes/hermes-agent-worktrees/upstream-merge/`) — the worktree and
branch can now be deleted at leisure, everything is on main.

### The big discovery that unblocked the graft (7895's plan had a hidden snag)

`git replace --graft 03731cda97 c0106e50e7` did NOT take effect:
`git log` stopped at the root and `merge-base` stayed empty even though
`cat-file` showed the replacement. Root cause: **the repo is a SHALLOW
clone and `03731cda97` is a shallow boundary** (`.git/shallow`, 17
entries) — shallow boundaries override replace refs during traversal.
The fork root was never a parentless squash at all: its true parent is
`f7273568...`, simply absent locally. Fix: backed up `.git/shallow`,
removed only that one line, merged, then **restored the shallow file
and deleted the replace ref** — the shared `.git`
(`~/.hermes/hermes-agent/.git`, common dir for live + worktrees) is
byte-identical to before. Do NOT delete
`.git/shallow.bak-graft-20260822` blindly; it is the backup that lets
you confirm that. (Safe to delete once confirmed.)

The graft-then-merge itself worked exactly as 7895 predicted: with base
`c0106e50e7`, only **8 files** textually conflicted (vs the 30-file
risk surface): gateway/run.py, gateway/slash_commands.py,
hermes_cli/commands.py, hermes_cli/gateway.py,
hermes_cli/model_cost_guard.py, hermes_state.py, package-lock.json,
tests/gateway/test_status_command.py. Everything else — including the
two scariest files, telegram/adapter.py and memory_tool.py —
auto-merged cleanly, and all fork features were verified present
afterwards (clipboard button, full_width picker in all 3 adapters,
memory capacity guard, MoA cooldown, discover_models fix, Zen
x-api-key, ClinePass no-spend gate, truncation-marker guards,
/clinepass + /aiuse).

### Test verdict (the full suite, ~36,900 tests)

Zero merge-caused failures and zero dep-upgrade-caused failures
survive. Methodology that made this tractable: raw xdist runs produce
large flaky failure counts (e.g. 117 raw in gateway+hermes_cli); rerun
the failed set in isolation, then rerun the SAME set on a pristine
`v2026.8.19` worktree with the same venv, and diff. Consistent
environmental failures on this macOS box (identical on pristine
upstream): ~37 total — voice/sounddevice, PulseAudio Linux-isms,
/tmp-symlink rm-guard, install-sh node tests, FTS5 search, GUI toolset.
Real regressions found and fixed this way: exactly ONE from the merge
(zen keyless vs policy gate) and ONE knock-on family from the dep bump
(pin-mirror tests).

## What We Tried

Chronological failures — each cost real time, don't repeat:

1. **`git replace --graft` silently ignored** — see shallow discovery
   above. Symptom pattern to recognize: `cat-file -p` shows the
   replacement parent but `log`/`merge-base` don't traverse it →
   check `.git/shallow` FIRST, not `core.usereplacerefs`/commit-graph
   (both were dead ends I checked before finding it).
2. **Combined 8-file pytest run looked hung** (16 min CPU, empty
   output, main thread in a cond-wait). It wasn't hung — pytest -q
   buffers into the pipe and tests/tools is just huge (7,547 tests). I
   killed it at 62%. Lesson: for this repo use `-n 8
   --timeout 180 --timeout-method=thread` and expect tests/tools alone
   to take ~27 min.
3. **Two background Bash runs were killed instantly** by something
   outside the session (operator TaskStop or harness policy — never
   identified). Foreground chunked runs worked throughout. If
   background pytest dies immediately again, just run foreground.
4. **Naive Slack-cap count was wrong** (117 "native") — the real
   arithmetic is `slack_native_slashes()`, which lands at exactly
   50/50 after the merged union of `_SLACK_VIA_HERMES_ONLY`.
5. **`hermes oneshot` is not a command** — the one-shot smoke tester
   is `hermes -z "<prompt>"`.
6. **First dep-bump attempt used absolute-latest versions** and
   immediately tripped the repo's `tool.uv exclude-newer = "14 days"`
   supply-chain aging gate (openai 3.3.1 is 3 days old). Redone
   correctly: bump to newest release ≥14 days old (PyPI upload dates),
   never downgrade. openai lands on 2.53.0, anthropic on 0.121.0 — the
   scary majors (openai 3.x, anthropic 1.0) are deliberately still
   ahead, age-gated, and will arrive in a later sweep.

## Key Decisions

All three merge-policy questions were put to the operator and answered
(AskUserQuestion, recommended options taken):

- **Target `v2026.8.19` tag**, not `upstream/main` tip. REJECTED: tip —
  one extra day of untagged churn for no benefit.
- **`gateway/run.py` hybrid**: upstream's restructure + fork's
  `should_restart_after_signal()` launchd clean-exit guard re-inserted
  (the patched block survived verbatim upstream; upstream only added an
  exit-75 `_restart_via_service` path after it, kept). REJECTED:
  dropping the launchd guard — reintroduces the 2026-08-08 RCA crash
  loop.
- **Keep both sides** of model_cost_guard.py (fork `is_free_model`
  no-spend policy + upstream pricing-trust helpers — different call
  sites) and runtime_provider.py (fork Zen `x-api-key` patch + fork
  policy gate + upstream keyless `opencode-free` routing — upstream
  does NOT subsume the keyed-Zen header fix).

Judgment calls made autonomously (flagged, not operator-ratified):

- **/status route selection**: fork (latest route) and upstream
  (dominant route, #87227) fixed the same stale-metadata bug two ways.
  Composed: **latest coherent main-loop tuple wins between turns
  (recency), dominant route is fallback, franken-tuples never.**
  `get_latest_session_model_usage` gained upstream's coherence filters
  (`task=''`, `model<>'unknown'`, `billing_provider<>''`) + `rowid
  DESC` tiebreak; upstream's test renamed/adapted to recency
  expectations. Rationale: dominance shows the OLD provider right after
  a deliberate switch — regressing the fork's original bug.
- **Zen free-only gate now admits upstream's VERIFIED keyless catalog**
  (`opencode_zen_free_runtime(...) is not None` short-circuits the
  models.dev pricing check) — keyless models cannot spend; without this
  upstream's `x-preview-f-free` was blocked (the one genuine merge
  regression found by the suite).
- **Two upstream tests adapted to fork policy** rather than reverting
  fork behavior: launchd plist wrapper test (no `--replace` — fork has
  its own pinning test + RCA), and paid-Zen-without-key test (policy
  ValueError fires before the AuthError path; still fail-closed).
- **Dep holdbacks** (each commented in pyproject): websockets 15.0.1
  ([feishu] lark-oapi caps <16); opentelemetry 1.39.1 (mistralai caps
  semconv <0.61); numpy 2.4.6 (2.5.x drops py3.11; requires-python is
  >=3.11); agent-client-protocol 0.9.0 (0.10+ removed
  `acp.schema.SessionModelState`, which tests/acp imports).

## Evidence & Data

- Merge: 8 textual conflicts / 30-file risk surface; 49-file fork root
  delta vs `c0106e50e7` confirmed pre-graft (`6693+/199-`).
- Slack: `slack_native_slashes()` == 50, cap 50;
  `_SLACK_VIA_HERMES_ONLY` union = old 11 + whoami, platform (upstream)
  + aiuse, clinepass (fork).
- Suite: tests/tools 7,410 passed/73 raw-failed → 14 consistent, all
  identical on pristine; gateway+hermes_cli 12,508 passed/117 raw → 20
  isolated vs 19 pristine (delta = the zen keyless test, fixed);
  agent+run_agent+hermes_state 7,001 passed → isolated failures all
  pass; chunk C 9,513 passed → 4 consistent, identical on pristine.
- Deps: 88 exact pins, 47 bumped, 4 held back; 38 lazy_deps pins
  synced; 4 google-workspace setup.py pins synced; `uv sync
  --all-extras --no-extra matrix` (python-olm needs cmake/libolm,
  build fails locally — matrix extra untested in the worktree venv, but
  live venv's lazy refresh handled its own backends: 7 refreshed, 10
  current).
- Live venv sync: `uv pip install -e '.[all]' --python venv/bin/python`
  then `_refresh_active_lazy_features` (17 active features).
- package-lock.json regenerated (`npm install --package-lock-only
  --ignore-scripts`, node v26.7.0) — now pins electron 40.10.6
  matching package.json (fork's bump survived the merge; upstream lock
  had 40.10.2).
- Old-chain WATCH items resolved in passing: litellm proxy serves the 3
  clinepass models; `hermes moa list` "Active in config: (off)" is
  CORRECT (active_preset is only set by `/moa on`; main provider is
  `cline`, MoA is on-demand; `agent.provider == "moa"` is the runtime
  activation condition — `moa.enabled`/`fanout` keys in config.yaml are
  not consulted for auto-fanout).

## Operator Feedback

- Answered the three merge-policy questions (all recommended options).
- Mid-session: *"We should also upgrade all of the deps, we have no
  reason I know of to keep old versions."* — done, within the existing
  14-day aging gate (the gate itself is a reason to not take <14-day-old
  versions; operator did not ask to remove the gate, so it stands).
- Two background test runs were externally stopped; if that was the
  operator signaling "don't run these in background", the lesson is
  taken (foreground chunks from now on).

## Where We're Going

### 1. THE NEXT ACTION — operator eyeballs the live gateway surfaces

Tap `/clinepass` and `/status` on Telegram: confirms the full-width
picker still renders post-merge AND exercises the new /status
recency-composed route display in one go. Everything else below is
optional/cleanup.

### 2. Cleanup (any session, low risk)

- Delete the worktree + branch:
  `git worktree remove ~/.hermes/hermes-agent-worktrees/upstream-merge`
  (the untracked UPSTREAM_MERGE_CONFLICT_MAP.md dies with it — its
  content is superseded by this handoff) and
  `git branch -d chore/upstream-merge-2026-08` (already pushed).
- Confirm shared `.git/shallow` matches
  `.git/shallow.bak-graft-20260822`, then delete the backup.
- Optional: `hermes gateway install` to regenerate the launchd plist —
  the installed `com.stayturgid.hermes-gateway.plist` still uses the
  pre-merge invocation (`hermes gateway run --replace`, no
  stderr-timestamp wrapper). Works fine as-is; regenerating applies the
  fork's no-takeover args + upstream's wrapper/--external-supervisor.

### 3. Deferred by the aging gate (future dep sweep)

Around 2026-09-02 the majors age in: openai 3.3.1, anthropic 1.0.0.
Expect API work for openai 3.x. Also parked: ACP 0.12 (port off
`SessionModelState`), websockets 16+ (needs lark-oapi to lift its cap),
numpy 2.5 (needs requires-python >=3.12).

### 4. Future upstream merges are now cheap

The merge commit `22fedc8a36` gives the fork REAL shared ancestry with
upstream. Next time: `git fetch upstream && git merge <next-tag>` —
no graft, no shallow surgery, ordinary 3-way merge.

### Carried from the separate `standalone-0b6d` chain (unrelated)

- Operator: cancel the DeepSeek account dashboard-side; then delete
  `~/.hermes/.env.bak-deepseek-retire-20260822` and
  `~/.hermes/config.yaml.bak-predeepseekretire-20260822`.
- After soak: open DPRs from
  `~/src/litellm-reviews/2026-08-22-clinepass/DPR_DESCRIPTION.md`.

## Quick Start

```bash
# --- Confirm live state ---------------------------------------------
cd ~/.hermes/hermes-agent && git log --oneline -4   # expect c4fc319704 at top
export PATH="$HOME/.local/bin:$PATH"
hermes gateway status                                # telegram/signal/discord connected
hermes -z "Reply with exactly: SMOKE-OK"             # end-to-end main loop

# --- Regression gate (all passed 2026-08-22 on the shipped tree) -----
cd ~/.hermes/hermes-agent-worktrees/upstream-merge   # or live checkout after cleanup
.venv/bin/pytest tests/hermes_cli/test_model_validation.py tests/hermes_cli/test_models.py \
  tests/gateway/test_telegram_choice_picker_rows.py tests/hermes_cli/test_clinepass_command.py \
  tests/gateway/test_clinepass_command.py tests/gateway/test_choice_picker.py \
  tests/hermes_cli/test_commands.py tests/gateway/test_status_command.py \
  tests/hermes_cli/test_opencode_zen_free_keyless.py tests/hermes_cli/test_opencode_zen_free_policy.py -q
# 235 passed

# --- Full-suite recipe for this repo (foreground, chunked) ------------
# uv sync --all-extras --no-extra matrix && uv pip install pytest-xdist pytest-timeout
# .venv/bin/pytest <chunk> -q -n 8 --timeout 180 --timeout-method=thread
# chunks: tests/tools (~27min) | tests/gateway tests/hermes_cli (~5min)
#         | tests/agent tests/run_agent tests/hermes_state (~3min) | rest (~3min)
# Triage raw failures by re-running them isolated, then diffing against
# a pristine worktree: git worktree add /tmp/upstream-pristine <ref>

# --- Gotchas carried forward (6848/7895, still true) -------------------
# Never bare `git push` in ~/.hermes/hermes-agent — always `git push origin main`.
# Don't trust `git status --branch` ahead/behind here.
# `hermes update` only syncs origin, never upstream.
# Worktrees live at ~/.hermes/hermes-agent-worktrees/<task>/.
```
