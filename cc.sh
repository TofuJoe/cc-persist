#!/usr/bin/env bash
# cc.sh — persistent, crash-resilient Claude Code sessions via ccmux.
# Sourced from ~/.bashrc. See README.md in this folder for the full rationale.
#
# ccmux keeps Claude in its CLASSIC renderer (native terminal scrollback / VTE
# touch-scroll keeps working) while still surviving a client/terminal crash — unlike
# abduco, under which Claude switched to its alternate-screen "fullscreen" renderer.
# To revert to the old abduco backend: cp cc.sh.abduco.bak cc.sh && source ~/.bashrc
#
# Session name: cc-<dir-basename>-<6hex-of-full-path>-<index>
#   cc        -> new agent at the lowest unused index in this dir
#   cc N      -> attach-or-create agent N here (reattach after a crash)
#   ccls      -> list sessions for the current project
#   ccall     -> list all cc-* sessions
#   ccnote N txt -> annotate session N (shown in listings)
#
# Detach (leave Claude running): Ctrl-\     Command run per session: $CC_CMD (default: claude).

: "${CC_CMD:=claude}"

# ccmux lives next to this script; CCMUX is the command used to invoke it.
# Resolve symlinks (this file is sourced via a symlink in ~/.bashrc.d/) so we find the
# real directory the script lives in, not the symlink's directory.
_cc_src="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")"
_cc_dir="$(cd -- "$(dirname -- "$_cc_src")" >/dev/null 2>&1 && pwd)"
: "${CCMUX:=python3 $_cc_dir/ccmux.py}"

# 6-hex stable id derived from the FULL directory path.
_cc_hash(){ printf '%s' "$PWD" | sha1sum | cut -c1-6; }

# cc [index] [extra args for $CC_CMD ...]
cc(){
  local slug="${PWD##*/}" hash prefix idx used
  hash="$(_cc_hash)"; prefix="cc-${slug}-${hash}-"
  if [[ ${1:-} =~ ^[0-9]+$ ]]; then idx="$1"; shift; fi
  if [[ -z ${idx:-} ]]; then
    used="$($CCMUX list 2>/dev/null | cut -f3 \
            | grep -oE -- "-${hash}-[0-9]+$" | grep -oE '[0-9]+$')"
    idx=1; while grep -qx "$idx" <<<"$used"; do idx=$((idx+1)); done
  fi
  printf '\033]0;%s\007' "${slug}#${idx}"        # label the terminal tab
  $CCMUX run "${prefix}${idx}" "$CC_CMD" "$@"
}

# Shared pretty-printer. Emits: "  #IDX  STATE  PROJECT  CREATED  NOTE"
# ccmux list format: one line per session, tab-separated:
#   <state> \t <YYYY-MM-DD HH:MM:SS> \t <name>
# state: 'attached' = a client is connected, 'detached' = running without a client.
# (Dead sessions clean themselves up, so they never appear.)
_cc_fmt(){
  local notes="$HOME/.config/cc/notes"
  $CCMUX list 2>/dev/null \
  | while IFS=$'\t' read -r state created name; do
      [[ $name =~ ^cc-(.+)-[0-9a-f]{6}-([0-9]+)$ ]] || continue
      local proj="${BASH_REMATCH[1]}" idx="${BASH_REMATCH[2]}" note=""
      [[ -f $notes ]] && note="$(grep -F "$name"$'\t' "$notes" | tail -1 | cut -f2-)"
      printf '  #%-3s %-9s %-18s %-19s %s\n' "$idx" "$state" "$proj" "$created" "$note"
    done | sort
}

ccall(){ printf '  IDX  STATE     PROJECT            CREATED          NOTE\n'; _cc_fmt; }

ccls(){
  printf '  IDX  STATE     PROJECT            CREATED          NOTE\n'
  _cc_fmt | grep -F -- "${PWD##*/}"
}

# ccnote <index> <free text...> — tag a session after its purpose is clear.
ccnote(){
  local slug="${PWD##*/}" hash name
  hash="$(_cc_hash)"; name="cc-${slug}-${hash}-${1}"; shift
  mkdir -p "$HOME/.config/cc"
  printf '%s\t%s\n' "$name" "$*" >> "$HOME/.config/cc/notes"
}
