#!/usr/bin/env bash
# deploy.sh — install the vector_wirepod custom integration into a Home
# Assistant Supervised host over ssh, then run HA's config check.
#
# Usage: deploy.sh [--host homeassistant] [--config /var/lib/homeassistant/homeassistant] [--restart]
#   --restart  restart HA core after a clean check (needed to load new code).
# Exit: 0 ok, 2 usage, 3 environment, 5 install or check failed.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOST="homeassistant"
CONFIG="/var/lib/homeassistant/homeassistant"
RESTART=0
TGZ=""
cleanup() { [[ -n "$TGZ" ]] && rm -f "$TGZ"; return 0; }
trap cleanup EXIT

usage() { sed -n '2,8p' "$0"; exit 2; }

parse_args() {
  local i
  for ((i = 0; i < 6 && $# > 0; i++)); do
    case "$1" in
      --host) HOST="${2:?}"; shift 2 ;;
      --config) CONFIG="${2:?}"; shift 2 ;;
      --restart) RESTART=1; shift ;;
      -h|--help) usage ;;
      *) echo "unknown argument: $1" >&2; usage ;;
    esac
  done
  [[ $# -eq 0 ]] || usage
}

remote_install() {
  # The tarball was uploaded first; this script arrives on stdin and the
  # destination path travels as an argument, never inside a quoted string.
  ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" bash -s -- "$CONFIG" <<'REMOTE'
set -euo pipefail
CONFIG="$1"
DEST="$CONFIG/custom_components/vector_wirepod"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
sudo -n test -d "$CONFIG/custom_components" || { echo "no custom_components under $CONFIG" >&2; exit 3; }
if sudo -n test -d "$DEST"; then
  sudo -n mv "$DEST" "$CONFIG/vector_wirepod.bak-$STAMP"
  echo "previous version kept at $CONFIG/vector_wirepod.bak-$STAMP"
fi
sudo -n mkdir -p "$DEST"
sudo -n tar -xzf /tmp/vector_wirepod.deploy.tgz -C "$CONFIG/custom_components"
rm -f /tmp/vector_wirepod.deploy.tgz
sudo -n ha core check
REMOTE
}

main() {
  parse_args "$@"
  command -v ssh >/dev/null || { echo "ssh missing" >&2; exit 3; }
  TGZ="$(mktemp -t vector_wirepod.XXXXXX)"
  COPYFILE_DISABLE=1 tar --no-xattrs --exclude __pycache__ -czf "$TGZ" \
    -C "$HERE/custom_components" vector_wirepod || exit 5
  ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" 'cat > /tmp/vector_wirepod.deploy.tgz' < "$TGZ" || exit 5
  remote_install || exit 5
  echo "installed on $HOST; ha core check passed"
  if [[ $RESTART -eq 1 ]]; then
    ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" 'sudo -n ha core restart' || exit 5
    echo "HA core restarted on $HOST"
  fi
}

main "$@"
