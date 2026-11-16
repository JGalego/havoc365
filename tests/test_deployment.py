import argparse
import base64
import copy
import io
import json
import os
import shlex
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from havoc365 import cli
from havoc365 import deployment as deploy
from havoc365.cli import parser as cli_parser
from havoc365.errors import HavocError


@pytest.fixture
def config(tmp_path):
    key = tmp_path / "id_ed25519"
    key.write_text("PRIVATE-KEY-MUST-NOT-APPEAR")
    key.chmod(0o600)
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))
    key.with_suffix(".pub").write_text(
        "ssh-ed25519 " + base64.b64encode(blob).decode() + " private-comment\n"
    )
    return {
        "version": 1,
        "ssh_key": str(key),
        "ssh_cidr": "203.0.113.1/32",
        "targets": [
            {
                "provider": "ssh",
                "name": "primary",
                "hostname": "lifeboat.example.org",
                "host": "192.0.2.1",
            },
            {
                "provider": "ssh",
                "name": "secondary",
                "hostname": "alt.example.org",
                "host": "192.0.2.2",
            },
        ],
    }


def arguments(tmp_path, config, *extra):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(config))
    parser = argparse.ArgumentParser()
    deploy.configure_parser(parser.add_subparsers(dest="action"))
    return parser.parse_args(
        ["deploy", "--config", str(source), "--output", str(tmp_path / "output"), *extra]
    )


@pytest.mark.parametrize(
    ("provider", "anchor", "expected"),
    [
        ("aws", "eu-west-1", "eu-central-1"),
        ("aws", "us-west-2", "us-east-1"),
        ("aws", "ap-northeast-1", "ap-northeast-3"),
        ("gcp", "europe-west1", "europe-west3"),
        ("gcp", "us-central1", "us-east1"),
        ("alibaba", "eu-central-1", "eu-west-2"),
        ("alibaba", "us-east-1", "us-west-1"),
    ],
)
def test_region_suggestions_separate_repeated_providers(provider, anchor, expected):
    target = {"provider": provider}
    others = [{"provider": provider, "region": anchor}]
    before = copy.deepcopy((target, others))
    assert deploy.suggest_region(target, others) == expected
    assert (target, others) == before


def test_region_suggestions_skip_used_regions_and_other_providers():
    others = [
        {"provider": "alibaba", "region": "eu-central-1"},
        {"provider": "aws", "region": "eu-west-1"},
        {"provider": "gcp", "region": "europe-west1"},
        {"provider": "aws", "region": "eu-central-1"},
    ]
    assert deploy.suggest_region({"provider": "aws"}, others) == "eu-west-3"
    assert deploy.suggest_region({"provider": "gcp"}, others) == "europe-west3"


@pytest.mark.parametrize(
    ("target", "others"),
    [
        ({"provider": "aws"}, []),
        ({"provider": "aws"}, [{"provider": "gcp", "region": "us-central1"}]),
        ({"provider": "aws"}, [{"provider": "aws", "region": "eu-west-2"}]),
        ({"provider": "aws"}, [{"provider": "aws", "region": "us-gov-west-1"}]),
        ({"provider": "aws"}, [{"provider": "aws", "region": "future-region-1"}]),
        (
            {"provider": "gcp"},
            [
                {"provider": "gcp", "region": "europe-west1"},
                {"provider": "gcp", "region": "europe-west3"},
            ],
        ),
        (
            {"provider": "aws", "region": "eu-west-1"},
            [{"provider": "aws", "region": "eu-west-1"}],
        ),
        ({"provider": "ssh"}, [{"provider": "ssh"}]),
    ],
)
def test_region_suggestions_never_guess_cross_boundary_or_relocate(target, others):
    assert deploy.suggest_region(target, others) == ""


@pytest.mark.parametrize(
    ("provider", "field", "value", "region"),
    [
        ("aws", "image", "ami-123abc", "eu-west-1"),
        ("aws", "vpc_id", "vpc-123abc", "eu-west-1"),
        ("aws", "subnet_id", "subnet-123abc", "eu-west-1"),
        ("gcp", "zone", "europe-west1-b", "europe-west1"),
        ("alibaba", "zone", "eu-central-1a", "eu-central-1"),
        ("alibaba", "image", "ubuntu_24_04", "eu-central-1"),
    ],
)
def test_region_suggestions_do_not_reuse_location_bound_resources(provider, field, value, region):
    assert (
        deploy.suggest_region(
            {"provider": provider, field: value}, [{"provider": provider, "region": region}]
        )
        == ""
    )


@pytest.mark.parametrize("topology", ["shared", "independent"])
def test_saved_plan_reports_recovery_risks_without_relocating(config, tmp_path, capsys, topology):
    config["topology"] = topology
    for index, target in enumerate(config["targets"]):
        target.pop("host")
        target.update(
            provider="aws",
            region="eu-west-1",
            image=f"ami-{index}abc",
            vpc_id=f"vpc-{index}abc",
            subnet_id=f"subnet-{index}abc",
        )
    args = arguments(tmp_path, config, "--non-interactive", "--plan")
    assert deploy.run_deployment(args) == 0
    output = capsys.readouterr().out
    assert "primary, secondary share aws/eu-west-1" in output
    assert "RTO/RPO" in output
    assert (
        "only database authority" in output
        if topology == "shared"
        else "not recovery copies" in output
    )
    saved = json.loads((args.output / "config.json").read_text())
    assert [target["region"] for target in saved["targets"]] == ["eu-west-1", "eu-west-1"]
    for target in saved["targets"]:
        variables = json.loads((args.output / target["name"] / "terraform.tfvars.json").read_text())
        assert variables["region"] == "eu-west-1"
        assert variables["image"] == target["image"]


def test_recovery_review_does_not_confuse_providers_or_claim_ssh_separation():
    config = {
        "topology": "shared",
        "targets": [
            {"provider": "aws", "name": "primary", "region": "eu-central-1"},
            {"provider": "alibaba", "name": "gateway", "region": "eu-central-1"},
            {"provider": "ssh", "name": "onprem"},
        ],
    }
    warnings = deploy.recovery_warnings(config)
    assert not any(" share " in warning for warning in warnings)
    assert any("SSH failure domains are unknown" in warning for warning in warnings)
    assert any("only database authority" in warning for warning in warnings)


@pytest.mark.parametrize(
    ("provider", "fields"),
    [
        (
            "aws",
            {
                "region": "eu-west-1",
                "image": "ami-123abc",
                "vpc_id": "vpc-123abc",
                "subnet_id": "subnet-123abc",
            },
        ),
        ("gcp", {"project": "test-project", "region": "us-central1", "zone": "us-central1-a"}),
        ("alibaba", {"region": "eu-central-1", "zone": "eu-central-1a", "image": "ubuntu_24_04"}),
        ("ssh", {"host": "192.0.2.30"}),
    ],
)
def test_target_choices_render_without_commands(config, tmp_path, monkeypatch, provider, fields):
    config["targets"] = [
        {
            "provider": provider,
            "name": "test-node",
            "hostname": "lifeboat.example.org",
            **fields,
        }
    ]
    monkeypatch.setattr(
        deploy.subprocess, "run", lambda *a, **k: pytest.fail("Plan must not execute commands")
    )
    args = arguments(tmp_path, config, "--non-interactive", "--plan")
    assert deploy.run_deployment(args) == 0
    directory = args.output / "test-node"
    assert "reverse_proxy 127.0.0.1:8080" in (directory / "Caddyfile").read_text()
    if provider != "ssh":
        assert (directory / "main.tf").is_file()
        variables = json.loads((directory / "terraform.tfvars.json").read_text())
        assert "private-comment" not in variables["public_key"]


@pytest.mark.parametrize("topology", ["shared", "independent"])
def test_shared_and_independent_use_honest_roles(config, tmp_path, topology):
    config["topology"] = topology
    output = tmp_path / "render"
    deploy.render(deploy.validate_config(config), output)
    first = json.loads((output / "primary/deployment.json").read_text())
    second = json.loads((output / "secondary/deployment.json").read_text())
    assert first["role"] == "primary"
    assert second["role"] == ("gateway" if topology == "shared" else "primary")
    caddy = (output / "secondary/Caddyfile").read_text()
    if topology == "shared":
        assert '!= "https://alt.example.org"' in caddy
        assert caddy.index("respond @foreign") < caddy.index("header_up Origin")
        assert "reverse_proxy https://lifeboat.example.org" in caddy
        assert "header_up Host lifeboat.example.org" in caddy
        assert "tls_insecure_skip_verify" not in caddy
        assert "8080" not in caddy
    else:
        assert "reverse_proxy 127.0.0.1:8080" in caddy
        assert "header_up Origin" not in caddy


@pytest.mark.parametrize("topology", ["shared", "independent"])
def test_only_alibaba_authorities_need_state_disks(config, tmp_path, topology):
    config["topology"] = topology
    config["targets"][1] = {
        "provider": "alibaba",
        "name": "alibaba",
        "hostname": "ali.example.org",
        "region": "eu-central-1",
        "zone": "eu-central-1a",
        "image": "ubuntu_24_04",
    }
    output = tmp_path / "output"
    deploy.render(deploy.validate_config(config), output)
    variables = json.loads((output / "alibaba/terraform.tfvars.json").read_text())
    settings = json.loads((output / "alibaba/deployment.json").read_text())
    assert variables["state_disk"] == ("no" if topology == "shared" else "yes")
    assert settings["data_device"] == ("" if topology == "shared" else "/dev/vdb")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hostname", "https://evil.example.org/path"),
        ("hostname", "localhost"),
        ("hostname", "127.0.0.1"),
        ("hostname", "room.internal"),
        ("hostname", "evil.example.org\nrespond hacked"),
        ("name", "../../escape"),
        ("ssh_user", "-oProxyCommand=evil"),
        ("host", "host;touch marker"),
        ("host", "-oProxyCommand=evil"),
        ("ssh_port", True),
        ("ssh_port", 0),
        ("ssh_port", "22"),
    ],
)
def test_rejects_bad_target_before_commands(config, tmp_path, monkeypatch, field, value):
    config["targets"][1][field] = value
    monkeypatch.setattr(deploy, "_command", lambda *a, **k: pytest.fail("Must validate first"))
    with pytest.raises(HavocError):
        deploy.run_deployment(arguments(tmp_path, config, "--apply", "--confirm", "APPLY"))
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    "origin",
    [
        "http://primary.example.org",
        "https://u:password@primary.example.org",
        "https://primary.example.org/",
        "https://primary.example.org/path",
        "https://primary.example.org?token=secret",
        "https://primary.example.org#fragment",
        "https://primary.example.org:443",
        "https://127.0.0.1",
        "https://primary.local",
        "https://primary.example.org\n",
    ],
)
def test_gateway_upstream_rejects_non_public_origins(origin):
    with pytest.raises(HavocError):
        deploy.validate_origin(origin)


@pytest.mark.parametrize("mutation", ["secret", "cidr", "key", "duplicate", "topology", "ref"])
def test_rejects_invalid_plan_inputs(config, mutation):
    if mutation == "secret":
        config["admin_token"] = "do-not-log-this"
    elif mutation == "cidr":
        config["ssh_cidr"] = "0.0.0.0/0"
    elif mutation == "key":
        Path(config["ssh_key"]).chmod(0o644)
    elif mutation == "duplicate":
        config["targets"][1]["hostname"] = config["targets"][0]["hostname"]
    elif mutation == "topology":
        config["topology"] = "replicate"
    else:
        config["ref"] = "main; echo secret"
    with pytest.raises(HavocError) as caught:
        deploy.validate_config(config)
    assert "do-not-log-this" not in str(caught.value)


def test_duplicate_ssh_host_is_rejected(config):
    config["targets"][1]["host"] = config["targets"][0]["host"]
    with pytest.raises(HavocError, match="only run one target"):
        deploy.validate_config(config)


def test_invalid_public_key_is_rejected_before_cloud_commands(config, tmp_path, monkeypatch):
    config["targets"] = [
        {
            "provider": "gcp",
            "name": "gcp-node",
            "hostname": "gcp.example.org",
            "project": "test-project",
            "region": "us-central1",
            "zone": "us-central1-a",
        }
    ]
    Path(config["ssh_key"] + ".pub").write_text("ssh-ed25519 c2VjcmV0Cg==")
    monkeypatch.setattr(deploy, "_command", lambda *a, **k: pytest.fail("Must validate first"))
    with pytest.raises(HavocError, match=r"valid matching \.pub"):
        deploy.run_deployment(arguments(tmp_path, config, "--apply", "--confirm", "APPLY"))
    assert not (tmp_path / "output").exists()


def test_output_has_no_credentials_and_uses_private_permissions(config, tmp_path, capsys):
    args = arguments(tmp_path, config)
    assert deploy.run_deployment(args) == 0
    contents = "\n".join(path.read_text() for path in args.output.rglob("*") if path.is_file())
    messages = capsys.readouterr().out
    assert "PRIVATE-KEY-MUST-NOT-APPEAR" not in contents + messages
    assert "main/branches are mutable" in messages
    for path in args.output.rglob("*"):
        if path.is_file():
            assert path.stat().st_mode & 0o077 == 0
    assert args.output.stat().st_mode & 0o077 == 0


def test_topology_change_requires_new_output(config, tmp_path):
    output = tmp_path / "output"
    validated = deploy.validate_config(config)
    deploy.render(validated, output)
    changed = {**validated, "topology": "independent"}
    with pytest.raises(HavocError, match="migrate"):
        deploy.render(changed, output)
    assert json.loads((output / "config.json").read_text())["topology"] == "shared"


def test_missing_assets_are_actionable(config, tmp_path):
    with pytest.raises(HavocError, match="--assets"):
        deploy.render(deploy.validate_config(config), tmp_path / "out", tmp_path / "missing")


@pytest.mark.parametrize("missing", ["ssh", "terraform"])
def test_missing_tools_are_actionable(config, monkeypatch, missing):
    if missing == "terraform":
        config["targets"][0]["provider"] = "aws"
    monkeypatch.setattr(
        deploy.shutil,
        "which",
        lambda tool: "/usr/bin/ssh" if (missing == "terraform" and tool == "ssh") else None,
    )
    with pytest.raises(HavocError, match=r"Install|missing"):
        deploy.preflight(config)


@pytest.mark.parametrize("answer", [None, "no", "APPLY"])
def test_confirmation_and_cancellation(config, tmp_path, monkeypatch, answer):
    calls = []
    monkeypatch.setattr(deploy, "preflight", lambda config: None)
    monkeypatch.setattr(deploy, "apply", lambda *args: calls.append(args))
    monkeypatch.setattr(deploy.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: answer or "")
    args = arguments(tmp_path, config, "--apply")
    args.confirm = answer if answer == "APPLY" else None
    assert deploy.run_deployment(args) == (0 if answer == "APPLY" else 1)
    assert bool(calls) is (answer == "APPLY")


def test_noninteractive_apply_requires_explicit_approval(config, tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "preflight", lambda config: None)
    monkeypatch.setattr(deploy, "apply", lambda *args: pytest.fail("No approval"))
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("Must not prompt"))
    assert deploy.run_deployment(arguments(tmp_path, config, "--apply", "--non-interactive")) == 1


@pytest.mark.parametrize("topology", ["shared", "independent"])
def test_supervised_watcher_only_runs_on_authorities(config, tmp_path, topology):
    probes = ["https://service.example.org/health", "https://other.example.org:443/probe%2F$ready"]
    config.update(topology=topology, watch={"urls": probes})
    output = tmp_path / "output"
    deploy.render(deploy.validate_config(config), output)
    for index, name in enumerate(("primary", "secondary")):
        authority = index == 0 or topology == "independent"
        unit = (output / name / "havoc365-watch.service").read_text()
        settings = json.loads((output / name / "deployment.json").read_text())
        assert settings["watch_urls"] == (probes if authority else [])
        if not authority:
            assert unit == ""
            continue
        command = next(
            line.removeprefix("ExecStart=")
            for line in unit.splitlines()
            if line.startswith("ExecStart=")
        )
        assert "%%2F$$ready" in command
        argv = [arg.replace("%%", "%").replace("$$", "$") for arg in shlex.split(command)]
        args = cli_parser().parse_args(argv[1:])
        assert args.url == probes
        assert args.lifeboat_url == f"https://{config['targets'][index]['hostname']}"
        assert args.sms is False
        assert "EnvironmentFile=/etc/havoc365/lifeboat.env" in unit
        assert "Restart=always" in unit and "WantedBy=multi-user.target" in unit
        assert "PRIVATE-KEY-MUST-NOT-APPEAR" not in unit


def test_manual_only_plan_is_explicit_and_has_no_runnable_watcher(config, tmp_path, capsys):
    args = arguments(tmp_path, config, "--plan")
    assert deploy.run_deployment(args) == 0
    assert "activation is NOT automatic" in capsys.readouterr().out
    saved = json.loads((args.output / "config.json").read_text())
    assert saved["watch"] == {"urls": []}
    for name in ("primary", "secondary"):
        assert (args.output / name / "havoc365-watch.service").read_text() == ""


@pytest.mark.parametrize(
    "watch",
    [
        None,
        [],
        {"sms": True},
        {"urls": "https://status.example.org"},
        {"urls": [None]},
        {"urls": ["http://status.example.org"]},
        {"urls": ["https://user:secret@status.example.org"]},
        {"urls": ["https://status.example.org/?token=secret"]},
        {"urls": ["https://status.example.org/#secret"]},
        {"urls": ["https://127.0.0.1/health"]},
        {"urls": ["https://status.internal/health"]},
        {"urls": ["https://status.example.org/%broken"]},
        {"urls": ["https://status.example.org/\nExecStart=evil"]},
        {"urls": ["https://status.example.org:8443/health"]},
        {"urls": ["https://status.example.org"] * 2},
    ],
)
def test_bad_watch_configuration_fails_before_commands(config, tmp_path, monkeypatch, watch):
    config["watch"] = watch
    monkeypatch.setattr(deploy, "_command", lambda *a, **k: pytest.fail("Validate probes first"))
    with pytest.raises(HavocError):
        deploy.run_deployment(arguments(tmp_path, config, "--apply", "--confirm", "APPLY"))
    assert not (tmp_path / "output").exists()


def test_command_failure_does_not_expose_provider_output(monkeypatch):
    monkeypatch.setattr(
        deploy.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=3,
            stdout=b"secret-provider-token",
            stderr=b"secret-root-token",
        ),
    )
    with pytest.raises(HavocError, match="exit 3") as caught:
        deploy._command(["terraform", "init"], "Provider initialization")
    assert "secret-" not in str(caught.value)
    assert "No automatic cleanup" in str(caught.value)


def test_subprocess_uses_argv_and_sanitizes_terraform_logs(monkeypatch):
    calls = []
    monkeypatch.setenv("TF_LOG", "TRACE")
    monkeypatch.setenv("TF_CLI_ARGS_apply", "-destroy -auto-approve")
    monkeypatch.setenv("TF_VAR_secret", "secret")
    monkeypatch.setenv("AWS_PROFILE", "operator")

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"okay")

    monkeypatch.setattr(deploy.subprocess, "run", run)
    assert deploy._command(["terraform", "validate"], "Validate") == b"okay"
    argv, kwargs = calls[0]
    assert argv == ["terraform", "validate"]
    assert "shell" not in kwargs
    assert kwargs["env"]["TF_LOG"] == "OFF"
    assert "TF_CLI_ARGS_apply" not in kwargs["env"]
    assert "TF_VAR_secret" not in kwargs["env"]
    assert kwargs["env"]["AWS_PROFILE"] == "operator"


def test_timeout_is_actionable_without_output(monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired("ssh", 3600, output="private-output")

    monkeypatch.setattr(deploy.subprocess, "run", run)
    with pytest.raises(HavocError, match="could not run") as caught:
        deploy._command(["ssh"], "Bootstrap")
    assert "private-output" not in str(caught.value)


def test_ssh_apply_transmits_only_installation_assets(config, tmp_path, monkeypatch, capsys):
    config = deploy.validate_config(config)
    output = tmp_path / "output"
    deploy.render(config, output)
    calls = []

    def command(*args, **kwargs):
        calls.append((args, kwargs))
        return b"PASS ubuntu\nPASS python\nPASS systemd\nPASS storage\n"

    monkeypatch.setattr(deploy, "_command", command)
    deploy.apply(config, output, None)
    assert len(calls) == 4
    installs = 0
    for (argv, _), kwargs in calls:
        assert argv[0] == "ssh"
        assert "StrictHostKeyChecking=yes" in argv
        assert "BatchMode=yes" in argv
        assert argv[-1].startswith("sudo -n /bin/sh")
        if argv[-1] == "sudo -n /bin/sh -s":
            continue
        installs += 1
        with tarfile.open(fileobj=io.BytesIO(kwargs["payload"])) as archive:
            assert set(archive.getnames()) == {
                "bootstrap.sh",
                "Caddyfile",
                "havoc365.service",
                "havoc365-watch.service",
                "deployment.json",
            }
        assert b"PRIVATE-KEY-MUST-NOT-APPEAR" not in kwargs["payload"]
    assert installs == 2
    output = capsys.readouterr().out
    assert "No invitations were delivered" in output
    assert output.count("cat /etc/havoc365/lifeboat.env") == 1


@pytest.mark.parametrize("destructive", [False, True])
def test_cloud_command_path_and_destructive_plan_rejection(
    config, tmp_path, monkeypatch, destructive
):
    config["targets"] = [
        {
            "provider": "aws",
            "name": "cloud",
            "hostname": "cloud.example.org",
            "region": "eu-west-1",
            "image": "ami-123abc",
            "vpc_id": "vpc-123abc",
            "subnet_id": "subnet-123abc",
        }
    ]
    config = deploy.validate_config(config)
    output = tmp_path / "output"
    deploy.render(config, output)
    calls = []

    def command(argv, label, **kwargs):
        calls.append(argv)
        if argv[0] == "ssh":
            return b"PASS ubuntu\nPASS python\nPASS systemd\nPASS storage\n"
        if "show" in argv:
            return json.dumps(
                {
                    "resource_changes": [
                        {"change": {"actions": ["delete", "create"] if destructive else ["create"]}}
                    ]
                }
            ).encode()
        if "output" in argv:
            return b'"8.8.4.4"'
        return b""

    monkeypatch.setattr(deploy, "_command", command)
    if destructive:
        with pytest.raises(HavocError, match="Refusing destructive"):
            deploy.apply(config, output, "/usr/bin/tofu")
        assert all("apply" not in argv for argv in calls)
        assert all(argv[0] != "ssh" for argv in calls)
    else:
        deploy.apply(config, output, "/usr/bin/tofu")
        assert [argv[2] for argv in calls if argv[0] != "ssh"] == [
            "init",
            "validate",
            "plan",
            "show",
            "apply",
            "output",
        ]
        assert calls[-1][-2] == "ubuntu@8.8.4.4"
        assert calls[4][-1] == "deployment.tfplan"


def test_missing_noninteractive_config_is_actionable(tmp_path, monkeypatch):
    parser = argparse.ArgumentParser()
    deploy.configure_parser(parser.add_subparsers(dest="action"))
    args = parser.parse_args(["deploy", "--non-interactive"])
    with pytest.raises(HavocError, match="requires --config"):
        deploy.run_deployment(args)


def test_saved_config_unchanged_when_target_order_changes(config, tmp_path):
    output = tmp_path / "output"
    clean = deploy.validate_config(config)
    deploy.render(clean, output)
    changed = copy.deepcopy(clean)
    changed["targets"].reverse()
    with pytest.raises(HavocError, match="Target order"):
        deploy.render(changed, output)
    assert json.loads((output / "config.json").read_text())["targets"][0]["name"] == "primary"


def test_cli_preserves_deployment_cancellation(monkeypatch):
    monkeypatch.setattr(cli, "run_deployment", lambda args, **kwargs: 1)
    assert cli.main(["lifeboat", "deploy", "--non-interactive"]) == 1


def test_check_reads_hosts_without_installing(config, tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(deploy, "preflight", lambda config: None)

    def command(argv, label, **kwargs):
        calls.append((argv, kwargs))
        return b"PASS ubuntu\nPASS python\nPASS systemd\nPASS storage\n"

    monkeypatch.setattr(deploy, "_command", command)
    assert deploy.run_deployment(arguments(tmp_path, config, "--check")) == 0
    assert len(calls) == 2
    assert all(argv[-1] == "sudo -n /bin/sh -s" for argv, _ in calls)
    assert all(b"apt-get" not in kwargs["payload"] for _, kwargs in calls)
    assert "No infrastructure or hosts were changed" in capsys.readouterr().out


def test_every_cloud_plan_is_checked_before_any_target_changes(config, tmp_path, monkeypatch):
    for index, name in enumerate(("primary", "secondary")):
        config["targets"][index] = {
            "provider": "aws",
            "name": name,
            "hostname": f"{name}.example.org",
            "region": "eu-west-1",
            "image": "ami-123abc",
            "vpc_id": "vpc-123abc",
            "subnet_id": "subnet-123abc",
        }
    clean = deploy.validate_config(config)
    output = tmp_path / "output"
    deploy.render(clean, output)
    calls = []

    def command(argv, label, **kwargs):
        calls.append(argv)
        if "plan" in argv and "secondary" in label:
            raise HavocError("secondary: quota/permission check failed")
        if "show" in argv:
            return b'{"resource_changes":[]}'
        if "output" in argv:
            return b'"8.8.4.4"'
        return b"PASS ubuntu\nPASS python\nPASS systemd\nPASS storage\n"

    monkeypatch.setattr(deploy, "_command", command)
    with pytest.raises(HavocError):
        deploy.apply(clean, output, "/usr/bin/tofu")
    assert not any("apply" in argv or (argv[0] == "ssh" and "tar " in argv[-1]) for argv in calls)


@pytest.mark.parametrize(
    "reply",
    [
        b"PASS ubuntu\nPASS python\nPASS systemd\nFAIL storage\n",
        b"FAIL ubuntu\nPASS python\nPASS systemd\nPASS storage\n",
        b"private-unexpected-output",
    ],
)
def test_failed_host_preflight_blocks_every_installation(
    config, tmp_path, monkeypatch, capsys, reply
):
    clean = deploy.validate_config(config)
    output = tmp_path / "output"
    deploy.render(clean, output)
    calls = []

    def command(argv, label, **kwargs):
        calls.append(argv)
        return reply

    monkeypatch.setattr(deploy, "_command", command)
    with pytest.raises(HavocError, match="No infrastructure or hosts were changed") as caught:
        deploy.apply(clean, output, None)
    assert len(calls) == 2
    assert all(argv[-1] == "sudo -n /bin/sh -s" for argv in calls)
    assert "private-unexpected-output" not in str(caught.value) + capsys.readouterr().out


@pytest.mark.parametrize("version", ["1.5.7", "unparseable", [], None])
def test_cloud_engine_version_is_checked(config, monkeypatch, version):
    config["targets"][0]["provider"] = "aws"
    monkeypatch.setattr(deploy.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(
        deploy, "_command", lambda *a, **kw: json.dumps({"terraform_version": version}).encode()
    )
    with pytest.raises(HavocError, match=r"OpenTofu/Terraform 1.6\+"):
        deploy.preflight(config)


def test_cloud_check_only_plans_and_reports_deferred_host_checks(
    config, tmp_path, monkeypatch, capsys
):
    config["targets"] = [
        {
            "provider": "gcp",
            "name": "cloud",
            "hostname": "cloud.example.org",
            "project": "test-project",
            "region": "us-central1",
            "zone": "us-central1-a",
        }
    ]
    monkeypatch.setattr(deploy, "preflight", lambda config: "/usr/bin/tofu")
    calls = []

    def command(argv, label, **kwargs):
        calls.append(argv)
        return b'{"resource_changes":[{"change":{"actions":["create"]}}]}'

    monkeypatch.setattr(deploy, "_command", command)
    assert deploy.run_deployment(arguments(tmp_path, config, "--check", "--non-interactive")) == 0
    assert [argv[2] for argv in calls] == ["init", "validate", "plan", "show"]
    text = capsys.readouterr().out
    assert "1 create, 0 update" in text
    assert "PENDING" in text and "after VM allocation" in text
    assert "No infrastructure or hosts were changed" in text


def test_new_vm_host_failure_stops_further_allocation(config, tmp_path, monkeypatch):
    config["targets"] = [
        {
            "provider": "gcp",
            "name": name,
            "hostname": f"{name}.example.org",
            "project": "test-project",
            "region": "us-central1",
            "zone": "us-central1-a",
        }
        for name in ("primary", "secondary")
    ]
    clean = deploy.validate_config(config)
    output = tmp_path / "output"
    deploy.render(clean, output)
    calls = []

    def command(argv, label, **kwargs):
        calls.append(argv)
        if argv[0] == "ssh":
            raise HavocError("SSH host key is not trusted yet.")
        if "output" in argv:
            return b'"8.8.4.4"'
        return b'{"resource_changes":[]}'

    monkeypatch.setattr(deploy, "_command", command)
    with pytest.raises(HavocError, match="Resources may remain billable"):
        deploy.apply(clean, output, "/usr/bin/tofu")
    assert sum("apply" in argv for argv in calls) == 1
    assert all(argv[-1] == "sudo -n /bin/sh -s" for argv in calls if argv[0] == "ssh")


@pytest.mark.parametrize(
    ("available", "status"),
    [(1_048_575, "FAIL"), (1_048_576, "PASS")],
)
def test_real_host_probe_checks_one_gib_free_on_each_mount(
    config, tmp_path, monkeypatch, available, status
):
    probes = []

    def command(argv, label, **kwargs):
        probes.append(kwargs["payload"])
        return b"PASS ubuntu\nPASS python\nPASS systemd\nPASS storage\n"

    monkeypatch.setattr(deploy, "_command", command)
    clean = deploy.validate_config(config)
    deploy._host_checks(clean, clean["targets"][0], "192.0.2.1")
    df = tmp_path / "df"
    df.write_text(
        "#!/bin/sh\nprintf '%s\\n' "
        "'Filesystem 1024-blocks Used Available Capacity Mounted on' "
        "'disk1 2097152 0 1048576 0% /var/lib' "
        f"'disk2 2097152 0 {available} 0% /opt'\n"
    )
    df.chmod(0o700)
    result = subprocess.run(
        ["/bin/sh"],
        input=probes[0],
        capture_output=True,
        timeout=10,
        env=dict(os.environ, PATH=f"{tmp_path}:{os.defpath}"),
        check=True,
    )
    assert f"{status} storage".encode() in result.stdout.splitlines()
