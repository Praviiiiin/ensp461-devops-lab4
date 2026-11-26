#!/usr/bin/env bash
# Evidence recorder: scripts/rec.sh <step> '<command>'
# Appends "$ <command>" plus its real combined output to evidence/logs/<step>.log
# (home directory shown as ~) and returns the command's exit status.
set -uo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
step="$1"; shift
log="$root/evidence/logs/$step.log"
mkdir -p "$(dirname "$log")"
printf '\n# %s\n$ %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*" >>"$log"
cd "$root"
bash -c "$*" 2>&1 | sed -u "s#$HOME#~#g" | tee -a "$log"
rc=${PIPESTATUS[0]}
printf '[exit %s]\n' "$rc" >>"$log"
exit "$rc"
