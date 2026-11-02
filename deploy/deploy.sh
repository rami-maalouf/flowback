#!/usr/bin/env bash
# deploys the committed HEAD to an ubuntu server over ssh: the engine and the web app, on its loopback only.
# the web app is built for the server's public origin (FLOWBACK_ORIGIN in /etc/flowback/web.env), if it has one.
# usage: bun run deploy <ssh-host>
set -euo pipefail

host=${1:?usage: bun run deploy <ssh-host>}
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"

if [ -n "$(git status --porcelain)" ]; then
  echo "commit or stash your changes first: a deploy ships the committed HEAD only." >&2
  exit 1
fi
revision=$(git rev-parse --short HEAD)

origin=$(ssh "$host" "sed -n 's/^FLOWBACK_ORIGIN=//p' /etc/flowback/web.env 2> /dev/null" || true)
EXACT_BACKEND_ORIGIN=${origin:-http://127.0.0.1:8891} bun exact.mjs web-build --out dist
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
mkdir "$stage/app"
git archive HEAD | tar -x -C "$stage/app"
echo "$revision" > "$stage/app/REVISION"

ssh "$host" 'sudo bash -s' <<'EOF'
set -euo pipefail
for tool in uv bun /usr/bin/python3.12; do
  command -v "$tool" > /dev/null || { echo "the server needs $tool" >&2; exit 1; }
done
# python3.12-dev: some locked packages, such as diffq, have no arm64 wheel and build from source.
packages=()
command -v ffmpeg > /dev/null || packages+=(ffmpeg)
dpkg -s python3.12-dev > /dev/null 2>&1 || packages+=(python3.12-dev)
command -v gcc > /dev/null || packages+=(gcc)
if [ ${#packages[@]} -gt 0 ]; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${packages[@]}" > /dev/null
fi
id flowback > /dev/null 2>&1 || useradd --system --home-dir /var/lib/flowback --shell /usr/sbin/nologin flowback
install -d -o flowback -g flowback -m 0750 /var/lib/flowback /var/lib/flowback/data /var/lib/flowback/models
install -d -m 0755 /opt/flowback /opt/flowback/app /opt/flowback/web /opt/flowback/bin
# the web service gets its own copy: a bun installed in a home directory is hidden from services.
install -m 0755 "$(readlink -f "$(command -v bun)")" /opt/flowback/bin/bun
EOF

rsync -a --delete --exclude /.venv --rsync-path='sudo rsync' "$stage/app/" "$host:/opt/flowback/app/"
rsync -a --delete --rsync-path='sudo rsync' dist/ "$host:/opt/flowback/web/"

ssh "$host" 'sudo bash -s' <<'EOF'
set -euo pipefail
chown -R root:root /opt/flowback
cd /opt/flowback/app
UV_PYTHON_DOWNLOADS=never uv sync --frozen --extra models --no-dev --python /usr/bin/python3.12 --quiet
install -m 0644 deploy/flowback-engine.service deploy/flowback-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet flowback-engine flowback-web
systemctl restart flowback-engine flowback-web
for attempt in $(seq 120); do
  curl -fsS http://127.0.0.1:8891/health > /dev/null 2>&1 && break
  [ "$attempt" = 120 ] && { journalctl -u flowback-engine -n 40 --no-pager; exit 1; }
  sleep 1
done
# 401 is the password prompt of a public site.
for attempt in $(seq 20); do
  case $(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8893/ 2> /dev/null) in 200 | 401) break ;; esac
  [ "$attempt" = 20 ] && { journalctl -u flowback-web -n 20 --no-pager; exit 1; }
  sleep 1
done
echo "flowback $(cat REVISION) is running: $(curl -fsS http://127.0.0.1:8891/health)"
EOF

if [ -n "$origin" ]; then
  echo "open $origin"
else
  echo "open it from this machine: ssh -N -L 8891:127.0.0.1:8891 -L 8893:127.0.0.1:8893 $host"
  echo "then go to http://127.0.0.1:8893"
fi
