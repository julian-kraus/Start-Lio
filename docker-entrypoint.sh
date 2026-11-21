#!/bin/sh
# Run as the owner of the mounted data directory, so a bind mount stays writable on any host
# without chowning the host's files. A fresh named volume (owned by root) is handed to the tracker user.
set -e
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$TRACKER_DATA_DIR"
    uid=$(stat -c %u "$TRACKER_DATA_DIR")
    gid=$(stat -c %g "$TRACKER_DATA_DIR")
    if [ "$uid" = "0" ]; then
        chown tracker:tracker "$TRACKER_DATA_DIR"
        uid=$(id -u tracker)
        gid=$(id -g tracker)
    fi
    exec setpriv --reuid="$uid" --regid="$gid" --clear-groups "$@"
fi
exec "$@"
