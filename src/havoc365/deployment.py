"""Offline-first Lifeboat deployment planning and explicitly confirmed installation."""

import argparse
import base64
import io
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Collection, Mapping, Sequence
from importlib.resources import files
from pathlib import Path
from string import Template
from urllib.parse import urlsplit

from havoc365.errors import HavocError
from havoc365.ui import UI

ASSETS = Path(str(files("havoc365").joinpath("deploy_assets")))
PROVIDERS = ("aws", "gcp", "alibaba", "ssh")
DEFAULT_REPOSITORY = "https://github.com/JGalego/havoc365.git"
FIELDS = {
    "aws": ("region", "image", "instance_type", "vpc_id", "subnet_id"),
    "gcp": ("project", "region", "zone", "image", "instance_type"),
    "alibaba": ("region", "zone", "image", "instance_type"),
    "ssh": ("host",),
}
DEFAULTS = {
    "aws": {"instance_type": "t3.micro"},
    "gcp": {
        "image": "ubuntu-os-cloud/ubuntu-2404-lts-amd64",
        "instance_type": "e2-micro",
    },
    "alibaba": {"instance_type": "ecs.t6-c1m1.large"},
    "ssh": {},
}
# Country/EU groups, not a sovereignty or capacity guarantee. Sources: deploy/README.md.
REGION_GROUPS = {
    "aws": (
        ("eu-west-1", "eu-central-1", "eu-west-3", "eu-north-1"),
        ("us-east-1", "us-east-2", "us-west-2", "us-west-1"),
        ("ap-northeast-1", "ap-northeast-3"),
        ("ap-south-1", "ap-south-2"),
        ("ap-southeast-2", "ap-southeast-4"),
        ("ca-central-1", "ca-west-1"),
    ),
    "gcp": (
        ("europe-west1", "europe-west3"),
        ("us-central1", "us-east1"),
    ),
    "alibaba": (
        ("eu-central-1", "eu-west-2"),
        ("us-east-1", "us-west-1"),
        ("cn-hangzhou", "cn-shanghai", "cn-beijing"),
    ),
}
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}\Z")


def suggest_region(target: Mapping[str, str], others: Sequence[Mapping[str, str]]) -> str:
    provider = target["provider"]
    bound_fields = {
        "aws": ("image", "vpc_id", "subnet_id"),
        "gcp": ("zone",),
        "alibaba": ("zone", "image"),
    }
    if target.get("region") or any(target.get(key) for key in bound_fields.get(provider, ())):
        return ""
    used = [
        other["region"] for other in others if other["provider"] == provider and other.get("region")
    ]
    if not used:
        return ""
    for group in REGION_GROUPS.get(provider, ()):
        if used[0] in group:
            return next((region for region in group if region not in used), "")
    return ""


def recovery_warnings(config: dict) -> list[str]:
    targets = config["targets"]
    warnings = [
        f"{targets[0]['name']} is the only database authority. Other regions/providers do not "
        "protect its data or provide failover; arrange off-site backups and a restore procedure."
        if config["topology"] == "shared"
        else "Independent rooms do not synchronize data or access. They are not recovery copies; "
        "decide which URL people should use during an outage."
    ]
    locations: dict[tuple[str, str], list[str]] = {}
    for target in targets:
        provider = target["provider"]
        if provider == "ssh":
            continue
        locations.setdefault((provider, target["region"]), []).append(target["name"])
    for (provider, region), names in locations.items():
        if len(names) > 1:
            warnings.append(
                f"{', '.join(names)} share {provider}/{region}. A regional outage can affect "
                "all of them; choose distinct regions unless this placement is deliberate."
            )
    if any(target["provider"] == "ssh" for target in targets):
        warnings.append(
            "SSH failure domains are unknown. Verify independent sites, power, network and "
            "storage; different host addresses do not establish separation."
        )
    warnings.append(
        "Not checked: recovery time/data-loss objectives (RTO/RPO), off-site backup freshness, "
        "restore tests, DNS and recovery credentials independent of Microsoft 365. "
        "Arrange and rehearse these before relying on recovery."
    )
    return warnings


def configure_parser(boats: argparse._SubParsersAction) -> None:
    deploy = boats.add_parser(
        "deploy",
        help="Plan, check or install Lifeboat on cloud VMs or SSH hosts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Default: full-screen offline planner; Esc cancels without saving.\n"
            "  --config deploy/generated/config.json --check\n"
            "      Read-only SSH checks and cloud planning; no installation.\n"
            "  --config deploy/generated/config.json --apply\n"
            "      Check every target, then provision/install after explicit APPLY approval."
        ),
    )
    deploy.add_argument("--config", type=Path, help="Validated saved deployment JSON; no secrets")
    deploy.add_argument("--output", type=Path, default=Path("deploy/generated"))
    deploy.add_argument(
        "--assets",
        type=Path,
        default=ASSETS,
        help="Optional override for bundled deployment assets",
    )
    mode = deploy.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan", action="store_true", help="Render locally without network (default)"
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Run read-only SSH/cloud pre-flight; may contact providers, never installs",
    )
    mode.add_argument(
        "--apply", action="store_true", help="Provision and install; may incur charges"
    )
    deploy.add_argument("--confirm", choices=["APPLY"], help="Explicit automation approval")
    deploy.add_argument(
        "--non-interactive", action="store_true", help="Never prompt; requires --config"
    )


def wizard() -> dict:
    from havoc365.deploy_tui import run_wizard

    return run_wizard()


def _hostname(value: object, label: str, *, public: bool = True, used: Collection[str] = ()) -> str:
    if not isinstance(value, str) or len(value) > 253:
        raise HavocError(f"{label} must be a DNS hostname.")
    if value != value.lower() or not all(_LABEL.fullmatch(part) for part in value.split(".")):
        raise HavocError(f"{label} must be a lowercase DNS hostname, without scheme or port.")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        address = None
    if public and (
        address is not None
        or "." not in value
        or value.endswith((".localhost", ".local", ".internal", ".invalid", ".test"))
    ):
        raise HavocError(f"{label} needs a public DNS name for HTTPS, not an IP or local name.")
    if value in used:
        raise HavocError(f"{label} is already used by another target. Choose a unique hostname.")
    return value


def _name(value: object, used: Collection[str] = ()) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,39}", value):
        raise HavocError("Target name must be 2-40 lowercase letters, digits or hyphens.")
    if value in used:
        raise HavocError("Target names and public hostnames must be unique.")
    return value


def _key_path(value: object) -> Path:
    if not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n"):
        raise HavocError("Set ssh_key to an existing local SSH private key path.")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise HavocError(
            "SSH private key is missing. Create a key with ssh-keygen and set ssh_key."
        )
    if path.stat().st_mode & 0o077:
        raise HavocError("SSH private key permissions are too open. Run chmod 600 on the key.")
    return path


def _cidr(value: object) -> str:
    try:
        cidr = str(ipaddress.IPv4Network(value, strict=True))
    except (ValueError, TypeError) as error:
        raise HavocError(
            "Set ssh_cidr to your SSH source IPv4 CIDR (for example 203.0.113.1/32)."
        ) from error
    if cidr == "0.0.0.0/0":
        raise HavocError(
            "Do not expose SSH to 0.0.0.0/0. Set ssh_cidr to your administration network."
        )
    return cidr


def _repository(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", value
    ):
        raise HavocError("Repository must be a public https://github.com/owner/repository URL.")
    return value


def _ref(value: object) -> str:
    if not isinstance(value, str) or not _VALUE.fullmatch(value) or ".." in value:
        raise HavocError(
            "Git ref must be a branch, tag or commit SHA without shell metacharacters."
        )
    return value


def _user(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", value):
        raise HavocError("SSH user must be a Linux username, not an SSH option.")
    return value


def _port(value: str) -> None:
    if not value.isascii() or not value.isdigit() or len(value) > 5 or not 1 <= int(value) <= 65535:
        raise HavocError("SSH port must be 1-65535.")


def _field(
    value: object,
    field: str,
    provider: str,
    *,
    port: int = 22,
    endpoints: Collection[tuple[str, int]] = (),
) -> str:
    if not isinstance(value, str) or not _VALUE.fullmatch(value) or ".." in value:
        raise HavocError(f"Set a valid {field}; see deploy/README.md.")
    if provider == "aws" and field in ("image", "vpc_id", "subnet_id"):
        prefix = {"image": "ami", "vpc_id": "vpc", "subnet_id": "subnet"}[field]
        if not re.fullmatch(rf"{prefix}-[0-9a-f]+", value):
            raise HavocError(f"AWS {field} must be a real {prefix}-... resource ID.")
    if provider == "ssh" and field == "host":
        try:
            ipaddress.IPv4Address(value)
        except ValueError:
            _hostname(value, "SSH host", public=False)
        if (value, port) in endpoints:
            raise HavocError("An existing SSH host/port can only run one target.")
    return value


def validate_origin(value: object) -> str:
    """Accept exactly a public HTTPS origin, never an upstream URL with credentials."""
    if not isinstance(value, str):
        raise HavocError("Primary must be a public HTTPS origin.")
    try:
        parsed = urlsplit(value)
        hostname = _hostname(parsed.hostname, "Primary hostname")
        if value != f"https://{hostname}" or parsed.port is not None:
            raise ValueError
    except (ValueError, TypeError) as error:
        raise HavocError(
            "Primary must be an HTTPS public origin with no port, path, query or credentials."
        ) from error
    return value


def _keys(value: dict, allowed: set[str], label: str) -> None:
    if not all(isinstance(key, str) for key in value) or set(value) - allowed:
        raise HavocError(f"Unknown {label} fields. Do not put credentials in deployment configs.")


def _public_key(key_path: Path) -> str:
    try:
        parts = key_path.with_suffix(key_path.suffix + ".pub").read_text().split()
        if len(parts) < 2 or parts[0] not in ("ssh-ed25519", "ssh-rsa"):
            raise ValueError
        blob = base64.b64decode(parts[1], validate=True)
        fields = []
        while blob:
            if len(blob) < 4:
                raise ValueError
            length = int.from_bytes(blob[:4], "big")
            if not 0 < length <= len(blob) - 4:
                raise ValueError
            fields.append(blob[4 : 4 + length])
            blob = blob[4 + length :]
        if not fields or fields[0] != parts[0].encode():
            raise ValueError
        if parts[0] == "ssh-ed25519":
            if len(fields) != 2 or len(fields[1]) != 32:
                raise ValueError
        elif len(fields) != 3 or len(fields[2]) < 256:
            raise ValueError
        return " ".join(parts[:2])
    except (OSError, ValueError, UnicodeError) as error:
        raise HavocError(
            "A valid matching .pub SSH key is required for cloud VMs (Ed25519 or RSA 2048+). "
            "Use ssh-keygen -y to derive the public key."
        ) from error


def _watch_config(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise HavocError(
            "watch must be an object containing urls, or omit it for manual activation."
        )
    _keys(raw, {"urls"}, "watch")
    urls = raw.get("urls", [])
    if not isinstance(urls, list) or len(urls) > 20:
        raise HavocError("watch.urls must contain at most 20 explicit public HTTPS probe URLs.")
    for url in urls:
        try:
            if (
                not isinstance(url, str)
                or not re.fullmatch(r"https://[A-Za-z0-9._~:/!$&'()*+,;=%-]+", url)
                or len(url) > 2048
                or re.search(r"%(?![0-9a-fA-F]{2})", url)
            ):
                raise ValueError
            parsed = urlsplit(url)
            validate_origin(f"{parsed.scheme}://{parsed.hostname}")
            if parsed.netloc not in (parsed.hostname, f"{parsed.hostname}:443"):
                raise ValueError
        except (ValueError, HavocError) as error:
            raise HavocError(
                "Probe URLs must be public HTTPS URLs on port 443, with no credentials, "
                "query, fragment, whitespace or malformed escapes. Use public health paths."
            ) from error
    if len(set(urls)) != len(urls):
        raise HavocError("watch.urls must not contain duplicate probe URLs.")
    return {"urls": list(urls)}


def _systemd_argv(argv: list[str]) -> str:
    # ExecStart is not a shell: escape systemd's specifier and environment expansion too.
    return " ".join(
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        + '"'
        for value in argv
    )


def validate_config(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise HavocError("Deployment config must be a JSON object; see deploy/README.md.")
    _keys(
        raw,
        {"version", "topology", "repository", "ref", "ssh_key", "ssh_cidr", "targets", "watch"},
        "deployment",
    )
    if type(raw.get("version", 1)) is not int or raw.get("version", 1) != 1:
        raise HavocError("Unsupported deployment config version. Use version 1.")
    topology = raw.get("topology", "shared")
    if topology not in ("shared", "independent"):
        raise HavocError("Topology must be shared or independent.")
    watch = _watch_config(raw.get("watch", {}))
    repository = _repository(raw.get("repository", DEFAULT_REPOSITORY))
    ref = _ref(raw.get("ref", "main"))
    key_path = _key_path(raw.get("ssh_key"))
    cidr = _cidr(raw.get("ssh_cidr", ""))
    targets = raw.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 20:
        raise HavocError("Configure between 1 and 20 deployment targets.")
    clean = []
    names, hostnames, endpoints = set(), set(), set()
    for target in targets:
        if not isinstance(target, dict) or target.get("provider") not in PROVIDERS:
            raise HavocError("Each target provider must be aws, gcp, alibaba or ssh.")
        provider = target["provider"]
        _keys(
            target,
            {"provider", "name", "hostname", "ssh_user", "ssh_port", *FIELDS[provider]},
            "target",
        )
        item = {**DEFAULTS[provider], **target}
        name = _name(item.get("name"), names)
        hostname = _hostname(item.get("hostname"), "Public hostname", used=hostnames)
        names.add(name)
        hostnames.add(hostname)
        _user(item.setdefault("ssh_user", "root" if provider == "alibaba" else "ubuntu"))
        port = item.setdefault("ssh_port", 22)
        if type(port) is not int or not 1 <= port <= 65535 or (provider != "ssh" and port != 22):
            raise HavocError("SSH port must be 1-65535; newly provisioned VMs use port 22.")
        for field in FIELDS[provider]:
            _field(item.get(field), field, provider, port=port, endpoints=endpoints)
        if provider == "ssh":
            host = item["host"]
            endpoint = (host, port)
            endpoints.add(endpoint)
        clean.append(item)
    if any(item["provider"] != "ssh" for item in clean):
        _public_key(key_path)
    return {
        "version": 1,
        "topology": topology,
        "repository": repository,
        "ref": ref,
        "ssh_key": str(key_path),
        "ssh_cidr": cidr,
        "targets": clean,
        "watch": watch,
    }


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise HavocError(
            f"Refusing to overwrite symlink {path}. Choose a private output directory."
        )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(content)


def _json(value: object) -> str:
    return json.dumps(value, indent=2) + "\n"


def render(config: dict, output: Path, assets: Path = ASSETS) -> None:
    """Render a validated config without probing SSH, cloud accounts or public DNS."""
    required = [
        "bootstrap.sh",
        "templates/Caddyfile.primary",
        "templates/Caddyfile.gateway",
        "templates/havoc365.service",
        "templates/havoc365-watch.service",
        *[
            f"terraform/{item['provider']}/main.tf"
            for item in config["targets"]
            if item["provider"] != "ssh"
        ],
    ]
    if any(not (assets / filename).is_file() for filename in required):
        raise HavocError(
            "Deployment assets are missing. Reinstall havoc365 or pass --assets PATH to templates."
        )
    if output.is_symlink():
        raise HavocError(
            "Output directory cannot be a symlink. Choose a private deployment directory."
        )
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    saved = output / "config.json"
    if saved.exists():
        try:
            previous = json.loads(saved.read_text())
        except (OSError, ValueError) as error:
            raise HavocError(
                "Saved deployment config is unreadable. Recover it before reapplying."
            ) from error

        # Changing a role or removing a target would abandon authoritative data.
        def identity(c):
            if not isinstance(c, dict) or not isinstance(c.get("targets"), list):
                raise HavocError("Saved config has an invalid shape. Recover it before reapplying.")
            return c.get("topology"), [
                (t.get("name"), t.get("provider")) for t in c["targets"] if isinstance(t, dict)
            ]

        if identity(previous) != identity(config):
            raise HavocError(
                "Target order, names, providers or topology changed. Use a new output directory; "
                "back up and migrate existing rooms manually. Nothing was destroyed."
            )
    primary = validate_origin(f"https://{config['targets'][0]['hostname']}")
    for index, target in enumerate(config["targets"]):
        directory = output / target["name"]
        gateway = config["topology"] == "shared" and index > 0
        role = "gateway" if gateway else "primary"
        values = {
            "hostname": target["hostname"],
            "origin": f"https://{target['hostname']}",
            "upstream": primary,
            "upstream_host": urlsplit(primary).hostname,
        }
        caddy = Template((assets / f"templates/Caddyfile.{role}").read_text()).substitute(values)
        _write(directory / "Caddyfile", caddy)
        _write(directory / "bootstrap.sh", (assets / "bootstrap.sh").read_text())
        _write(directory / "havoc365.service", (assets / "templates/havoc365.service").read_text())
        watch_urls = [] if gateway else config["watch"]["urls"]
        argv = ["/opt/havoc365/runtime/bin/havoc365", "watch"]
        for url in watch_urls:
            argv.extend(["--url", url])
        argv.extend(["--lifeboat-url", values["origin"]])
        watcher = (
            Template((assets / "templates/havoc365-watch.service").read_text()).substitute(
                exec_start=_systemd_argv(argv)
            )
            if watch_urls
            else ""
        )
        _write(directory / "havoc365-watch.service", watcher)
        _write(
            directory / "deployment.json",
            _json(
                {
                    "role": role,
                    "hostname": target["hostname"],
                    "repository": config["repository"],
                    "ref": config["ref"],
                    "watch_urls": watch_urls,
                    "data_device": "/dev/vdb"
                    if target["provider"] == "alibaba" and not gateway
                    else "",
                }
            ),
        )
        if target["provider"] != "ssh":
            provider = target["provider"]
            _write(directory / "main.tf", (assets / f"terraform/{provider}/main.tf").read_text())
            variables = {field: target[field] for field in FIELDS[provider]}
            key = Path(config["ssh_key"])
            variables.update(
                {
                    "name": target["name"],
                    "ssh_user": target["ssh_user"],
                    "ssh_cidr": config["ssh_cidr"],
                    "public_key": _public_key(key),
                }
            )
            if provider == "alibaba":
                variables["state_disk"] = "no" if gateway else "yes"
            _write(directory / "terraform.tfvars.json", _json(variables))
            _write(
                directory / "variables.tf.json",
                _json({"variable": {name: {"type": "string"} for name in variables}}),
            )
    _write(saved, _json(config))


def _command(
    argv: list[str], label: str, *, payload: bytes | None = None, timeout: int = 3600
) -> bytes:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("TF_VAR_", "TF_CLI_ARGS", "TF_LOG"))
    }
    env.update(TF_INPUT="0", TF_IN_AUTOMATION="1", TF_LOG="OFF")
    try:
        result = subprocess.run(
            argv, input=payload, capture_output=True, check=False, env=env, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HavocError(
            f"{label} could not run. Check tool installation, connectivity and permissions; "
            "rerun the same config/output directory. No automatic cleanup was attempted."
        ) from error
    if result.returncode:
        raise HavocError(
            f"{label} failed (exit {result.returncode}). Check provider credentials/quotas or SSH "
            "known_hosts, sudo and host journals. Output is suppressed to protect credentials. "
            "Infrastructure may remain billable; fix the issue and rerun. No automatic cleanup."
        )
    return result.stdout


def preflight(config: dict) -> str | None:
    rows = []
    ssh = shutil.which("ssh")
    rows.append(
        (
            "PASS" if ssh else "FAIL",
            "OpenSSH",
            ssh or "OpenSSH client is missing. Install openssh-client.",
        )
    )
    engine = None
    if any(target["provider"] != "ssh" for target in config["targets"]):
        engine = shutil.which("tofu") or shutil.which("terraform")
        if not engine:
            rows.append(("FAIL", "Cloud engine", "Install OpenTofu (tofu) or Terraform 1.6+."))
        else:
            try:
                version = json.loads(
                    _command([engine, "version", "-json"], "Cloud engine version", timeout=30)
                )
                number = version["terraform_version"]
                match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", number)
                if not match or tuple(map(int, match.group(1, 2))) < (1, 6):
                    raise ValueError
                rows.append(("PASS", "Cloud engine", f"{engine}: {number}"))
            except (ValueError, KeyError, TypeError) as error:
                raise HavocError(
                    "Cannot verify OpenTofu/Terraform 1.6+. Check the installed version."
                ) from error
    _check_results(rows, "Local pre-flight failed. Install the missing tools and rerun.")
    return engine


def _check_results(rows: list[tuple[str, str, str]], failure: str) -> None:
    UI().table("Pre-flight", ("Status", "Check", "Details"), rows)
    failures = [f"{label}: {detail}" for status, label, detail in rows if status == "FAIL"]
    if failures:
        raise HavocError(failure + "\n" + "\n".join(failures))


def _bundle(directory: Path) -> bytes:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as stream:
        for name in (
            "bootstrap.sh",
            "Caddyfile",
            "havoc365.service",
            "havoc365-watch.service",
            "deployment.json",
        ):
            content = (directory / name).read_bytes()
            info = tarfile.TarInfo(name)
            info.mode = 0o600
            info.size = len(content)
            stream.addfile(info, io.BytesIO(content))
    return archive.getvalue()


def _ssh(config: dict, target: dict, host: str) -> list[str]:
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=2",
        "-p",
        str(target["ssh_port"]),
        "-i",
        config["ssh_key"],
        f"{target['ssh_user']}@{host}",
    ]


def _host_checks(config: dict, target: dict, host: str) -> list[tuple[str, str, str]]:
    probe = b"""\
if ( . /etc/os-release; [ "$ID" = ubuntu ] && [ "$VERSION_ID" = 24.04 ] ); then
    printf 'PASS ubuntu\\n'
else printf 'FAIL ubuntu\\n'; fi
if python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
    printf 'PASS python\\n'
else printf 'FAIL python\\n'; fi
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
    printf 'PASS systemd\\n'
else printf 'FAIL systemd\\n'; fi
if df -Pk /var/lib /opt | awk 'NR > 1 {n++; if ($4 < 1048576) bad=1} END {exit(n < 2 || bad)}'; then
    printf 'PASS storage\\n'
else printf 'FAIL storage\\n'; fi
"""
    result = _command(
        [*_ssh(config, target, host), "sudo -n /bin/sh -s"],
        f"{target['name']}: SSH host check (verify known_hosts, key and sudo -n first)",
        payload=probe,
        timeout=45,
    )
    descriptions = {
        "ubuntu": "Dedicated Ubuntu 24.04 required",
        "python": "Python 3.12+ required before bootstrap",
        "systemd": "Booted systemd host required",
        "storage": "1 GiB free on /var/lib and /opt required; durability not verified",
    }
    try:
        lines = [line.split() for line in result.decode("ascii").splitlines()]
        checks = {}
        for line in lines:
            if len(line) != 2 or line[0] not in ("PASS", "FAIL") or line[1] in checks:
                raise ValueError
            checks[line[1]] = line[0]
        if set(checks) != set(descriptions):
            raise ValueError
    except (ValueError, UnicodeError) as error:
        raise HavocError(
            f"{target['name']}: cannot interpret SSH pre-flight output. "
            "Check login banners and sudo on a private terminal; raw output was suppressed."
        ) from error
    return [
        ("PASS", f"{target['name']}: SSH / sudo", f"Trusted-key, noninteractive access to {host}"),
        *[
            (checks[name], f"{target['name']}: {name}", description)
            for name, description in descriptions.items()
        ],
    ]


def _cloud_plan(target: dict, directory: Path, engine: str | None) -> str:
    if engine is None:
        raise HavocError("Install OpenTofu or Terraform before checking cloud targets.")
    tf = [engine, f"-chdir={directory}"]
    label = target["name"]
    ui = UI()
    with ui.working(f"{label}: reading cloud plan (no resource changes)..."):
        _command([*tf, "init", "-input=false", "-no-color"], f"{label}: provider initialization")
        _command([*tf, "validate", "-no-color"], f"{label}: template validation")
        _command(
            [*tf, "plan", "-input=false", "-no-color", "-out=deployment.tfplan"],
            f"{label}: infrastructure plan",
        )
        try:
            plan = json.loads(
                _command([*tf, "show", "-json", "deployment.tfplan"], f"{label}: read plan")
            )
            changes = plan.get("resource_changes", [])
            if not isinstance(changes, list) or plan.get("errored", False):
                raise ValueError
            counts = {"create": 0, "update": 0, "read": 0, "no-op": 0}
            for change in changes:
                actions = change["change"]["actions"]
                if (
                    not isinstance(actions, list)
                    or not actions
                    or not all(isinstance(action, str) for action in actions)
                ):
                    raise ValueError
                if "delete" in actions:
                    raise HavocError(
                        f"{label}: would delete/replace resources. Refusing destructive apply. "
                        "Back up the room and review migration manually."
                    )
                if set(actions) - {"no-op", "create", "read", "update"}:
                    raise ValueError
                for action in actions:
                    counts[action] += 1
        except (ValueError, AttributeError, KeyError, TypeError) as error:
            raise HavocError(
                f"{label}: cannot parse infrastructure plan. Inspect it before applying."
            ) from error
    return (
        f"{counts['create']} create, {counts['update']} update, "
        f"{counts['no-op']} unchanged; no delete/replace"
    )


def check_targets(config: dict, output: Path, engine: str | None) -> None:
    rows = []
    for target in config["targets"]:
        try:
            if target["provider"] == "ssh":
                with UI().working(f"{target['name']}: checking SSH host (read-only)..."):
                    rows.extend(_host_checks(config, target, target["host"]))
            else:
                summary = _cloud_plan(target, (output / target["name"]).resolve(), engine)
                rows.append(("PASS", f"{target['name']}: cloud plan", summary))
                rows.append(
                    (
                        "PENDING",
                        f"{target['name']}: host",
                        "SSH/OS/storage checks run after VM allocation, before any installation",
                    )
                )
        except HavocError as error:
            rows.append(("FAIL", target["name"], str(error)))
    _check_results(rows, "Pre-flight failed. No infrastructure or hosts were changed.")
    print("Pre-flight does not prove DNS/TLS, inbound ports, disk durability or provider capacity.")


def apply(config: dict, output: Path, engine: str | None) -> None:
    check_targets(config, output, engine)
    hosts = {}
    for target in config["targets"]:
        directory = (output / target["name"]).resolve()
        host = target.get("host")
        if target["provider"] != "ssh":
            tf = [str(engine), f"-chdir={directory}"]
            label = target["name"]
            print(f"{label}: applying the checked cloud plan; resources may become billable.")
            _command(
                [*tf, "apply", "-input=false", "-no-color", "deployment.tfplan"],
                f"{label}: infrastructure apply",
            )
            try:
                host = json.loads(
                    _command([*tf, "output", "-json", "host"], f"{label}: VM address")
                )
                if not isinstance(host, str) or not ipaddress.IPv4Address(host).is_global:
                    raise ValueError
            except (ValueError, TypeError) as error:
                raise HavocError(
                    f"{label}: no public IPv4 address returned. Inspect VM networking."
                ) from error
            print(
                f"{label}: allocated {host}. Set DNS for {target['hostname']} "
                "and verify its SSH host key."
            )
            try:
                cloud_checks = _host_checks(config, target, host)
            except HavocError as error:
                cloud_checks = [("FAIL", label, str(error))]
            _check_results(
                cloud_checks,
                "Host pre-flight failed after VM allocation. Resources may remain billable. "
                "No host installation was started; fix DNS/SSH/host prerequisites and rerun.",
            )
        hosts[target["name"]] = str(host)
    for target in config["targets"]:
        directory = (output / target["name"]).resolve()
        host = hosts[target["name"]]
        print(f"{target['name']}: installing on {host}; DNS must point here for HTTPS.")
        # The remote command is constant; user input is only in argv or validated archive data.
        remote = (
            "sudo -n /bin/sh -c 'umask 077; "
            "install -d -m 700 /opt/havoc365-deploy && "
            "cd /opt/havoc365-deploy && tar --no-same-owner -xf - && "
            "exec /bin/bash ./bootstrap.sh'"
        )
        _command(
            [*_ssh(config, target, str(host)), remote],
            f"{target['name']}: SSH/bootstrap (verify host key and passwordless sudo first)",
            payload=_bundle(directory),
        )
        print(f"Installed: https://{target['hostname']} (verify DNS/TLS and /readyz).")
        if config["topology"] == "independent" or target is config["targets"][0]:
            retrieval = [*_ssh(config, target, str(host)), "sudo -n cat /etc/havoc365/lifeboat.env"]
            print(
                "Private recovery only — run on a trusted terminal; never paste output into logs:"
            )
            print("  " + shlex.join(retrieval))
    print(
        "Sign in with the recovery admin key at the chosen URL, create two named admins and "
        "member invitations there, and distribute them privately. No invitations were delivered."
    )


def run_deployment(args: argparse.Namespace) -> int:
    try:
        if args.config:
            try:
                raw = json.loads(args.config.read_text())
            except (OSError, ValueError) as error:
                raise HavocError(
                    "Cannot read deployment JSON. Check --config and deploy/README.md."
                ) from error
        elif (
            args.non_interactive
            or not sys.stdin.isatty()
            or not sys.stdout.isatty()
            or os.environ.get("TERM") == "dumb"
        ):
            raise HavocError(
                "Noninteractive deployment requires --config PATH; default is offline --plan. "
                "The full-screen planner needs an interactive input/output terminal."
            )
        else:
            raw = wizard()
        config = validate_config(raw)
        render(config, args.output, args.assets)
        if args.config:
            UI().table(
                "Deployment review",
                ("Target", "Provider / role", "URL / activation"),
                [
                    (
                        target["name"],
                        target["provider"]
                        + (
                            " / gateway"
                            if config["topology"] == "shared" and index
                            else " / authority"
                        )
                        + (f" / {target['region']}" if target["provider"] != "ssh" else ""),
                        f"https://{target['hostname']} / "
                        + (
                            "forwarded"
                            if config["topology"] == "shared" and index
                            else "automatic"
                            if config["watch"]["urls"]
                            else "manual"
                        ),
                    )
                    for index, target in enumerate(config["targets"])
                ],
            )
            for warning in recovery_warnings(config):
                print(f"Recovery warning: {warning}")
        print(f"Saved private, credential-free plan: {args.output / 'config.json'}")
        if args.config:
            print(
                f"Bootstrap ref: {config['ref']} "
                "(main/branches are mutable, not reproducible pins)."
            )
            print(
                "Shared state: first target is the only authority; others are gateways. "
                "Primary is a SPOF. No replication, automatic failover or cross-domain SSO."
                if config["topology"] == "shared"
                else "Independent rooms: separate databases, people, invites and recovery keys."
            )
            print(
                "Automatic activation: supervised HTTPS watcher on each authority (not gateways); "
                "manual holds override it. No SMS or invite delivery is enabled."
                if config["watch"]["urls"]
                else "Manual-only: no probe URLs or installed watcher; activation is NOT automatic."
            )
        if not args.apply and not args.check:
            print("Plan only: no cloud/SSH commands ran. Review files and deploy/README.md first.")
            command = shlex.join(
                [
                    "havoc365",
                    "lifeboat",
                    "deploy",
                    "--config",
                    str(args.output / "config.json"),
                    "--output",
                    str(args.output),
                ]
            )
            print(f"Next, read-only: {command} --check")
            print(f"Install after review: {command} --apply")
            return 0
        engine = preflight(config)
        if args.check:
            check_targets(config, args.output, engine)
            print(
                "Read-only checks finished. No infrastructure or hosts were changed; "
                "review PENDING checks."
            )
            return 0
        print(
            "APPLY creates/updates billable VMs and configures dedicated hosts (Caddy/systemd). "
            "Existing Caddy configuration is replaced. No destructive cleanup is automatic."
        )
        confirmation = args.confirm
        if confirmation is None and not args.non_interactive and sys.stdin.isatty():
            confirmation = input("Type APPLY to proceed: ").strip()
        if confirmation != "APPLY":
            print("Cancelled. No infrastructure or hosts were changed.")
            return 1
        apply(config, args.output, engine)
        return 0
    except (EOFError, KeyboardInterrupt):
        print(
            "\nCancelled. No automatic cleanup; review any resources already created."
            if args.apply
            else "\nCancelled. No infrastructure or hosts were changed."
        )
        return 1
    except OSError as error:
        raise HavocError(
            "Cannot read/write deployment assets. Check file permissions and paths; "
            "use a private output directory."
        ) from error
