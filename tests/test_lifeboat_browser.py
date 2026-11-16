from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright
from starlette.testclient import TestClient

from havoc365.lifeboat import create_app
from havoc365.storage import write_json


@pytest.fixture
def incident_room(monkeypatch, tmp_path):
    member = "offline-member-" + "m" * 32
    admin = {"Authorization": "Bearer offline-admin-" + "a" * 32}
    monkeypatch.setenv("HAVOC365_LIFEBOAT_TOKEN", member)
    monkeypatch.setenv("HAVOC365_LIFEBOAT_ADMIN_TOKEN", admin["Authorization"][7:])
    contacts = tmp_path / "contacts.json"
    write_json(
        contacts,
        {
            "contacts": [
                {"name": "Incident desk", "contact": "incident@fallback.example"},
                {"name": "Network desk", "contact": "network@fallback.example"},
                {"name": "Site lead", "contact": "site@fallback.example"},
            ]
        },
    )
    state = {"offline": False, "fail_send": False}
    external = []
    with TestClient(create_app(tmp_path / "room", contacts)) as client, sync_playwright() as driver:
        browser = driver.chromium.launch(args=["--host-resolver-rules=MAP * ~NOTFOUND"])
        context = browser.new_context()
        page = context.new_page()

        def respond(route):
            request = route.request
            parts = urlsplit(request.url)
            if parts.hostname != "lifeboat.invalid":
                external.append(request.url)
                route.abort()
            elif (parts.path == "/api/room" and state["offline"]) or (
                parts.path == "/api/messages" and state["fail_send"]
            ):
                route.abort()
            else:
                response = client.request(
                    request.method,
                    request.url,
                    headers=request.headers,
                    content=request.post_data_buffer,
                )
                route.fulfill(
                    status=response.status_code,
                    headers=dict(response.headers),
                    body=response.content,
                )

        context.route("**/*", respond)
        try:
            page.goto("https://lifeboat.invalid/")
            yield page, client, member, admin, state
            assert external == [], "The lifeboat must not request third-party resources"
        finally:
            context.close()
            browser.close()


def join(page, member):
    page.get_by_label("Member token", exact=True).fill(member)
    page.get_by_role("button", name="Join room", exact=True).click()
    expect(page.locator("#workspace")).to_be_visible()


def test_standby_contacts_are_searchable_without_activating_chat(incident_room):
    page, _, member, _, _ = incident_room
    join(page, member)
    expect(page.locator("#status")).to_have_text("Standby")
    expect(page.locator("#chat")).to_be_hidden()
    page.get_by_role("button", name="Find a contact").click()
    expect(page.get_by_role("searchbox", name="Search contacts")).to_be_focused()
    page.get_by_role("searchbox").fill("network")
    expect(page.locator("#contacts .contact")).to_have_count(1)
    expect(page.locator("#contacts")).to_contain_text("network@fallback.example")
    page.get_by_role("searchbox").fill("no matching contact")
    expect(page.locator("#contacts")).to_contain_text("No contacts match")
    expect(page.locator("#contact-count")).to_have_text("0 / 3")


def test_connection_loss_preserves_draft_then_keyboard_send_is_plain_text(incident_room):
    page, client, member, admin, state = incident_room
    page.set_viewport_size({"width": 1280, "height": 900})
    client.post("/api/status", headers=admin, json={"active": True})
    client.post(
        "/api/messages",
        headers={"Authorization": f"Bearer {member}"},
        json={
            "author": "Incident desk",
            "message": "The room is open for check-ins.",
        },
    )
    join(page, member)
    assert page.locator("#send-submit").evaluate(
        "button => button.getBoundingClientRect().bottom <= innerHeight"
    )
    message = "<img src=x onerror=alert(1)> is plain text."
    page.get_by_label("Your name", exact=True).fill("Volunteer")
    page.get_by_label("Your check-in", exact=True).fill(message)
    state["offline"] = True
    expect(page.locator("#status")).to_have_text("Reconnecting")
    expect(page.get_by_role("button", name="Send check-in")).to_be_disabled()
    expect(page.get_by_label("Your check-in", exact=True)).to_have_value(message)
    state["offline"] = False
    page.get_by_role("button", name="Retry now").click()
    expect(page.locator("#status")).to_have_text("Chat open")
    page.get_by_label("Your check-in", exact=True).press("Control+Enter")
    expect(page.locator(".message-body").last).to_have_text(message)
    expect(page.locator("#messages img")).to_have_count(0)
    expect(page.get_by_label("Your check-in", exact=True)).to_have_value("")
    page.get_by_role("button", name="Leave room").click()
    expect(page.locator("#messages")).to_be_empty()
    expect(page.locator("#workspace")).to_be_hidden()


def test_mobile_failure_is_visible_after_poll_and_standby_keeps_draft(incident_room):
    page, client, member, admin, state = incident_room
    page.set_viewport_size({"width": 390, "height": 844})
    client.post("/api/status", headers=admin, json={"active": True})
    join(page, member)
    expect(page.locator("#room-title")).to_be_in_viewport(ratio=1)
    page.get_by_role("button", name="Contacts", exact=True).click()
    expect(page.get_by_role("searchbox", name="Search contacts")).to_be_focused()
    page.get_by_label("Your name", exact=True).fill("Volunteer")
    page.get_by_label("Your check-in", exact=True).fill("Checking in from my phone.")
    state["fail_send"] = True
    page.get_by_role("button", name="Send check-in").click()
    expect(page.locator("#send-feedback")).to_contain_text("Not confirmed")
    for _ in range(2):
        with page.expect_response("**/api/room"):
            pass
    expect(page.locator("#send-feedback")).to_contain_text("Not confirmed")
    client.post("/api/status", headers=admin, json={"active": False})
    expect(page.locator("#standby")).to_be_visible()
    expect(page.locator("#draft-notice")).to_be_visible()
    client.post("/api/status", headers=admin, json={"active": True})
    expect(page.locator("#chat")).to_be_visible()
    expect(page.get_by_label("Your check-in", exact=True)).to_have_value(
        "Checking in from my phone."
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.reload()
    expect(page.locator("#join")).to_be_visible()
    expect(page.locator("#workspace")).to_be_hidden()
    assert page.evaluate("localStorage.length + sessionStorage.length") == 0


def test_new_messages_do_not_move_someone_reading_older_check_ins(incident_room):
    page, client, member, admin, _ = incident_room
    client.post("/api/status", headers=admin, json={"active": True})
    headers = {"Authorization": f"Bearer {member}"}
    for index in range(15):
        client.post(
            "/api/messages",
            headers=headers,
            json={
                "author": "Incident desk",
                "message": f"Update {index}: " + "A longer check-in for the incident room. " * 8,
            },
        )
    join(page, member)
    page.locator("#messages").evaluate("list => list.scrollTop = 0")
    first = page.locator(".message").first.element_handle()
    client.post(
        "/api/messages",
        headers=headers,
        json={
            "author": "Incident desk",
            "message": "A new update has arrived.",
        },
    )
    expect(page.get_by_role("button", name="New check-ins. Jump to latest")).to_be_visible()
    assert first.evaluate("item => item.isConnected")
    assert page.locator("#messages").evaluate("list => list.scrollTop") == 0
    page.get_by_role("button", name="New check-ins. Jump to latest").click()
    expect(page.locator("#jump-latest")).to_be_hidden()
