#!/usr/bin/env bash
# Set the two Garage secrets on repositories.
# Usage: tools/set_secrets.sh <env-file> <owner/repo>...
# The env file holds GARAGE_ACCESS_KEY_ID=... and GARAGE_SECRET_ACCESS_KEY=... lines.
set -euo pipefail
env_file=$1; shift
for name in GARAGE_ACCESS_KEY_ID GARAGE_SECRET_ACCESS_KEY; do
  grep -q "^$name=." "$env_file" || { echo "$name missing in $env_file" >&2; exit 1; }
done
for repo in "$@"; do
  gh secret set --repo "$repo" --env-file "$env_file" >/dev/null
  echo "set  $repo"
done
