#!/usr/bin/env bash
# cc.sh — persistent, crash-resilient Claude Code sessions via ccmux.
# Sourced from ~/.bashrc. See README.md in this folder for the full rationale.
#
# ccmux keeps Claude in its CLASSIC renderer (native terminal scrollback / VTE
# touch-scroll keeps working) while still surviving a client/terminal crash — unlike
# abduco, under which Claude switched to its alternate-screen "fullscreen" renderer.
# To revert to the old abduco backend: cp cc.sh.abduco.bak cc.sh && source ~/.bashrc
#
# Session name: {cc|oc}-<dir-basename>-<6hex-of-full-path>-<index>
#   cc        -> new agent at the lowest unused index in this dir
#   cc N      -> attach-or-create agent N here (reattach after a crash)
#   oc, oc N  -> same, for opencode (session name prefix oc- instead of cc-)
#   ccls      -> list sessions for the current project
#   ccall     -> list all cc-*/oc-* sessions
#   ccnote N txt -> annotate session N (shown in listings); ocnote N txt for opencode
#
# Detach (leave Claude running): Ctrl-\     Command run per session: $CC_CMD (default: claude).

: "${CC_CMD:=claude}"
: "${OC_CMD:=opencode}"

# ccmux lives next to this script; CCMUX is the command used to invoke it.
# Resolve symlinks (this file is sourced via a symlink in ~/.bashrc.d/) so we find the
# real directory the script lives in, not the symlink's directory.
_cc_src="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")"
_cc_dir="$(cd -- "$(dirname -- "$_cc_src")" >/dev/null 2>&1 && pwd)"
: "${CCMUX:=python3 $_cc_dir/ccmux.py}"

# 6-hex stable id derived from the FULL directory path.
_cc_hash(){ printf '%s' "$PWD" | sha1sum | cut -c1-6; }

# _cc_launch <tool-prefix> <command> [index] [extra args ...]
# Shared by cc (Claude) and oc (opencode). Each prefix has its own index space, so
# `oc 2` always reattaches/recreates opencode session 2, never Claude's.
_cc_launch(){
  local tool="$1" cmd="$2"; shift 2
  local slug="${PWD##*/}" hash prefix idx used
  hash="$(_cc_hash)"; prefix="${tool}-${slug}-${hash}-"
  if [[ ${1:-} =~ ^[0-9]+$ ]]; then idx="$1"; shift; fi
  if [[ -z ${idx:-} ]]; then
    used="$($CCMUX list 2>/dev/null | cut -f3 \
            | grep -F -- "$prefix" | grep -oE -- "-${hash}-[0-9]+$" | grep -oE '[0-9]+$')"
    idx=1; while grep -qx "$idx" <<<"$used"; do idx=$((idx+1)); done
  fi
  local label="${PWD/#$HOME/\~}#${idx}"; [[ $tool == cc ]] || label="$tool $label"
  printf '\033]0;%s\007' "$label"   # label the tab: ~/path#idx (oc: "oc ~/path#idx")
  local iso=()
  [[ $cmd == claude && ${CC_ISOLATE:-ask} != 0 ]] && iso=(--settings "$(_cc_isolate_settings)")
  # Stop Claude from overwriting the tab title with its own.
  CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1 $CCMUX run "${prefix}${idx}" "$cmd" "${iso[@]}" "$@"
}

# cc [index] [extra args for $CC_CMD ...]   — Claude Code (or $CC_CMD)
cc(){ _cc_launch cc "$CC_CMD" "$@"; }

# oc [index] [extra args for $OC_CMD ...]   — opencode (or $OC_CMD)
oc(){ _cc_launch oc "$OC_CMD" "$@"; }

# Hooks that stop a session messaging peer sessions (other terminals); see cc-isolate.py.
# CC_ISOLATE=ask (default) prompts, =deny refuses, =0 disables. Read at session start.
_cc_isolate_settings(){
  local h="CC_ISOLATE=${CC_ISOLATE:-ask} python3 $_cc_dir/cc-isolate.py"
  printf '{"hooks":{"PreToolUse":[{"matcher":"SendMessage","hooks":[{"type":"command","command":"%s guard"}]}],"PostToolUse":[{"matcher":"Agent|SendMessage|ListAgents","hooks":[{"type":"command","command":"%s record"}]}]}}' "$h" "$h"
}

# Shared pretty-printer. Emits: "  #IDX  TOOL  STATE  PROJECT  CREATED  NOTE"
# ccmux list format: one line per session, tab-separated:
#   <state> \t <YYYY-MM-DD HH:MM:SS> \t <name>
# state: 'attached' = a client is connected, 'detached' = running without a client.
# (Dead sessions clean themselves up, so they never appear.)
_cc_fmt(){
  local notes="$HOME/.config/cc/notes"
  $CCMUX list 2>/dev/null \
  | while IFS=$'\t' read -r state created name; do
      [[ $name =~ ^(cc|oc)-(.+)-[0-9a-f]{6}-([0-9]+)$ ]] || continue
      local tool="${BASH_REMATCH[1]}" proj="${BASH_REMATCH[2]}" idx="${BASH_REMATCH[3]}" note=""
      [[ -f $notes ]] && note="$(grep -F "$name"$'\t' "$notes" | tail -1 | cut -f2-)"
      printf '  #%-3s %-4s %-9s %-18s %-19s %s\n' "$idx" "$tool" "$state" "$proj" "$created" "$note"
    done | sort
}

ccall(){ printf '  IDX  TOOL STATE     PROJECT            CREATED             NOTE\n'; _cc_fmt; }

ccls(){
  printf '  IDX  TOOL STATE     PROJECT            CREATED             NOTE\n'
  _cc_fmt | grep -F -- "${PWD##*/}"
}

# ccnote <index> <free text...> — tag a session after its purpose is clear.
# ocnote <index> <free text...> — same, for an opencode session.
_cc_note(){
  local tool="$1" slug="${PWD##*/}" hash name
  hash="$(_cc_hash)"; name="${tool}-${slug}-${hash}-${2}"; shift 2
  mkdir -p "$HOME/.config/cc"
  printf '%s\t%s\n' "$name" "$*" >> "$HOME/.config/cc/notes"
}
ccnote(){ _cc_note cc "$@"; }
ocnote(){ _cc_note oc "$@"; }
