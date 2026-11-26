#!/usr/bin/env bash
# shot.sh <url> <out.png> [width] [height]: headless Chrome screenshot (no login needed).
C="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$C" --headless=new --disable-gpu --hide-scrollbars --window-size="${3:-1600},${4:-1000}" \
  --virtual-time-budget=30000 --screenshot="$2" "$1" 2>/dev/null | grep -v '^$' || true
ls -l "$2" | awk '{print $5, $NF}'
