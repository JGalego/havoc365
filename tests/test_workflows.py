import json
import zipfile

import pytest

from havoc365.cli import main
from havoc365.errors import HavocError
from havoc365.mapping import MapReport
from havoc365.workflows import add_workflows


def export(tmp_path, value):
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(value))
    return path


def test_published_workflow_template_reveals_mail_dependency(fixture, tmp_path):
    path = export(tmp_path, fixture("workflow-template"))
    report = add_workflows(MapReport("exports", {}, ()), [path])
    assert report.sections["Exchange Online"][0].summary.startswith("1 exported")
    assert report.sections["Teams"][0].summary.startswith("0 exported")
    assert "supplied exports only" in report.plain()


def test_workflow_archive_is_read_without_extracting_paths(fixture, tmp_path):
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../../definition.json", json.dumps(fixture("workflow-template")))
    report = add_workflows(MapReport("exports", {}, ()), [path])
    assert report.sections["Power Automate"][0].summary.startswith("1 exported")
    assert not (tmp_path.parent / "definition.json").exists()


def test_nested_teams_connector_and_webhook_are_detected_without_leaking_url(tmp_path):
    # Local configuration inputs, not invented API responses.
    secret_url = "https://example.webhook.office.com/webhookb2/private-token?sig=secret"
    path = export(
        tmp_path,
        {
            "actions": {
                "Send": {
                    "inputs": {
                        "host": {"apiId": "/providers/Microsoft.PowerApps/apis/shared_teams"}
                    }
                },
                "Hook": {"inputs": {"uri": secret_url}},
            }
        },
    )
    report = add_workflows(MapReport("exports", {}, ()), [path])
    assert report.sections["Teams"][0].summary.startswith("1 exported")
    assert "sig=secret" not in report.plain()
    assert "private-token" not in report.plain()
    assert secret_url not in report.plain()


def test_invalid_exports_do_not_look_like_empty_inventories(tmp_path):
    for value in ({"unrelated": "data"}, []):
        with pytest.raises(HavocError):
            add_workflows(MapReport("exports", {}, ()), [export(tmp_path, value)])


def test_exports_only_cli_never_signs_in(fixture, tmp_path, monkeypatch, capsys):
    path = export(tmp_path, fixture("workflow-template"))
    monkeypatch.chdir(tmp_path)
    assert main(["map", "--exports-only", "--flows", str(path), "--no-interactive"]) == 0
    assert "1 exported workflows reference Exchange Online" in capsys.readouterr().out


def test_exports_only_requires_input(capsys):
    assert main(["map", "--exports-only"]) == 1
    assert "requires at least one" in capsys.readouterr().err
