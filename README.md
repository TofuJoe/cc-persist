# cc-persist — crash-resilient Claude Code sessions

Persistent, reattachable terminal sessions for Claude Code (and any command),
keyed by **project directory + numeric index**, using **ccmux** (a small custom
session manager that ships in this folder).

Created 2026-09-29. Switched from abduco to ccmux 2026-10-02 (see "Why ccmux, not
abduco" below).

## Why this exists

On this machine (Fedora, GNOME **Wayland**, GPU-unstable — see the xe/GTK4 notes)
gnome-shell crashes are a recurring event. On Wayland a shell crash tears down the
whole session and kills every client, **including Ptyxis and every shell inside it**.
The running processes (builds, ssh, Claude Code agents) die with the terminal because
they are its children on ptys it owns.

A **detached-server multiplexer** is the only thing that survives, because it owns the
ptys in a separate long-lived process, decoupled from the terminal window.

## Why not tmux

tmux works, but it takes over the **alternate screen** and keeps its **own scrollback**.
That starves Ptyxis's native scrollback — which breaks the custom **VTE touch-scroll
patch** (`~/ai/vte-touch-scroll-public`) that lets finger drags scroll Ptyxis history.
Any full multiplexer (tmux/screen/zellij) has this problem: it paints its own UI on the
alternate screen, so native scrollback (and touch-scroll) can't work. Ruled out.

## Why ccmux, not abduco

We started on **abduco** — a transparent detach/reattach shim with no alt-screen of its
own. It was the right idea, but it stopped working once Claude Code's **fullscreen
renderer** rolled out to this account: under abduco, Claude switched to its
alternate-screen ("fullscreen") renderer, which has no terminal scrollback, so the wheel
turned into arrow keys and touch-scroll died. Running `claude` **directly** stayed in the
classic renderer and scrolled fine — so something about abduco was the trigger.

It was tracked down (2026-10-02) by elimination. Everything Claude can observe at the
syscall boundary is **identical** under abduco vs. a direct run — environment, `$TERM`,
window size, the fd/tty set, termios/line-discipline, foreground pgrp, session structure;
abduco injects no env and Claude never walks `/proc` for its parent. The one real
difference: abduco relays program↔client bytes **through a Unix-domain socket + a detached
server**, and Claude's terminal layer reacts to that relay's I/O behaviour by going
fullscreen. A non-detaching in-process pty relay (`goodrelay`, kept for reference) stays
classic; abduco does not. No env var or setting overrides it (`tui:"default"`,
`CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1`, `CLAUDE_CODE_NO_FLICKER=0`, mouse toggles — all
tested, all still fullscreen), because the choice is driven by a server-gated rollout, not
a knob.

**ccmux** keeps abduco's virtues (transparent, no alt-screen, detach/reattach, survives
terminal death) but **does not byte-relay**. The detached server owns the pty + child and
passes the pty's **master fd** to the attached foreground client over its control socket
(`SCM_RIGHTS`). The client then drives the real pty directly — exactly like running the
program under a plain pty wrapper — so Claude sees an ordinary interactive terminal and
**stays classic**. Native scrollback and the touch-scroll patch keep working, and the
session still survives a Ptyxis/gnome-shell crash. Trade-off: it's ~190 lines of custom
Python (not battle-tested like abduco), single-client per session, and no splits/UI.

## logind: no changes needed (and do NOT set KillUserProcesses=no)

What this protects against: closing the Ptyxis tab/window, a Ptyxis or VTE crash, an SSH
drop, and a gnome-shell crash **where the logind session itself survives** (gnome-shell
respawns). In all of these the login session stays open, so the detached ccmux server —
an ordinary process in that session's cgroup — keeps running on stock systemd defaults. No
logind change is required for this, the common case.

What it does NOT survive: a **full logout / session teardown**. Default
`KillUserProcesses=yes` reaps the session scope, taking the ccmux server with it. We
accept this as a valid exit — recover with `cc <N> --continue`, which resumes the agent
from its on-disk transcript after a reboot/logout just as it does after a hard crash.

> ⚠️ Do NOT try to plug that gap with `KillUserProcesses=no`. On 2026-09-29 that setting
> **broke GUI login**: applying it drops the live GNOME session, and because its
> processes are no longer reaped the dead session keeps squatting on `seat0`, so every
> new graphical login bounces back to GDM (`gnome-shell … NotInControl`). Recovery was
> `loginctl terminate-session <stale-id>` from a text VT. If you ever want full-logout
> survival, the safe route is `loginctl enable-linger` on top of the `cc.slice` scope launch
> below (the scope lives under `user@.service`, which linger keeps alive across logout).

## Desktop priority: `cc.slice`

ccmux launches the program via `systemd-run --user --scope --slice=cc.slice`, so every
session and everything it spawns (tool shells, builds, subagents) sits in one cgroup,
`cc.slice/ccmux-<session>-<pid>.scope` (the pid keeps a scope that outlives its session —
a lingering backgrounded child, say — from blocking a relaunch of the same name). `~/.config/systemd/user/cc.slice` sets its weight against
the rest of the desktop: `CPUWeight=20`, `IOWeight=20` (gnome-shell in session.slice is 500
via uresourced, ordinary apps 100) and `MemoryHigh=8G`, so a fleet of sessions gets
reclaimed and throttled before gnome-shell is pushed into swap. systemd-oomd kills the worst
session cgroup at 50% memory-pressure for 10 s, same as app.slice. `nice` cannot do this:
with the cpu controller on, nice only ranks tasks *within* a cgroup.

Edit the slice file and `systemctl --user daemon-reload` to retune (applies to running
sessions); `CPUWeight=idle` runs sessions only on otherwise-idle CPU. `CCMUX_SLICE=` (empty)
disables the wrapper; ccmux also falls back to a plain exec when `systemd-run` or
`XDG_RUNTIME_DIR` is missing. `systemctl --user list-units 'ccmux-*'` shows the live scopes.

## Naming model

Session name = `<tool>-<dir-basename>-<6hex-of-full-path>-<index>`, where `<tool>` is
`cc` (Claude Code) or `oc` (opencode). Each tool has its **own index space**, so `oc 2`
always means opencode session 2 and never collides with `cc 2`.

- The 6-hex hash of the **full path** disambiguates two different dirs with the same
  basename (e.g. two `web/` dirs).
- The **index** is positional, not semantic — by deliberate choice. We often run several
  Claude instances in the *same* project dir (one building, one for read-only/strategy
  discussion) and don't want to pre-declare each terminal's purpose.
- You recover meaning by **reattaching and reading** the live session, by **created
  time** in the listing, or by an optional after-the-fact **note** (`ccnote`).

## Functions (defined in `cc.sh`)

| Command | Does |
|---|---|
| `cc`            | Start a new agent at the **lowest unused index** in this dir |
| `cc N`          | **Attach-or-create** agent N in this dir (reattach after a crash) |
| `cc N --flag …` | Extra args pass through to the launched command (e.g. `cc 1 --continue`) |
| `ccls`          | List sessions for the **current** project |
| `oc`, `oc N`, `oc N --flag …` | Same as `cc`, but runs **opencode** (`oc-…` sessions) |
| `ccall`         | List **all** cc-*/oc-* sessions across projects (TOOL column shows which) |
| `ccnote N text` | Annotate session N (shown by `ccls`/`ccall`) — tag it *after* its purpose emerges |
| `ocnote N text` | Same, for opencode session N |

The command each session runs is `$CC_CMD` (default `claude`) for `cc`, `$OC_CMD`
(default `opencode`) for `oc`. The SendMessage isolation hooks apply only to `claude`.

opencode note: it is a full-screen (alt-screen) TUI by design, so it never has native
scrollback/touch-scroll — ccmux gives it crash survival and reattach only. After a
*terminal* crash, the new terminal never saw opencode's alt-screen switch, so its
repaint lands on the main screen (cosmetic; the session itself is fine).

### Detach / reattach
- Detach: **Ctrl-\\** (ccmux default; the inner app never sees it — override with
  `CCMUX_DETACH=\xNN`). Or just close the tab / let GNOME crash — the session keeps
  running headless.
- Reattach: `cc N` from the same directory.
- Exit for real: type `exit` / end Claude → the session is destroyed and cleaned up.

### Read-only strategy sessions
"Read-only" here is a Claude Code concern, not a ccmux one. Launch such a session in
plan mode so it genuinely cannot edit:

    cc 2 --permission-mode plan

### Session isolation (`cc-isolate.py`)
Claude Code can list every Claude session on the machine (`ListAgents`) and message any
of them (`SendMessage`), so a session would sometimes hand work to an agent running in
another terminal. Asking it in the prompt not to was unreliable. `cc` therefore launches
`claude --settings '<json>'` with two hooks (the JSON is generated by
`_cc_isolate_settings`; `--settings` merges with user/project hooks, verified):

- `record` (PostToolUse on Agent/SendMessage/ListAgents) saves the session's own
  subagents (name, agentId, and the `[ref]` once seen) to `~/.cache/cc/isolate/<session_id>.json`.
  State persists so `--continue` can still reach revived agents; files untouched for 60 days are pruned.
- `guard` (PreToolUse on SendMessage) lets messages to those subagents through. Any other
  target gets `CC_ISOLATE`'s decision: `ask` (default, a permission prompt) or `deny`.
  If the hook itself errors, it returns `ask`, never allow.

`CC_ISOLATE=0 cc 3` launches without the hooks. The value is fixed when the session is
created; reattaching doesn't change it. Limits: this only guards outgoing messages from
`cc`-launched sessions, and `ListAgents` still shows peer sessions; the hooks block
messages to them, not seeing them.

## Resilience layers (what survives what)

| Failure | ccmux keeps process alive? | `claude --continue` (disk transcript)? |
|---|---|---|
| gnome-shell / Ptyxis crash (common here) | ✅ live, mid-task, in-memory context | ⚠️ transcript only; in-flight tool runs lost |
| reboot / kernel Oops / power loss | ❌ process gone | ✅ reloads conversation from disk |

Use ccmux for GUI crashes; `cc N --continue` recreates an index and resumes from disk
after a hard crash.

## How ccmux works (`ccmux.py`)

- `ccmux run <name> [cmd…]` — attach to session `<name>`, or create it running `cmd`.
- `ccmux list` — one TSV line per live session: `state <TAB> created <TAB> name`
  (`state` = `attached` | `detached`). `cc.sh` uses this for listings and auto-indexing.
- **Server** (detached: `setsid` + double-fork → reparented to init): owns the pty and the
  child, ignores SIGHUP/SIGPIPE, and parks the program when no client is attached (draining
  into a bounded **replay buffer** so the program never blocks on a full pty).
- **Client** (foreground): sends `A`, receives the pty **master fd** via `SCM_RIGHTS` plus a
  length-prefixed replay of output produced while detached, then relays the real pty and
  forwards `SIGWINCH`. This direct-fd path is what keeps Claude classic.
- Single client per session (a second attach is refused). Sockets live in `~/.ccmux/`
  (dir `0700`, socket `0600`); a session removes its socket when the program exits; stale
  sockets are cleaned up on `list`.

Env knobs: `CCMUX_DIR` (default `~/.ccmux`), `CCMUX_DETACH` (default `\x1c` = Ctrl-\\),
`CCMUX_BUF` (replay-buffer cap, default 1 MiB).

## Gotchas

- **Detached = idle = zero token burn.** A detached Claude is blocked on input; it does
  no work and costs nothing until reattached. Many persistent sessions are cheap.
- **Reattach scrollback:** on reattach, ccmux resizes the pty, sends SIGWINCH (Claude
  repaints), and **replays output produced while you were detached**. But classic
  scrollback lives in the *terminal*, so output from *before* you detached is not
  re-rendered into a fresh window — that window's scrollback starts from the replay. During
  a live session, touch-scroll of full history works as normal.
- **Same-repo concurrency risk** (two Claudes editing one tree) is pre-existing and
  orthogonal — ccmux neither adds nor removes it.
- **ccmux is custom code**, newer than abduco. Shake it out on non-critical sessions
  first. To revert to abduco: `cp cc.sh.abduco.bak cc.sh && source ~/.bashrc.d/cc-persist.sh`.
- **Pre-existing abduco sessions** are not visible to the ccmux-based `cc`. Reattach them
  directly with `abduco -A <name>` until they're done.

## Install / uninstall

`~/.bashrc` already sources everything in `~/.bashrc.d/`, so setup symlinked this in
rather than editing `.bashrc` (keeps `cc.sh` here the single source of truth):

    ~/.bashrc.d/cc-persist.sh -> ~/ai/cc-persist/cc.sh

`cc.sh` resolves that symlink (`readlink -f`) to find `ccmux.py` beside it. ccmux
sockets live in `~/.ccmux/<name>`. Notes live in `~/.config/cc/notes`.

To uninstall:

    rm ~/.bashrc.d/cc-persist.sh
    sudo dnf remove abduco        # optional; only still needed to reach old abduco sessions

(Setup makes no system-level changes, so there is nothing else to undo. If an *earlier*
version left `/etc/systemd/logind.conf.d/keep-sessions.conf` behind, remove it with
`sudo rm … && sudo systemctl reload systemd-logind` — reload, never restart, so live
sessions are not dropped.)

## Files

- `ccmux.py` — the session manager.
- `cc.sh` — the `cc`/`oc`/`ccls`/`ccall`/`ccnote`/`ocnote` shell functions (ccmux backend).
- `cc-isolate.py` — SendMessage guard hooks that keep a session from messaging peer sessions.
- `cc.sh.abduco.bak` — the previous abduco-backed `cc.sh` (one-line revert).
- `gmtest.py` — regression suite for ccmux (run from this dir with
  `CCMUX_DIR=/tmp/gmtest`, backgrounded + `</dev/null` so its pty children don't wedge
  the shell). 6 checks: create, detach, reattach+replay, crash-survival, reattach, teardown.

## Verified against
- Fedora 44, Ptyxis 50.1, kernel 7.2.5, GNOME Wayland, Python 3.14.
- Claude Code 2.1.287 (fullscreen rollout active on this account).
- ccmux: 6/6 gmtest checks pass; Claude confirmed classic (no `ESC[?1049h`) under it.
- abduco 0.6 (rpm abduco-0.6-24.fc44) — previous backend, kept installed for old sessions.

## Related notes
- `~/ai/vte-touch-scroll-public` — the VTE touch-scroll patch this preserves.
- Memory: vte-touchscroll-patch, cc-claude-touchscroll-mouse (the full fullscreen/ccmux
  investigation), gtk4-vulkan-device-lost, intel-xe-driver (why crashes are frequent here).
