import json
from urllib.parse import parse_qs

import httpx
import pytest

from havoc365.errors import HavocError
from havoc365.storage import write_json
from havoc365.watch import invite_roster, watch


def test_outage_creates_personal_invites_then_sends_each_recipient_sms(monkeypatch, tmp_path):
    roster = tmp_path / "roster.json"
    write_json(
        roster,
        {
            "people": [
                {"name": "First operator", "phone": "+15551234567"},
                {"name": "Second operator", "phone": "+15557654321"},
            ]
        },
    )
    for key, value in {
        "HAVOC365_LIFEBOAT_ADMIN_TOKEN": "a" * 32,
        "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
        "TWILIO_AUTH_TOKEN": "offline-test-token",
        "TWILIO_FROM_NUMBER": "+15550000000",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TWILIO_TO_NUMBER", raising=False)
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/api/status":
            return httpx.Response(200, json={"active": True})
        if request.url.path == "/api/admin/invites":
            name = json.loads(request.content)["name"]
            return httpx.Response(201, json={"credential": name.replace(" ", "-") + "x" * 32})
        if request.url.host == "api.twilio.com":
            return httpx.Response(201, json={"status": "queued"})
        return httpx.Response(503)

    make_client = httpx.Client
    monkeypatch.setattr(
        "havoc365.watch.httpx.Client",
        lambda **kwargs: make_client(transport=httpx.MockTransport(handler)),
    )
    output = []
    assert (
        watch(
            ["https://outlook.office.com/"],
            tmp_path,
            once=True,
            failures=1,
            sms=True,
            lifeboat_url="https://lifeboat.test",
            invite_roster_path=roster,
            announce=output.append,
        )
        == 1
    )
    invites = [request for request in calls if request.url.path == "/api/admin/invites"]
    messages = [request for request in calls if request.url.host == "api.twilio.com"]
    assert len(invites) == len(messages) == 2
    assert json.loads(invites[0].content) == {
        "name": "First operator",
        "role": "member",
        "hours": 24,
    }
    first = parse_qs(messages[0].content.decode())
    second = parse_qs(messages[1].content.decode())
    assert first["To"] == ["+15551234567"]
    assert second["To"] == ["+15557654321"]
    assert "/#invite=First-operator" in first["Body"][0]
    assert "/#invite=Second-operator" in second["Body"][0]
    assert "First-operator" not in "\n".join(output)
    assert "delivery is not yet confirmed" in "\n".join(output)


def test_automatic_invites_require_explicit_paid_sms_and_lifeboat(tmp_path):
    with pytest.raises(HavocError, match="requires --sms"):
        watch(
            ["https://outlook.office.com/"],
            tmp_path,
            once=True,
            invite_roster_path=tmp_path / "private.json",
        )


@pytest.mark.parametrize(
    "people",
    [
        [],
        [{"name": "Operator", "phone": "not a phone"}],
        [{"name": "Operator", "phone": "+15551234567"}] * 2,
        [{"name": "Operator", "phone": "+15551234567", "password": "no"}],
    ],
)
def test_invalid_rosters_fail_before_sending_anything(tmp_path, people):
    path = tmp_path / "people.json"
    write_json(path, {"people": people})
    with pytest.raises(HavocError):
        invite_roster(path)
