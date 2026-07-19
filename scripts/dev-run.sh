#!/usr/bin/env bash
# Run EchoList against an isolated dev sandbox instead of your real device.
#
# Redirects $HOME for the app's process only, so ~/.echolist (default.json,
# backups) never touches your real config, and gives you a plain local
# folder to use as the "device" destination instead of a real Echo Mini —
# safe to wipe and reseed any time.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV_HOME="$ROOT/dev-data/home"
DEV_SOURCE="$ROOT/dev-data/source"
DEV_DEVICE="$ROOT/dev-data/device"

# Redirecting HOME below would also hide anything else that lives under the
# real HOME — pip's --user site-packages (if not using a venv) and X11 auth.
# Capture those from the real HOME first so the sandboxed process still sees them.
REAL_USER_SITE="$(python3 -c 'import site; print(site.getusersitepackages())' 2>/dev/null || true)"
REAL_XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"

mkdir -p "$DEV_HOME" "$DEV_SOURCE" "$DEV_DEVICE"
python3 "$ROOT/scripts/seed_dev_library.py"

echo "Dev sandbox:"
echo "  HOME (config/backups): $DEV_HOME"
echo "  Source library:        $DEV_SOURCE"
echo "  Fake device folder:    $DEV_DEVICE"
echo
echo "First run: in Settings, browse Source to the library path above and"
echo "Dest to the fake device folder above (confirm 'local folder' prompt)."
echo "Real-device auto-detect is disabled in this sandbox — a plugged-in"
echo "Echo Mini will never be picked up here."
echo

HOME="$DEV_HOME" \
XAUTHORITY="$REAL_XAUTHORITY" \
PYTHONPATH="$REAL_USER_SITE${PYTHONPATH:+:$PYTHONPATH}" \
ECHOLIST_DISABLE_DEVICE_DETECT=1 \
exec python3 -m echolist
