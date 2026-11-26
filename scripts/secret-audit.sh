#!/usr/bin/env bash
# secret-audit.sh: proves the real secret values never reached Git and that the cluster holds
# exactly the values that were encrypted. Reads plaintext only from .secrets/ (git-ignored) and
# prints counts and hash prefixes, never values.
set -uo pipefail
cd "$(dirname "$0")/.."
echo "== plaintext secret values found anywhere in Git history (all commits, all files):"
git log -p --all | grep -F -c -f .secrets/plaintext-values
echo "== cluster value vs local plaintext (sha256 prefixes)"
for pair in "sl-data pg-credentials app-password pg-app" "sl-canary shortlink-secrets pg-password pg-app" \
            "sl-canary shortlink-secrets admin-token admin-token-1" "sl-bg shortlink-secrets admin-token admin-token-1"; do
  set -- $pair
  c=$(kubectl -n "$1" get secret "$2" -o jsonpath="{.data.$3}" | base64 -d | shasum -a 256 | cut -c1-12)
  l=$(tr -d '\n' < ".secrets/$4" | shasum -a 256 | cut -c1-12)
  echo "$1/$2[$3] cluster=$c local=$l $([ "$c" = "$l" ] && echo MATCH || echo DIFFERENT)"
done
