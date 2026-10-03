#!/usr/bin/env bash
# Ubuntu 24.04 amd64; run from a reviewed source checkout as root.
set -euo pipefail
umask 077
exec 9>/run/lock/booking-bootstrap.lock
flock -n 9 || { echo 'Another bootstrap is running.' >&2; exit 1; }
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ $EUID -ne 0 ]]; then
    echo 'Run bootstrap as root.' >&2
    exit 1
fi
. /etc/os-release
if [[ ${ID:-} != ubuntu || ${VERSION_ID:-} != 24.04 || $(uname -m) != x86_64 ]]; then
    echo 'Bootstrap requires Ubuntu 24.04 x86_64; other platforms are unqualified.' >&2
    exit 1
fi
prepare_only=false
for arg in "$@"; do
    if [[ $arg == --prepare-only ]]; then prepare_only=true; fi
done
if ! $prepare_only; then
    systemctl show-environment >/dev/null
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl python3.12-venv
if ! $prepare_only; then
    if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
        # Do not remove/replace a pre-existing container runtime automatically.
        for package in docker.io podman-docker containerd runc; do
            if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed'; then
                echo "Conflicting package $package; review host runtime before bootstrap." >&2
                exit 1
            fi
        done
        install -d -m 0755 /etc/apt/keyrings
        curl --fail --show-error --location --retry 3 \
            https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
        chmod 0644 /etc/apt/keyrings/docker.asc
        cat > /etc/apt/sources.list.d/docker.sources <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
        apt-get update
        apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    fi
    systemctl enable --now docker
    docker info >/dev/null
    docker compose version
fi
exec python3.12 "$source_dir/scripts/bootstrap_server.py" --source "$source_dir" "$@"
