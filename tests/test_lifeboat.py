import json

import httpx
import pytest
from starlette.testclient import TestClient

from havoc365.cli import main
from havoc365.errors import HavocError
from havoc365.lifeboat import Room, create_app, set_active
from havoc365.storage import write_json


@pytest.fixture
def room_client(monkeypatch, tmp_path):
    monkeypatch.setenv("HAVOC365_LIFEBOAT_TOKEN", "member-" + "m" * 32)
    monkeypatch.setenv("HAVOC365_LIFEBOAT_ADMIN_TOKEN", "admin-" + "a" * 32)
    member = {"Authorization": "Bearer " + "member-" + "m" * 32}
    admin = {"Authorization": "Bearer " + "admin-" + "a" * 32}
    with TestClient(create_app(tmp_path)) as client:
        yield client, member, admin


def test_fallback_requires_no_microsoft_login_and_protects_room_data(room_client):
    client, member, _ = room_client
    page = client.get("/")
    assert page.status_code == 200
    assert "No Microsoft sign-in" in page.text
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    assert client.get("/api/room").status_code == 401
    assert client.get("/api/room", headers=member).json()["active"] is False
    assert client.get("/app.js").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}


def test_member_cannot_activate_but_can_chat_once_admin_opens_room(room_client):
    client, member, admin = room_client
    message = {"author": "Volunteer", "message": "<script>not executable</script>"}
    assert client.post("/api/status", headers=member, json={"active": True}).status_code == 403
    assert client.post("/api/messages", headers=member, json=message).status_code == 409
    assert client.post("/api/status", headers=admin, json={"active": True}).status_code == 200
    assert client.post("/api/messages", headers=member, json=message).status_code == 201
    snapshot = client.get("/api/room", headers=member).json()
    assert snapshot["messages"][0]["body"] == message["message"]
    assert snapshot["messages"][0]["author"] == "Volunteer"
    assert client.post("/api/status", headers=admin, json={"active": False}).status_code == 200
    assert client.post("/api/messages", headers=member, json=message).status_code == 409


def test_room_and_contacts_survive_service_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("HAVOC365_LIFEBOAT_TOKEN", "m" * 32)
    monkeypatch.setenv("HAVOC365_LIFEBOAT_ADMIN_TOKEN", "a" * 32)
    contacts = tmp_path / "contacts.json"
    write_json(contacts, {"contacts": [{"name": "Incident desk", "contact": "+1 555 0100"}]})
    room = Room(tmp_path / "data", contacts)
    room.activate(True)
    assert room.post("Volunteer", "Checking in")
    restored = Room(tmp_path / "data").snapshot()
    assert restored["active"]
    assert restored["contacts"][0]["name"] == "Incident desk"
    assert restored["messages"][0]["body"] == "Checking in"
    assert room.path.stat().st_mode & 0o777 == 0o600


def test_invalid_or_oversized_chat_is_rejected(room_client):
    client, member, admin = room_client
    client.post("/api/status", headers=admin, json={"active": True})
    for data in (
        {"author": "", "message": "hello"},
        {"author": "Someone", "message": "x" * 2001},
        {"author": "Someone\nelse", "message": "hello"},
    ):
        assert client.post("/api/messages", headers=member, json=data).status_code == 400
    assert client.post("/api/messages", headers=member, content="x" * 20_000).status_code == 400
    assert client.post("/api/status", headers=admin, json={"active": "true"}).status_code == 400
    assert not client.get("/api/room", headers=member).json()["messages"]


def test_missing_or_identical_service_tokens_fail_locally(monkeypatch, tmp_path):
    monkeypatch.delenv("HAVOC365_LIFEBOAT_TOKEN", raising=False)
    with pytest.raises(HavocError, match="distinct"):
        create_app(tmp_path)
    monkeypatch.setenv("HAVOC365_LIFEBOAT_TOKEN", "x" * 32)
    monkeypatch.setenv("HAVOC365_LIFEBOAT_ADMIN_TOKEN", "x" * 32)
    with pytest.raises(HavocError, match="distinct"):
        create_app(tmp_path)


def test_activator_uses_admin_header_not_query_string():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"active": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    set_active(client, "https://lifeboat.example.org", "a" * 32, True)
    assert calls[0].url.path == "/api/status"
    assert calls[0].url.query == b""
    assert calls[0].headers["Authorization"] == "Bearer " + "a" * 32


def test_watch_updates_lifeboat_without_a_tenant_or_sms(monkeypatch, capsys):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"active": True})
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("havoc365.watch.httpx.Client", lambda **kwargs: client)
    monkeypatch.setenv("HAVOC365_LIFEBOAT_ADMIN_TOKEN", "a" * 32)
    assert (
        main(
            [
                "watch",
                "--url",
                "https://outlook.office.com/",
                "--failures",
                "1",
                "--once",
                "--lifeboat-url",
                "https://lifeboat.example.org",
            ]
        )
        == 1
    )
    assert len(calls) == 2
    assert calls[-1].url.host == "lifeboat.example.org"
    assert "Alert state: outage" in capsys.readouterr().out


def test_lifeboat_cli_validates_port_before_starting_server(capsys):
    assert main(["lifeboat", "serve", "--port", "0"]) == 1
    assert "between 1 and 65535" in capsys.readouterr().err


def test_sms_failure_does_not_prevent_lifeboat_activation(monkeypatch, capsys):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "lifeboat.example.org":
            return httpx.Response(200, json={"active": True})
        if request.url.host == "api.twilio.com":
            raise httpx.ReadTimeout("injected SMS timeout", request=request)
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("havoc365.watch.httpx.Client", lambda **kwargs: client)
    for key, value in {
        "HAVOC365_LIFEBOAT_ADMIN_TOKEN": "a" * 32,
        "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
        "TWILIO_AUTH_TOKEN": "offline-test-token",
        "TWILIO_FROM_NUMBER": "+15551234567",
        "TWILIO_TO_NUMBER": "+15557654321",
    }.items():
        monkeypatch.setenv(key, value)
    assert (
        main(
            [
                "watch",
                "--url",
                "https://outlook.office.com/",
                "--failures",
                "1",
                "--once",
                "--sms",
                "--lifeboat-url",
                "https://lifeboat.example.org",
            ]
        )
        == 1
    )
    hosts = [request.url.host for request in calls]
    assert hosts.index("lifeboat.example.org") < hosts.index("api.twilio.com")
    assert hosts.count("api.twilio.com") == 1
    assert "uncertain" in capsys.readouterr().err


def test_lifeboat_must_confirm_activation_not_just_return_http_200():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"active": False}))
    )
    with pytest.raises(HavocError, match="did not confirm"):
        set_active(client, "https://lifeboat.example.org", "a" * 32, True)


def test_contacts_reject_malformed_input(tmp_path):
    contacts = tmp_path / "contacts.json"
    write_json(contacts, {"contacts": [{"name": "Incident desk"}]})
    with pytest.raises(HavocError, match="name and contact"):
        Room(tmp_path / "data", contacts)


def test_chat_history_is_bounded_and_newest_messages_remain(tmp_path):
    room = Room(tmp_path)
    room.activate(True)
    with room.database() as db:
        db.executemany(
            "INSERT INTO messages(author, body, created) VALUES (?, ?, ?)",
            (
                ("Volunteer", f"Message {index}", "2026-11-16T12:00:00+00:00")
                for index in range(1000)
            ),
        )
    assert room.post("Volunteer", "Message 1000")
    with room.database() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1000
    messages = room.snapshot()["messages"]
    assert len(messages) == 200
    assert messages[-1]["body"] == "Message 1000"


def test_watch_recovers_room_after_restart(monkeypatch, tmp_path):
    from havoc365.watch import watch

    active = []
    probe_status = [503]

    def handler(request):
        if request.method == "POST":
            value = json.loads(request.content)["active"]
            active.append(value)
            return httpx.Response(200, json={"active": value})
        return httpx.Response(probe_status[0])

    make_client = httpx.Client
    monkeypatch.setattr(
        "havoc365.watch.httpx.Client",
        lambda **kwargs: make_client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setenv("HAVOC365_LIFEBOAT_ADMIN_TOKEN", "a" * 32)

    def check():
        return watch(
            ["https://outlook.office.com/"],
            tmp_path,
            once=True,
            failures=1,
            recoveries=2,
            lifeboat_url="https://lifeboat.example.org",
            announce=lambda _: None,
        )

    assert check() == 1
    probe_status[0] = 200
    assert check() == 0
    assert check() == 0
    assert active == [True, True, False]
