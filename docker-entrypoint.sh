#!/bin/sh
set -eu

mask=${UMASK-022}
case "$mask" in
    [0-7][0-7][0-7]|0[0-7][0-7][0-7]) ;;
    *) printf '%s\n' 'UMASK must be three octal digits or four octal digits starting with 0.' >&2; exit 2 ;;
esac
umask "$mask"
if [ "$#" -eq 0 ]; then
    printf '%s\n' 'docker-entrypoint requires a command.' >&2
    exit 2
fi
exec "$@"
