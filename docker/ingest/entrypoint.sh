#!/bin/sh
# Self-contained worker entrypoint: Xvfb for headed Chrome, then the API.
# The VPS needs nothing but the Docker engine.
set -eu

export DISPLAY="${DISPLAY:-:99}"
display_number="${DISPLAY#:}"
rm -f "/tmp/.X${display_number}-lock" "/tmp/.X11-unix/X${display_number}"

Xvfb "$DISPLAY" -screen 0 1920x1080x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
xvfb_pid=$!
trap 'kill "$xvfb_pid" 2>/dev/null || true' EXIT

for _ in 1 2 3 4 5 6 7 8 9 10; do
    if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
        exec "$@"
    fi
    sleep 0.2
done

echo "Xvfb did not become ready on $DISPLAY" >&2
exit 1
