#!/usr/bin/env bash
# Concatenates report/parts (intro, then Q1..Q7) into report/lab4.md.
cd "$(dirname "$0")/../report"
{ cat parts/intro.md; for q in 1 2 3 4 5 6 7; do printf '\n'; cat "parts/q$q.md"; done; } > lab4.md
grep -c '^# ' lab4.md
