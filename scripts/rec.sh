#!/usr/bin/env bash
# Evidence recorder: scripts/rec.sh <step> '<command>'
# Appends "$ <command>" plus its real combined output to evidence/logs/<step>.log
# and returns the command's exit status. The home directory is shown as ~ and
# ANSI colour codes are stripped (scripts/clean-output.py).
set -uo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
step="$1"; shift
log="$root/evidence/logs/$step.log"
mkdir -p "$(dirname "$log")"
printf '\n# %s\n$ %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*" >>"$log"
cd "$root"
bash -c "$*" 2>&1 | python3 -u "$root/scripts/clean-output.py" | tee -a "$log"
rc=${PIPESTATUS[0]}
printf '[exit %s]\n' "$rc" >>"$log"
exit "$rc"
