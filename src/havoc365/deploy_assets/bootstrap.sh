#!/usr/bin/env bash
set -euo pipefail
umask 077

fail() { printf '%s\n' "$*" >&2; exit 1; }
[[ ${EUID} -eq 0 ]] || fail "Run as root using passwordless sudo."
cd /opt/havoc365-deploy
# shellcheck source=/dev/null
source /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] ||
    fail "Use a dedicated Ubuntu 24.04 host. Other distributions need manual installation."

python3 - <<'PY'
import json
from pathlib import Path

config = json.loads(Path("deployment.json").read_text())
saved = Path("/etc/havoc365/deployment.json")
if saved.exists() and json.loads(saved.read_text())["role"] != config["role"]:
    raise SystemExit("Refusing to change an existing authority/gateway role. Back up and migrate manually.")
PY

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git python3 python3-venv caddy e2fsprogs util-linux
install -d -m 700 /etc/havoc365
role=$(python3 -c 'import json; print(json.load(open("deployment.json"))["role"])')

if [[ "$role" == primary ]]; then
    device=$(python3 -c 'import json; print(json.load(open("deployment.json"))["data_device"])')
    if [[ -n "$device" ]]; then
        [[ "$device" == /dev/vdb && -b "$device" ]] ||
            fail "Persistent Alibaba data disk /dev/vdb missing; attach it before retrying."
        if ! mountpoint -q /var/lib/havoc365; then
            [[ ! -d /var/lib/havoc365 || -z "$(ls -A /var/lib/havoc365)" ]] ||
                fail "State directory isn't empty. Mount the retained data disk manually first."
            # Never format a disk with an existing signature, even if it isn't ext4.
            if [[ -z "$(lsblk -no FSTYPE "$device")" ]]; then
                [[ -z "$(wipefs -n "$device")" ]] || fail "Unknown disk signature; mount manually."
                mkfs.ext4 -q "$device"
            fi
            [[ "$(blkid -s TYPE -o value "$device")" == ext4 ]] ||
                fail "Expected ext4 data disk. Mount your persistent disk manually."
            install -d -m 700 /var/lib/havoc365
            uuid=$(blkid -s UUID -o value "$device")
            grep -q "^UUID=$uuid " /etc/fstab ||
                printf 'UUID=%s /var/lib/havoc365 ext4 defaults 0 2\n' "$uuid" >> /etc/fstab
            mount /var/lib/havoc365
        fi
    fi
    id havoc365 >/dev/null 2>&1 ||
        useradd --system --home-dir /var/lib/havoc365 --shell /usr/sbin/nologin havoc365
    install -d -o havoc365 -g havoc365 -m 700 /var/lib/havoc365
    install -d -m 755 /opt/havoc365
    if [[ ! -x /opt/havoc365/tools/bin/uv ]]; then
        python3 -m venv /opt/havoc365/tools
        /opt/havoc365/tools/bin/pip install --disable-pip-version-check --quiet uv
    fi
    specification=$(python3 - <<'PY'
import json
c = json.load(open("deployment.json"))
print("git+" + c["repository"] + "@" + c["ref"])
PY
)
    (
        # The unprivileged service must be able to read/execute the public runtime.
        umask 022
        if [[ ! -x /opt/havoc365/runtime/bin/python ]]; then
            /opt/havoc365/tools/bin/uv venv --python /usr/bin/python3 /opt/havoc365/runtime
        fi
        /opt/havoc365/tools/bin/uv pip install --python /opt/havoc365/runtime/bin/python \
            --reinstall-package havoc365 "$specification"
        chmod -R go+rX /opt/havoc365/runtime
    )
    python3 - <<'PY'
import os
import secrets
from pathlib import Path

path = Path("/etc/havoc365/lifeboat.env")
if not path.exists():
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write("HAVOC365_LIFEBOAT_ADMIN_TOKEN=" + secrets.token_urlsafe(48) + "\n")
        stream.write("HAVOC365_LIFEBOAT_TOKEN=" + secrets.token_urlsafe(48) + "\n")
        stream.write("HAVOC365_LIFEBOAT_LEGACY_ACCESS=0\n")
else:
    values = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    admin = values.get("HAVOC365_LIFEBOAT_ADMIN_TOKEN", "")
    member = values.get("HAVOC365_LIFEBOAT_TOKEN", "")
    if len(admin) < 32 or len(member) < 32 or admin == member:
        raise SystemExit("Existing recovery secrets are invalid. Repair the private environment file.")
    values["HAVOC365_LIFEBOAT_LEGACY_ACCESS"] = "0"
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
path.chmod(0o600)
PY
    install -m 644 havoc365.service /etc/systemd/system/havoc365.service
    systemctl daemon-reload
    systemctl enable havoc365 >/dev/null
    systemctl restart havoc365
    ready=0
    for _ in {1..30}; do
        if curl --fail --silent http://127.0.0.1:8080/api/state >/dev/null; then
            ready=1
            break
        fi
        sleep 1
    done
    [[ "$ready" == 1 ]] || fail "Lifeboat isn't responsive. Inspect journalctl -u havoc365."
fi

caddy validate --config ./Caddyfile --adapter caddyfile
install -m 644 Caddyfile /etc/caddy/Caddyfile
systemctl enable caddy >/dev/null
systemctl restart caddy
systemctl is-active --quiet caddy || fail "Caddy failed. Inspect journalctl -u caddy."
install -m 600 deployment.json /etc/havoc365/deployment.json
hostname=$(python3 -c 'import json; print(json.load(open("deployment.json"))["hostname"])')
curl --fail --silent --retry 12 --retry-delay 5 --retry-all-errors --max-time 10 \
    "https://$hostname/api/state" >/dev/null ||
    fail "Public HTTPS verification failed. Fix DNS/ports/TLS (and primary reachability), then rerun."
if [[ "$role" == primary ]]; then
    watch_enabled=$(python3 -c 'import json; print(bool(json.load(open("deployment.json")).get("watch_urls")))')
    if [[ "$watch_enabled" == True ]]; then
        install -m 644 havoc365-watch.service /etc/systemd/system/havoc365-watch.service
        systemctl daemon-reload
        systemctl enable havoc365-watch >/dev/null
        systemctl restart havoc365-watch
        systemctl is-active --quiet havoc365-watch ||
            fail "Watcher failed. Inspect journalctl -u havoc365-watch."
        printf 'Automatic activation watcher enabled for explicit HTTPS probes; no SMS.\n'
    else
        if [[ -f /etc/systemd/system/havoc365-watch.service ]]; then
            systemctl disable --now havoc365-watch >/dev/null
        fi
        printf 'Manual-only activation: no watcher configured.\n'
    fi
fi
printf 'Lifeboat host verified over HTTPS.\n'
