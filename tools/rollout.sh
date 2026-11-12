#!/usr/bin/env bash
# Migrate repositories to this action, in fresh clones.
# Usage: tools/rollout.sh prepare <workdir> <owner/repo>...   clone, migrate, commit (no push)
#        tools/rollout.sh push    <workdir> <owner/repo>...   push the prepared commits
set -euo pipefail
mode=$1; work=$2; shift 2
here=$(cd "$(dirname "$0")" && pwd)
for repo in "$@"; do
  dir="$work/${repo#*/}"
  if [ "$mode" = prepare ]; then
    rm -rf "$dir"
    gh repo clone "$repo" "$dir" -- -q --depth 1
    files=$(grep -lE 'uses:\s*actions/upload-artifact@' "$dir"/.github/workflows/*.y*ml || true)
    [ -n "$files" ] || { echo "skip $repo (nothing to migrate)"; continue; }
    steps=$(python "$here/migrate.py" $files | awk '{s+=$1} END {print s}')
    git -C "$dir" commit -qam "CI: upload artifacts to Garage instead of GitHub artifact storage

GitHub artifacts count against the account storage quota; the org's
upload-artifact action stores them in the Garage bucket and links them
from the job summary.

Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>"
    echo "ready $repo: $steps step(s), branch $(git -C "$dir" branch --show-current)"
  else
    git -C "$dir" push -q origin HEAD && echo "pushed $repo"
  fi
done
