import asyncio
import base64
import json

import pytest
from textual.widgets import Button, Collapsible, Input, Select, Static

from havoc365 import deploy_tui, deployment
from havoc365.cli import main, parser
from havoc365.deploy_tui import DeploymentApp


@pytest.fixture
def key(tmp_path):
    path = tmp_path / "id_ed25519"
    path.write_text("synthetic-offline-key")
    path.chmod(0o600)
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))
    path.with_suffix(".pub").write_text("ssh-ed25519 " + base64.b64encode(blob).decode())
    return path


def fill(app, **values):
    for name, value in values.items():
        app.query_one(f"#field-{name}", Input).value = str(value)


async def choose(app, pilot, provider):
    app.query_one("#provider", Select).value = provider
    await pilot.pause()


@pytest.mark.parametrize("size", [(80, 24), (60, 20)])
def test_provider_screen_fits_and_keyboard_selection_is_editable(size):
    async def exercise():
        app = DeploymentApp()
        async with app.run_test(size=size) as pilot:
            await pilot.press("enter", "home", "down", "enter")
            await pilot.pause()
            assert [target["provider"] for target in app.targets] == ["aws"]
            await pilot.press("enter", "home", "down", "enter")
            assert [target["provider"] for target in app.targets] == ["aws", "aws"]
            assert [target["name"] for target in app.targets] == ["aws-1", "aws-2"]
            picker = app.query_one("#provider", Select)
            body = app.query_one("#body")
            assert picker.region.bottom <= body.content_region.bottom
            assert picker.region.y >= body.content_region.y
            app.query_one("#remove-0").scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click("#remove-0")
            assert len(app.targets) == 1
            assert app.targets[0]["name"] == "aws-2"
            assert app.query_one("#next").region.bottom <= size[1] - 1
            assert app.query_one("#navigation").region.right <= size[0]
            assert app.query_one("#provider").region.right <= size[0]
            await pilot.press("escape")
        assert app.return_value is None

    asyncio.run(exercise())


def test_escape_dismisses_picker_before_cancelling():
    async def exercise():
        app = DeploymentApp()
        async with app.run_test() as pilot:
            await pilot.press("enter")
            assert app.query_one("#provider", Select).expanded
            await pilot.press("escape")
            assert not app.query_one("#provider", Select).expanded
            assert app.is_running
            await pilot.press("ctrl+q")
        assert app.return_value is None

    asyncio.run(exercise())


@pytest.mark.parametrize("topology", ["shared", "independent"])
def test_complete_multicloud_plan_with_back_navigation_and_no_network(key, monkeypatch, topology):
    monkeypatch.setattr(
        deployment, "_command", lambda *a, **kw: pytest.fail("Planner must stay offline")
    )

    async def exercise():
        app = DeploymentApp()
        async with app.run_test(size=(80, 24)) as pilot:
            for provider in ("aws", "gcp", "alibaba", "ssh", "ssh"):
                await choose(app, pilot, provider)
            app.query_one("#topology", Select).value = topology
            await pilot.pause()
            await pilot.press("alt+right")
            fill(app, ssh_key=key, ssh_cidr="203.0.113.1/32")
            await pilot.press("alt+right")
            assert app.page == 2
            fields = [
                {
                    "region": "eu-west-1",
                    "image": "ami-123abc",
                    "vpc_id": "vpc-123abc",
                    "subnet_id": "subnet-123abc",
                },
                {"project": "test-project", "region": "us-central1", "zone": "us-central1-a"},
                {"region": "eu-central-1", "zone": "eu-central-1a", "image": "ubuntu_24_04"},
                {"host": "192.0.2.1", "ssh_port": "2222"},
                {"host": "192.0.2.2"},
            ]
            for index, values in enumerate(fields):
                fill(app, hostname=f"room{index}.example.org", **values)
                await pilot.press("alt+right")
                assert app.page == index + 3
            fill(app, probes="http://bad.example.org")
            await pilot.press("alt+right")
            assert app.page == 7
            assert app.query_one("#field-probes", Input).has_class("invalid")
            fill(app, probes="https://status.example.org/health")
            await pilot.press("alt+right")
            assert app.query_one("#next", Button).label == "Save plan"
            assert app.return_value is None
            await pilot.press("alt+left")
            assert (
                app.query_one("#field-probes", Input).value == "https://status.example.org/health"
            )
            await pilot.press("alt+right")
            await pilot.click("#next")
        config = app.return_value
        assert config is not None
        assert config["topology"] == topology
        assert [target["provider"] for target in config["targets"]] == [
            "aws",
            "gcp",
            "alibaba",
            "ssh",
            "ssh",
        ]
        assert config["targets"][3]["ssh_port"] == 2222
        assert config["watch"]["urls"] == ["https://status.example.org/health"]
        return config

    config = asyncio.run(exercise())
    assert deployment.validate_config(config) == config


def test_invalid_fields_preserve_drafts_and_reveal_advanced_source(key):
    async def exercise():
        app = DeploymentApp()
        async with app.run_test() as pilot:
            await pilot.press("alt+right")
            assert app.page == 0
            assert "Choose at least" in str(app.query_one("#error", Static).render())
            await choose(app, pilot, "ssh")
            await pilot.press("alt+right")
            await pilot.pause()
            assert app.query_one("#body").scroll_y == 0
            fill(app, ssh_key="/missing/key", ssh_cidr="203.0.113.1/32")
            await pilot.press("alt+right")
            assert app.page == 1
            assert app.focused.id == "field-ssh_key"
            fill(app, ssh_key=key, ref="main;invalid")
            await pilot.press("alt+right")
            assert app.page == 1
            assert app.query_one(Collapsible).collapsed is False
            fill(app, ref="main")
            await pilot.press("alt+right")
            fill(app, hostname="https://wrong.example.org", host="192.0.2.1")
            await pilot.click("#next")
            assert app.page == 2
            assert app.query_one("#field-hostname", Input).has_class("invalid")
            fill(app, hostname="room.example.org", ssh_port="2222")
            await pilot.press("alt+left")
            assert app.query_one("#field-ssh_key", Input).value == str(key)
            await pilot.press("alt+right")
            assert app.query_one("#field-hostname", Input).value == "room.example.org"
            assert app.query_one("#field-ssh_port", Input).value == "2222"
            await pilot.press("alt+right")
            await pilot.press("alt+right")
            await pilot.click("#next")
        assert app.return_value["watch"]["urls"] == []

    asyncio.run(exercise())


def test_duplicate_ssh_target_is_rejected_inline(key):
    async def exercise():
        app = DeploymentApp()
        async with app.run_test() as pilot:
            await choose(app, pilot, "ssh")
            await choose(app, pilot, "ssh")
            await pilot.press("alt+right")
            fill(app, ssh_key=key, ssh_cidr="203.0.113.1/32")
            await pilot.press("alt+right")
            fill(app, hostname="room1.example.org", host="192.0.2.1")
            await pilot.press("alt+right")
            fill(app, hostname="room1.example.org", host="192.0.2.1")
            await pilot.press("alt+right")
            assert app.page == 3
            assert app.focused.id == "field-hostname"
            fill(app, hostname="room2.example.org")
            await pilot.press("alt+right")
            assert app.page == 3
            assert app.focused.id == "field-host"
            fill(app, ssh_port="2222")
            await pilot.press("alt+right")
            assert app.page == 4
            await pilot.press("escape")

    asyncio.run(exercise())


def test_target_names_roles_and_removal_stay_in_sync():
    async def exercise():
        app = DeploymentApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await choose(app, pilot, "aws")
            app.query_one("#target-name-0", Input).value = "europe"
            await choose(app, pilot, "aws")
            app.query_one("#target-name-1", Input).value = "america"
            app.targets[1]["region"] = "us-east-1"
            await choose(app, pilot, "ssh")
            assert [target["name"] for target in app.targets] == ["europe", "america", "ssh-1"]
            assert "Authority" in str(app.query_one("#role-0", Static).render())
            assert "Gateway" in str(app.query_one("#role-1", Static).render())
            app.query_one("#remove-0").scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click("#remove-0")
            assert [target["name"] for target in app.targets] == ["america", "ssh-1"]
            assert app.targets[0]["region"] == "us-east-1"
            assert app.query_one("#target-name-1", Input).value == "america"
            assert "Authority" in str(app.query_one("#role-1", Static).render())
            app.query_one("#topology", Select).value = "independent"
            await pilot.pause()
            assert "Authority" in str(app.query_one("#role-2", Static).render())
            assert "2 authorities" in str(app.query_one("#fleet-summary", Static).render())
            await choose(app, pilot, "aws")
            assert len({target["name"] for target in app.targets}) == 3
            assert app.targets[0]["name"] == "america"
            await pilot.press("escape")

    asyncio.run(exercise())


def test_duplicate_target_names_are_flagged_before_leaving_selection():
    async def exercise():
        app = DeploymentApp()
        async with app.run_test() as pilot:
            await choose(app, pilot, "gcp")
            await choose(app, pilot, "gcp")
            app.query_one("#target-name-1", Input).value = "gcp-1"
            await pilot.press("alt+right")
            assert app.page == 0
            assert app.focused.id == "target-name-1"
            assert "unique" in str(app.query_one("#error", Static).render())
            app.query_one("#target-name-1", Input).value = "backup"
            await pilot.press("alt+right")
            assert app.page == 1
            await pilot.press("escape")

    asyncio.run(exercise())


@pytest.mark.parametrize("topology", ["shared", "independent"])
@pytest.mark.parametrize("override_region", [False, True])
def test_repeated_aws_targets_render_separate_regions_and_state(
    key, tmp_path, monkeypatch, topology, override_region
):
    monkeypatch.setattr(
        deployment, "_command", lambda *a, **kw: pytest.fail("Planner must stay offline")
    )

    async def exercise():
        app = DeploymentApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await choose(app, pilot, "aws")
            await choose(app, pilot, "aws")
            app.query_one("#topology", Select).value = topology
            await pilot.pause()
            await pilot.press("alt+right")
            fill(app, ssh_key=key, ssh_cidr="203.0.113.1/32")
            await pilot.press("alt+right")
            for name, region, resource in (
                ("aws-1", "eu-west-1", "123abc"),
                ("aws-2", "us-east-1" if override_region else "eu-central-1", "456def"),
            ):
                assert app.query_one("#field-name", Input).value == name
                assert app.query_one("#field-region", Input).value == (
                    "" if name == "aws-1" else "eu-central-1"
                )
                assert app.query_one("#field-image", Input).value == ""
                assert app.query_one("#field-vpc_id", Input).value == ""
                assert app.query_one("#field-subnet_id", Input).value == ""
                fill(
                    app,
                    hostname=f"{name}.example.org",
                    **({"region": region} if name == "aws-1" or override_region else {}),
                    image=f"ami-{resource}",
                    vpc_id=f"vpc-{resource}",
                    subnet_id=f"subnet-{resource}",
                )
                await pilot.press("alt+right")
            await pilot.press("alt+left")
            assert app.query_one("#field-region", Input).value == (
                "us-east-1" if override_region else "eu-central-1"
            )
            assert app.query_one("#field-image", Input).value == "ami-456def"
            await pilot.press("alt+right")
            await pilot.press("alt+right")
            warnings = str(app.query_one("#recovery-warnings", Static).render())
            assert "RTO/RPO" in warnings
            assert (
                "only database authority" in warnings
                if topology == "shared"
                else "not recovery copies" in warnings
            )
            await pilot.click("#next")
        return app.return_value

    config = asyncio.run(exercise())
    output = tmp_path / "plan"
    deployment.render(config, output)
    for name, region, role in (
        ("aws-1", "eu-west-1", "primary"),
        (
            "aws-2",
            "us-east-1" if override_region else "eu-central-1",
            "gateway" if topology == "shared" else "primary",
        ),
    ):
        directory = output / name
        assert json.loads((directory / "terraform.tfvars.json").read_text())["region"] == region
        assert json.loads((directory / "deployment.json").read_text())["role"] == role
        assert (directory / "main.tf").is_file()
    assert len({target["name"] for target in config["targets"]}) == 2


def test_tui_review_warns_about_explicit_same_region_targets(key):
    async def exercise():
        app = DeploymentApp()
        async with app.run_test(size=(60, 20)) as pilot:
            for index in range(2):
                await choose(app, pilot, "aws")
                app.targets[index].update(
                    hostname=f"room{index}.example.org",
                    region="eu-west-1",
                    image=f"ami-{index}abc",
                    vpc_id=f"vpc-{index}abc",
                    subnet_id=f"subnet-{index}abc",
                )
            app.settings.update(ssh_key=str(key), ssh_cidr="203.0.113.1/32")
            app.page = 5
            await app.show_page()
            warnings = str(app.query_one("#recovery-warnings", Static).render())
            assert "aws-1, aws-2 share aws/eu-west-1" in warnings
            assert app.query_one("#next").region.bottom <= 19
            await pilot.press("alt+left")
            await pilot.press("alt+left")
            assert app.query_one("#field-region", Input).value == "eu-west-1"
            await pilot.press("escape")

    asyncio.run(exercise())


def test_twenty_targets_keep_navigation_visible_and_reopen_empty_row_after_removal():
    async def exercise():
        app = DeploymentApp()
        async with app.run_test(size=(60, 20)) as pilot:
            for _ in range(20):
                await choose(app, pilot, "ssh")
                if len(app.targets) < 20:
                    picker = app.query_one("#provider", Select)
                    assert picker.region.bottom <= app.query_one("#body").content_region.bottom
            assert app.query_one("#provider", Select).disabled
            assert len(app.targets) == len({target["name"] for target in app.targets}) == 20
            assert app.query_one("#next").region.bottom <= 19
            app.query_one("#remove-19").scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click("#remove-19")
            assert len(app.targets) == 19
            assert not app.query_one("#provider", Select).disabled
            await choose(app, pilot, "ssh")
            assert len(app.targets) == len({target["name"] for target in app.targets}) == 20
            await pilot.press("escape")

    asyncio.run(exercise())


def test_resizing_keeps_the_fresh_destination_picker_visible():
    async def exercise():
        app = DeploymentApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await choose(app, pilot, "aws")
            await choose(app, pilot, "aws")
            await pilot.resize_terminal(60, 20)
            await pilot.pause()
            picker = app.query_one("#provider", Select)
            body = app.query_one("#body")
            assert picker.region.bottom <= body.content_region.bottom
            assert picker.region.y >= body.content_region.y
            await pilot.press("escape")

    asyncio.run(exercise())


def test_terminal_cancel_never_writes_or_applies(monkeypatch, tmp_path):
    monkeypatch.setattr(DeploymentApp, "run", lambda self: None)
    monkeypatch.setattr(deployment.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(deployment.sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(
        deployment, "_command", lambda *a, **kw: pytest.fail("Cancel must not run commands")
    )
    output = tmp_path / "plan"
    assert main(["lifeboat", "deploy", "--output", str(output)]) == 1
    assert not output.exists()


def test_terminal_config_is_saved_without_reprinting_review(monkeypatch, tmp_path, key, capsys):
    config = {
        "ssh_key": str(key),
        "ssh_cidr": "203.0.113.1/32",
        "targets": [
            {
                "provider": "ssh",
                "name": "room",
                "hostname": "room.example.org",
                "host": "192.0.2.1",
            }
        ],
    }
    monkeypatch.setattr(DeploymentApp, "run", lambda self: config)
    monkeypatch.setattr(deployment.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(deployment.sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(
        deployment, "_command", lambda *a, **kw: pytest.fail("Save must stay offline")
    )
    output = tmp_path / "plan"
    args = parser().parse_args(["lifeboat", "deploy", "--output", str(output)])
    assert deployment.run_deployment(args) == 0
    assert json.loads((output / "config.json").read_text())["targets"][0]["name"] == "room"
    assert "Deployment review" not in capsys.readouterr().out


def test_saved_config_and_help_do_not_start_tui(monkeypatch):
    monkeypatch.setattr(
        deploy_tui, "run_wizard", lambda: pytest.fail("Noninteractive path must not start TUI")
    )
    with pytest.raises(SystemExit) as result:
        main(["lifeboat", "deploy", "--help"])
    assert result.value.code == 0


@pytest.mark.parametrize(
    ("input_tty", "output_tty", "term"),
    [(False, True, "xterm"), (True, False, "xterm"), (True, True, "dumb")],
)
def test_unsupported_terminal_explains_config_path_without_starting_ui(
    monkeypatch, capsys, input_tty, output_tty, term
):
    monkeypatch.setattr(deployment.sys.stdin, "isatty", lambda: input_tty)
    monkeypatch.setattr(deployment.sys.stdout, "isatty", lambda: output_tty)
    monkeypatch.setenv("TERM", term)
    monkeypatch.setattr(
        deploy_tui, "run_wizard", lambda: pytest.fail("No full-screen UI without a usable TTY")
    )
    assert main(["lifeboat", "deploy"]) == 1
    assert "--config PATH" in capsys.readouterr().err
