# RCA: Gateway stayed down after a single SIGTERM on macOS — `under_systemd` misdetection and launchd's crash-loop teardown

**Status:** unresolved — manual recovery only (`launchctl bootstrap`); no code fix yet
**Severity:** P2 — on macOS, an external SIGTERM to the gateway can leave it permanently down with no auto-revival, contrary to what the shutdown log claims.

## Summary

At 2026-08-08 10:02:16 the gateway process received a single unplanned
`SIGTERM`, shut down cleanly in 2.33s, and exited with code 1 at 10:02:29
— exactly per the intended "unplanned signal → exit non-zero so the
service manager revives us" design in `gateway/run.py`. On macOS this
did **not** happen: 124ms after the exit, `launchd` logged `service
inactive: ai.hermes.gateway` / `removing service: ai.hermes.gateway` and
fully deregistered the job from the `gui/501` domain. `launchctl print`
subsequently returned `Could not find service "ai.hermes.gateway" in
domain for user gui: 501` — the gateway was not "crash-looping and
restarting", it was gone, and stayed gone for ~3.5 hours until manually
revived with:

```bash
launchctl bootstrap gui/501 ~/Library/LaunchAgents/ai.hermes.gateway.plist
```

Telegram/Signal/Discord all reconnected cleanly once relaunched — no
data loss, but a silent, unbounded outage window on the operator's only
messaging channel to the agent.

## What actually killed it (unconfirmed)

The gateway's own shutdown forensics (`gateway/shutdown_forensics.py`,
`_proc_summary`) captured `parent_name=?` and `parent_cmdline='(unknown)'`
for the SIGTERM sender — its own diagnostic couldn't identify who sent
the signal. `log show` for the surrounding window only shows
`backgroundtaskmanagementd` re-registering the legacy launch item three
times (10:01:33, 10:01:44, 10:02:16) shortly before the kill, then
`launchd`'s own teardown at 10:02:29 — consistent with, but not proof
of, a macOS Background Task Management (BTM) re-registration cycle
being the trigger. No `hermes update`/`hermes gateway stop`/manual
`kill` was run by the operator or any known agent session in that
window. This part of the RCA is inconclusive and would need a live
repro with `log stream` running to pin down.

## Root cause: `under_systemd` is a false positive on 100% of macOS launchd-managed runs

`gateway/shutdown_forensics.py:143`:

```python
ctx["under_systemd"] = bool(invocation_id) or ppid == 1
```

The `ppid == 1` half of this check is Linux-specific reasoning that
does not hold on macOS. Since macOS 10.10, launchd unifies **all**
domains (system, per-user GUI, background) under the single PID-1
`launchd` process — there is no separate per-user launchd process the
way there is a separate systemd `--user` instance on Linux. Any
process bootstrapped via `launchctl bootstrap gui/<uid> ...` — i.e.
every `LaunchAgent` on this machine, not just this gateway — reports
`getppid() == 1`. Confirmed directly against the live process:

```
$ ps -o pid,ppid,comm -p 91049
  PID  PPID COMM
91049     1 /Users/djbclark/.local/bin/hermes
```

So on macOS, `under_systemd` reads `True` unconditionally, for every
launchd-managed run, regardless of whether systemd is involved at all
(it never is — this host doesn't have systemd). The
`systemd_invocation_id`/`systemd_journal_stream` env-var checks are
correct and platform-specific; the `ppid == 1` fallback is not.

**This is not what caused the outage** — the exit-code-1 branch at
`gateway/run.py:25727` does not actually gate on `under_systemd`, it
fires unconditionally for `_signal_initiated_shutdown and not
runner._restart_requested`. But the log line it emits is actively
misleading on this platform:

```
INFO gateway.run: Exiting with code 1 (signal-initiated shutdown without restart
request) so systemd Restart=on-failure can revive the gateway.
```

There is no systemd here. What actually governs whether the process
comes back is launchd's `KeepAlive`/`ThrottleInterval` — a materially
different contract:

- systemd `Restart=on-failure`: retries indefinitely (subject to
  `StartLimitBurst`/`StartLimitIntervalSec`, which are opt-in and
  usually generous), and a unit that trips the limit is left in a
  `failed` state that's trivially visible via `systemctl status` and
  re-armed with `systemctl reset-failed`.
- macOS launchd `KeepAlive=true`: this incident shows a **single**
  qualifying exit was enough for launchd to log `removing service` and
  fully deregister the job — no `failed` state, no `launchctl list`
  entry at all, nothing short of re-reading the plist and
  `launchctl bootstrap`-ing it again brings it back. There is no
  built-in equivalent of `systemctl reset-failed` to discover or clear.

The `container_boot` auto-start path (`gateway_state=running` persisted
for the next boot, issue #42675) only helps on a full reboot/container
recreate — it does nothing for this case, since the *box* never
restarted, only the launchd job record was torn down.

## Suggested fix

1. Stop conflating "under launchd" and "under systemd" — they're
   different supervisors with different restart guarantees and the
   operator-facing log/behavior should say which one is actually in
   play:

   ```python
   import platform

   if platform.system() == "Linux":
       ctx["under_systemd"] = bool(invocation_id) or ppid == 1
   else:
       ctx["under_systemd"] = bool(invocation_id)  # env-var signal only

   ctx["under_launchd"] = (
       platform.system() == "Darwin" and bool(os.environ.get("XPC_SERVICE_NAME"))
   )
   ```

   (`XPC_SERVICE_NAME` is set by launchd for agents/daemons it
   bootstraps and is the macOS-native equivalent of
   `INVOCATION_ID`/`JOURNAL_STREAM` — a positive signal instead of the
   `ppid==1` guess.)

2. Make the `gateway.run` log line and `format_context_for_log` branch
   on which supervisor was actually detected, e.g. `"so launchd
   KeepAlive can revive the gateway (best-effort — a launchd job that
   exits repeatedly can be deregistered outright; check `launchctl
   print gui/<uid>/ai.hermes.gateway`)"` on Darwin, versus the current
   systemd-specific message on Linux.

3. Consider a lightweight self-heal: since launchd gives no
   `reset-failed` equivalent, a periodic external check (cron/launchd
   `StartInterval` on a *second*, trivial watchdog job, or a check
   folded into `hermes doctor`) that runs `launchctl print
   gui/<uid>/ai.hermes.gateway` and re-bootstraps from the plist if the
   service is missing would close the gap this incident exposed —
   otherwise any future signal (identified or not) that trips this path
   on macOS produces a silent, indefinite outage again.

## Recovery (what was actually run)

```bash
launchctl bootstrap gui/501 ~/Library/LaunchAgents/ai.hermes.gateway.plist
```

Confirmed via `launchctl print gui/501/ai.hermes.gateway` (`state =
running`) and gateway log (`✓ telegram connected`, `✓ signal
connected`, `✓ discord connected` within ~15s).
