#!/usr/bin/env bash
# prints the public site's waitlist, oldest first: bun run waitlist <ssh-host>
set -euo pipefail
host=${1:?Use bun run waitlist <ssh-host>.}
ssh "$host" 'file=/var/lib/flowback-web/waitlist.jsonl; if sudo test -s "$file"; then sudo cat "$file"; else echo "No one has joined yet." >&2; fi'
