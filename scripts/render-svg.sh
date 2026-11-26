#!/usr/bin/env bash
# render-svg.sh <file.svg> <out.png>: renders an SVG at its viewBox size with headless Chrome.
read -r w h < <(python3 -c "import re,sys; v=re.search(r'viewBox=\"0 0 (\d+) (\d+)\"', open(sys.argv[1]).read()); print(v[1], v[2])" "$1")
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu --hide-scrollbars \
  --window-size="$w,$h" --screenshot="$2" "file://$(cd "$(dirname "$1")" && pwd)/$(basename "$1")" 2>/dev/null
