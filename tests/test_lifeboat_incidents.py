import json
import sqlite3

import httpx
import pytest
from starlette.testclient import TestClient

from havoc365.cli import main
from havoc365.errors import HavocError
from havoc365.lifeboat import Room, backup, create_app, restore, set_active

MEMBER = "offline-member-" + "m" * 32
ADMIN = "offline-admin-" + "a" * 32


@pytest.fixture
def incident(monkeypatch, tmp_path):
    monkeypatch.setenv("HAVOC365_LIFEBOAT_TOKEN", MEMBER)
    monkeypatch.setenv("HAVOC365_LIFEBOAT_ADMIN_TOKEN", ADMIN)
    app = create_app(tmp_path / "live")
    with TestClient(app, base_url="https://lifeboat.test") as client:
        yield client, app, tmp_path


def signin(client, credential):
    response = client.post("/api/session", json={"credential": credential})
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf"]}


def invite(client, headers, name="Volunteer", role="member"):
    response = client.post(
        "/api/admin/invites",
        headers=headers,
        json={"name": name, "role": role, "hours": 24},
    )
    assert response.status_code == 201, response.text
    return response.json()["credential"]


def test_cookie_sessions_enforce_csrf_and_members_cannot_administer(incident):
    admin, app, _ = incident
    headers = signin(admin, ADMIN)
    assert admin.get("/api/session").json()["user"]["role"] == "admin"
    response = admin.post("/api/admin/contacts", json={"contacts": []})
    assert response.status_code == 403
    response = admin.post(
        "/api/admin/contacts",
        headers={**headers, "Origin": "https://another.test"},
        json={"contacts": []},
    )
    assert response.status_code == 403
    credential = invite(admin, headers)
    member = TestClient(app, base_url="https://lifeboat.test")
    try:
        member_headers = signin(member, credential)
        assert member.get("/api/admin").status_code == 403
        assert (
            member.post(
                "/api/admin/incident", headers=member_headers, json={"mode": "open"}
            ).status_code
            == 403
        )
        assert member.get("/api/admin/export").status_code == 403
        assert member.post("/api/logout", headers=member_headers).status_code == 200
        assert member.get("/api/session").status_code == 401
    finally:
        member.close()


def test_hold_survives_watcher_recovery_restart_and_explicit_release(incident):
    admin, _, root = incident
    headers = signin(admin, ADMIN)
    data = {
        "mode": "open",
        "title": "Mail unavailable",
        "update": "Call the incident desk.",
        "lead": "Incident desk",
        "next_update": "16:00 UTC",
    }
    assert admin.post("/api/admin/incident", headers=headers, json=data).status_code == 200
    bearer = {"Authorization": f"Bearer {ADMIN}"}
    response = admin.post(
        "/api/status", headers=bearer, json={"active": False, "source": "watcher"}
    )
    assert response.json() == {"active": True, "requested": False, "mode": "open"}
    restored = Room(root / "live").snapshot()
    assert restored["active"]
    assert restored["incident"]["title"] == "Mail unavailable"
    response = admin.post("/api/status", headers=bearer, json={"mode": "auto"})
    assert response.json() == {"active": False, "mode": "auto"}
    assert admin.get("/api/room").json()["incident"]["title"] == "Mail unavailable"


def test_enrolled_identity_checkins_and_per_person_revocation(incident):
    admin, app, _ = incident
    headers = signin(admin, ADMIN)
    credential = invite(admin, headers, "Field operator")
    member = TestClient(app, base_url="https://lifeboat.test")
    try:
        member_headers = signin(member, credential)
        person_id = member.get("/api/session").json()["user"]["id"]
        admin.post("/api/admin/incident", headers=headers, json={"mode": "open"})
        response = member.post(
            "/api/messages",
            headers=member_headers,
            json={"author": "Someone else", "message": "On cellular."},
        )
        assert response.status_code == 201
        assert admin.get("/api/room").json()["messages"][-1]["author"] == "Field operator"
        assert (
            member.post(
                "/api/checkins",
                headers=member_headers,
                json={"status": "help", "note": "Need an alternate contact."},
            ).status_code
            == 200
        )
        snapshot = admin.get("/api/room").json()
        assert snapshot["checkins"][0]["person_id"] == person_id
        assert snapshot["checkins"][0]["status"] == "help"
        admin.post("/api/admin/incident", headers=headers, json={"mode": "standby"})
        admin.post("/api/admin/incident", headers=headers, json={"mode": "open"})
        snapshot = admin.get("/api/room").json()
        assert snapshot["incident"]["id"] == 2
        assert snapshot["checkins"] == []
        assert (
            admin.post(
                f"/api/admin/people/{person_id}/revoke", headers=headers, json={}
            ).status_code
            == 200
        )
        assert member.get("/api/room").status_code == 401
        exported = admin.get("/api/admin/export")
        assert exported.status_code == 200
        assert "attachment" in exported.headers["Content-Disposition"]
        assert exported.json()["audit"]
        assert any(person["id"] == person_id for person in exported.json()["people"])
        assert exported.json()["invites"]
        assert credential not in exported.text
        assert ADMIN not in exported.text
        assert "csrf" not in exported.text
    finally:
        member.close()


def test_contacts_editing_readiness_and_invalid_payloads(incident):
    admin, _, _ = incident
    headers = signin(admin, ADMIN)
    contacts = [{"name": "Incident desk", "contact": "+1 555 0100"}]
    assert (
        admin.post("/api/admin/contacts", headers=headers, json={"contacts": contacts}).status_code
        == 200
    )
    assert admin.get("/api/room").json()["contacts"] == contacts
    assert admin.get("/readyz").json() == {"status": "ready"}
    for mode in ([], {}, 0, None):
        response = admin.post("/api/admin/incident", headers=headers, json={"mode": mode})
        assert response.status_code == 400
    assert (
        admin.post("/api/checkins", headers=headers, json={"status": [], "note": ""}).status_code
        == 400
    )
    assert (
        admin.post(
            "/api/admin/contacts", headers=headers, json={"contacts": [{"name": "Broken"}]}
        ).status_code
        == 400
    )


def test_watcher_accepts_effective_manual_state_but_not_false_confirmation():
    def held(request):
        assert json.loads(request.content)["source"] == "watcher"
        return httpx.Response(200, json={"active": True, "requested": False, "mode": "open"})

    with httpx.Client(transport=httpx.MockTransport(held)) as client:
        set_active(client, "https://lifeboat.test", ADMIN, False, source="watcher")
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"active": True}))
        ) as client,
        pytest.raises(HavocError, match="did not confirm"),
    ):
        set_active(client, "https://lifeboat.test", ADMIN, False, source="watcher")


def test_online_backup_and_offline_restore_preserve_accounts_and_room(incident):
    admin, _, root = incident
    headers = signin(admin, ADMIN)
    credential = invite(admin, headers, "Recovery volunteer")
    admin.post("/api/admin/incident", headers=headers, json={"mode": "open", "title": "Drill"})
    output = root / "backup.sqlite3"
    backup(root / "live", output)
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(HavocError, match="already exists"):
        backup(root / "live", output)
    with pytest.raises(HavocError, match="already running"):
        restore(root / "live", output, replace=True)
    fresh = root / "restored"
    restore(fresh, output)
    with TestClient(create_app(fresh), base_url="https://lifeboat.test") as recovered:
        signin(recovered, credential)
        assert recovered.get("/api/session").json()["user"]["name"] == "Recovery volunteer"
        assert recovered.get("/api/room").json()["incident"]["title"] == "Drill"
    with pytest.raises(HavocError, match="already exists"):
        restore(fresh, output)


def test_restore_rejects_non_lifeboat_and_corrupt_backups_without_overwriting(incident):
    _, _, root = incident
    for name in ("other", "corrupt"):
        source = root / f"{name}.sqlite3"
        if name == "other":
            with sqlite3.connect(source) as db:
                db.execute("CREATE TABLE unrelated (id INTEGER)")
        else:
            source.write_bytes(b"not a database")
        with pytest.raises(HavocError):
            restore(root / f"target-{name}", source)
        assert not (root / f"target-{name}" / "lifeboat.sqlite3").exists()


def test_backup_cli_reports_missing_database(capsys, tmp_path):
    assert main(["lifeboat", "backup", "--output", str(tmp_path / "backup.sqlite3")]) == 1
    assert "No Lifeboat database" in capsys.readouterr().err
