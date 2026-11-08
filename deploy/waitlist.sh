#!/usr/bin/env bash
# prints the public site's waitlist, oldest first: bun run waitlist <ssh-host>
set -euo pipefail
host=${1:?Use bun run waitlist <ssh-host>.}
ssh "$host" 'sudo cat /var/lib/flowback-web/waitlist.jsonl 2> /dev/null || echo "No one has joined yet." >&2'
