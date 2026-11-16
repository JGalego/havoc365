import argparse
import base64
import copy
import io
import json
import shlex
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

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


@pytest.mark.parametrize("probes", ["", "https://status.example.org/health"])
def test_wizard_multiple_choices_and_shared_default(config, monkeypatch, capsys, probes):
    responses = iter(
        [
            "aws,gcp,alibaba,ssh",
            "",
            config["ssh_key"],
            config["ssh_cidr"],
            "",
            "",
            "aws-node",
            "aws.example.org",
            "",
            "eu-west-1",
            "ami-123abc",
            "",
            "vpc-123abc",
            "subnet-123abc",
            "gcp-node",
            "gcp.example.org",
            "",
            "test-project",
            "us-central1",
            "us-central1-a",
            "",
            "",
            "ali-node",
            "ali.example.org",
            "",
            "eu-central-1",
            "eu-central-1a",
            "ubuntu_24_04",
            "",
            "ssh-node",
            "ssh.example.org",
            "",
            "192.0.2.1",
            probes,
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt: next(responses))
    value = deploy.validate_config(deploy.wizard())
    assert value["topology"] == "shared"
    assert [t["provider"] for t in value["targets"]] == ["aws", "gcp", "alibaba", "ssh"]
    assert value["watch"]["urls"] == ([probes] if probes else [])
    assert ("NOT be automatic" if not probes else "supervised watcher") in capsys.readouterr().out


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


def test_wizard_rejects_unknown_provider(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "azure")
    with pytest.raises(HavocError, match="Unknown target"):
        deploy.wizard()


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
    monkeypatch.setattr(deploy, "_command", lambda *a, **k: calls.append((a, k)) or b"")
    deploy.apply(config, output, None)
    assert len(calls) == 2
    for (argv, _), kwargs in calls:
        assert argv[0] == "ssh"
        assert "StrictHostKeyChecking=yes" in argv
        assert "BatchMode=yes" in argv
        assert argv[-1].startswith("sudo -n /bin/sh")
        with tarfile.open(fileobj=io.BytesIO(kwargs["payload"])) as archive:
            assert set(archive.getnames()) == {
                "bootstrap.sh",
                "Caddyfile",
                "havoc365.service",
                "havoc365-watch.service",
                "deployment.json",
            }
        assert b"PRIVATE-KEY-MUST-NOT-APPEAR" not in kwargs["payload"]
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
        assert [argv[2] for argv in calls[:-1]] == [
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
