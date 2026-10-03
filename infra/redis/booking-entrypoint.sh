#!/bin/sh
# The generated shell command must enter the official privilege-drop branch.
set -eu
if [ "${1:-}" = sh ] && [ "${2:-}" = -c ] &&
   [ "${3:-}" = 'exec redis-server --appendonly yes --requirepass "$REDIS_PASSWORD"' ]; then
    exec /usr/local/bin/docker-entrypoint.sh redis-server \
        --appendonly yes --requirepass "${REDIS_PASSWORD:?Missing Redis password}"
fi
exec /usr/local/bin/docker-entrypoint.sh "$@"
