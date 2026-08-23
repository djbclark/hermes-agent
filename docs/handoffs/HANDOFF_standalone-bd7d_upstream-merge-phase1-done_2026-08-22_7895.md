---
schema_version: 1
handoff_id: 7895
parent_handoff_ids: [6848]
lineage: deterministic
chain: [standalone-bd7d]
repo: hermes-agent
workspace: upstream-merge
branch: chore/upstream-merge-2026-08
head_sha: ae230f072e20c75f637a7b83336e8895336606f5
created_at: 2026-08-22T18:15:00-0400
writer: claude-code
---

# Handoff — upstream-merge Phase 1 done, Phase 2 (the actual merge) deliberately deferred to a fresh session

## The Goal

Direct continuation of handoff `6848`. That handoff closed with Phase 1
(read-only conflict-surface reconnaissance) running in the background,
not yet returned. This handoff exists because Phase 1 has now completed
with a result that changes the plan, and because Phase 2 (the actual
merge — real conflict resolution, including at least one genuine
design-intent collision) is judgment-heavy enough that it deserves a
fresh context window rather than continuing in this already-large
session. Read `6848` first for full background (the ClinePass fix, why
the "20961 behind" stat was garbage, why this is a separate worktree
task) — it is not repeated here except where it changed.

## Where We Are

**Phase 1 complete. Phase 2 not started — this is a deliberate stop,
not a blocker.** Operator said "proceed" to Phase 2 but also asked
whether to handoff+baton first; the answer given (and acted on) was yes,
given the stakes below.

### Git state — unchanged from 6848 except the handoff commits themselves

| Location | Branch | HEAD | Notes |
|---|---|---|---|
| `~/.hermes/hermes-agent` (LIVE) | `main` | `f8697f5598` | Only new commit since `6848` is the handoff-doc commit itself (`f8697f5598`, docs-only). No code changes. Gateway still running the same code as `6848` (`ae230f072e`). |
| `~/.hermes/hermes-agent-worktrees/upstream-merge` | `chore/upstream-merge-2026-08` | `ae230f072e` | Zero new commits. One untracked file: `UPSTREAM_MERGE_CONFLICT_MAP.md` (461 lines, Phase 1's output — deliberately left uncommitted, it's a working artifact not a code change). |

No tests were run this session-fragment (Phase 1 was read-only recon by
a subagent; nothing to test yet). Explicitly noting "none run" per the
validation gate rather than omitting it.

### Phase 1 result — corrects `6848`'s stated merge base

`6848` said the fork's content was closest to upstream tag `v2026.8.13`
and treated that as the practical merge base. **Correction**: `v2026.8.13`
is the closest *tag*, but not the closest *commit*. The Phase 1 agent
found the true nearest common point is upstream commit **`c0106e50e7`**
(2026-08-10, "fix(kimi): send Hermes attribution headers") — only **49
files** different from the fork's squashed root, versus 801 for the tag,
and it IS an ancestor of both `v2026.8.13` and `upstream/main`. So a real
3-way merge is achievable (graft the fork's root onto `c0106e50e7`) even
though a direct `git merge-base main upstream/main` still returns nothing
(the fork's squashed root isn't literally connected in the stored graph —
grafting fixes that for merge purposes).

With the corrected base, the real conflict surface is much smaller than
`6848` feared:
- Upstream changed 3,045 files since `c0106e50e7`.
- The fork changed only **60** files since the same point (the fork's
  entire history is 6 commits off a squashed root — cleanly separable
  from upstream drift once the right base is used).
- The true intersection — files both sides touched, i.e. real conflict
  risk — is **30 files**. (Using the wrong tag-based comparison from
  `6848` would have shown 383 "conflicting" files; ~350 of those were
  phantom, an artifact of measuring from the wrong point.)
- 30 more files are fork-only (upstream never touched them) — safe to
  keep verbatim, no merge needed there.

Full detail, including per-file annotations, is in
`UPSTREAM_MERGE_CONFLICT_MAP.md` in the worktree (untracked — read it
directly, do not rely on this summary for the full file list).

### The 8 files flagged as needing real judgment (not mechanical merge)

In descending risk order:

1. **`plugins/platforms/telegram/adapter.py`** — carries two fork
   features (a clipboard-copy button, and today's `full_width` picker
   flag from `31ecf3d50b`) against 26 upstream commits touching the same
   file (860+/142- fork side vs 203+/6- upstream side).
2. **`tools/memory_tool.py`** — fork adds a 634-line capacity-guard
   subsystem; upstream touched the same entry points.
3. **`gateway/slash_commands.py`** — ClinePass command handlers +
   `full_width=True` call site vs 19 upstream commits.
4. **`gateway/run.py`** — **genuine intent collision, not just diverged
   edits.** Upstream made systemd the sole restart owner across 102
   commits. Part of this fork's own reason for existing is preventing
   non-zero exits under launchd (a different supervision model). These
   two sides disagree about who owns restart responsibility — this needs
   an actual design decision from the operator, not a diff resolution.
5. **`agent/moa_loop.py`** — advisor cooldown logic, comparable size
   both sides, smaller file.
6. **`hermes_cli/models.py`** — today's `discover_models` fix (`6848`,
   `ae230f072e`) vs 40 upstream commits touching the model catalog in the
   same file.
7. **`hermes_cli/runtime_provider.py`** — upstream added its own
   OpenCode provider handling that may already subsume the fork's Zen
   `x-api-key` patch — needs a read of both before assuming either side
   wins outright.
8. **`hermes_cli/model_cost_guard.py`** — fork and upstream **independently
   solved the same problem** ("don't trust a foreign catalog's pricing")
   differently. This is the other clear needs-a-decision file, alongside
   `gateway/run.py`.

One piece of good news: Discord/Matrix choice-picker risk is *lower*
than `6848` feared — upstream reworked `ChoicePickerView` internals but
left its public signature intact, so the fork's `full_width` kwarg
should survive a straight parity check without a redesign.

## What We Tried

No new failed approaches this session-fragment — Phase 1 was a single
successful background agent run. (See `6848` for the session's actual
failed-approach list: the wrong worktree-convention guess, the wrong
test-patch target, the "20961 behind" misdiagnosis, the Tier 1 list-vs-
string schema error. All still relevant background, none repeated here.)

## Key Decisions

- **Defer Phase 2 to a fresh session instead of continuing here.**
  CHOSEN, this handoff's reason for existing: Phase 2 involves at least
  two files (`gateway/run.py`, `hermes_cli/model_cost_guard.py`) that
  need real design judgment, not mechanical conflict resolution, and
  this session is already carrying a large amount of unrelated context
  (today's earlier ClinePass investigation, the ops-suite `/baton` resume,
  etc.). REJECTED: continuing Phase 2 inline — technically possible, but
  worse odds of catching a subtle intent-collision mistake with a
  cluttered context window doing the reviewing.
- **Use `c0106e50e7` as the real merge base, not `v2026.8.13`, not a
  plain `git merge upstream/main` against the unrelated-history root.**
  The corrected base cuts real conflict-review work from 383 files to 30
  — worth the extra step of grafting the root before merging.
- **Leave `UPSTREAM_MERGE_CONFLICT_MAP.md` uncommitted in the worktree.**
  It's a working artifact for the next session to read, not a code
  change — no reason to commit it to the eventual merge branch's history.

## Evidence & Data

- Phase 1 agent stats: 28 tool uses, ~75K tokens, ~6 minutes wall time.
- File counts (base `c0106e50e7`): upstream changed 3,045 files; fork
  changed 60 files; intersection (real conflict risk) 30 files;
  fork-only (no upstream touch, safe to keep) 30 files.
- File counts (base `v2026.8.13`, superseded/wrong): upstream 2,653;
  fork 809; intersection 383 (mostly phantom — discard this comparison).
- `c0106e50e7` diff from fork's squashed root: 49 files (vs. 801 files
  for `v2026.8.13`) — this is the number that identified it as the true
  nearest point.
- 8 flagged files listed above with rough line-diff magnitudes and
  upstream commit-touch counts, per the Phase 1 report.
- Report location: `~/.hermes/hermes-agent-worktrees/upstream-merge/
  UPSTREAM_MERGE_CONFLICT_MAP.md` (461 lines, untracked).

## Operator Feedback

- *"proceed, but should we do another handoff and baton first?"* —
  read as: proceed with the task overall, but sequence a handoff+fresh-
  session before the risky part (Phase 2) rather than doing it in an
  already-loaded session. Acted on directly: this handoff is that
  handoff. No pushback yet on the recommendation to defer Phase 2 — if
  the operator actually wanted Phase 2 attempted immediately in-session,
  that reading was wrong and should be corrected next session.

## Where We're Going

### 1. THE NEXT ACTION — start a fresh session, `/baton` into this chain, begin Phase 2

A fresh Claude Code session run from anywhere should be able to
`/baton`/`/resume` into chain `standalone-bd7d` (it's the only or most
recent chain touching `hermes-agent`/`upstream-merge` — if multiple
chains exist by then, this one's `updated_at` will be the most recent
for that task). Model/effort recommendation from `6848` still stands:
**Claude Opus 5, high reasoning effort**, bumped to **xhigh specifically
for `gateway/run.py` and `hermes_cli/model_cost_guard.py`** — those two
need a real design decision, not just careful reading.

### 2. Read the full conflict map before touching anything

`cat ~/.hermes/hermes-agent-worktrees/upstream-merge/UPSTREAM_MERGE_CONFLICT_MAP.md`
— this handoff's summary is not a substitute for the full per-file
detail in that report.

### 3. Graft and merge

In the worktree: graft the fork's squashed root onto `c0106e50e7` (so
git sees real ancestry — `git replace --graft` or an equivalent rebase
that preserves the fork's 6 commits on top of `c0106e50e7`'s tree), then
`git merge upstream/main` from there. Resolve the 30 real-conflict files
guided by the report, starting with the two decision-required files.

### 4. Verify, then promote

Full regression suite (234-test command is in `6848`'s Quick Start)
before any push. Push to `origin`, then update the LIVE checkout
(`~/.hermes/hermes-agent`) and `hermes gateway restart` only after
verification — never merge directly against the live checkout gateway
reads from.

### Carried, unrelated to this task (from the earlier `standalone-0b6d`
chain, still open, does not block this)

- Operator: cancel the DeepSeek account in its dashboard.
- Operator: tap bare `/clinepass` on Telegram to eyeball the full-width
  picker buttons.

## Quick Start

```bash
# --- Read the conflict map -------------------------------------------
cat ~/.hermes/hermes-agent-worktrees/upstream-merge/UPSTREAM_MERGE_CONFLICT_MAP.md

# --- Confirm nothing drifted since this handoff ------------------------
cd ~/.hermes/hermes-agent-worktrees/upstream-merge && git status -s && git rev-parse HEAD  # expect ae230f072e, only the untracked map file
cd ~/.hermes/hermes-agent && git rev-parse HEAD    # expect f8697f5598 (docs only, code unchanged since ae230f072e)

# --- The 8 files needing real judgment, in risk order -------------------
# plugins/platforms/telegram/adapter.py   tools/memory_tool.py
# gateway/slash_commands.py                gateway/run.py  (DESIGN DECISION)
# agent/moa_loop.py                        hermes_cli/models.py
# hermes_cli/runtime_provider.py           hermes_cli/model_cost_guard.py  (DESIGN DECISION)

# --- Regression gate to run after any Phase 2 resolution ----------------
cd ~/.hermes/hermes-agent && venv/bin/pytest \
  tests/hermes_cli/test_model_validation.py \
  tests/hermes_cli/test_model_switch_custom_providers.py \
  tests/hermes_cli/test_user_providers_model_switch.py \
  tests/hermes_cli/test_model_switch_configured_provider_routing.py \
  tests/hermes_cli/test_models.py tests/test_minimax_model_validation.py \
  tests/hermes_cli/test_openai_codex_model_validation_fallback.py \
  tests/hermes_cli/test_opencode_go_validation_fallback.py \
  tests/hermes_cli/test_openai_listing_authority.py \
  tests/gateway/test_telegram_choice_picker_rows.py \
  tests/hermes_cli/test_clinepass_command.py tests/gateway/test_clinepass_command.py \
  tests/gateway/test_choice_picker.py tests/gateway/test_reasoning_command.py \
  tests/gateway/test_fast_command.py tests/hermes_cli/test_commands.py -q
# (then the FULL suite before pushing -- the above is only the fix-6848
# regression set, not comprehensive for a merge this size)
```

### Gotchas carried forward from `6848` (unchanged, still true)

- Never bare `git push` in `~/.hermes/hermes-agent` — `main` tracks
  `upstream/main` directly; always `git push origin main`.
- Don't trust `git status --branch` ahead/behind on this repo.
- `hermes update` only syncs `origin`, never `upstream`.
- hermes-agent's task-worktree convention is
  `~/.hermes/hermes-agent-worktrees/<task>/`, not `~/src/hermes-worktrees/`.
