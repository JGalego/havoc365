import base64
import json
import struct
import zlib
from urllib.parse import quote, urlsplit

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
    state = {
        "offline": False,
        "fail_send": False,
        "drop_send_response": False,
        "fail_upload": False,
        "fail_remove": False,
        "requests": [],
    }
    external = []
    with TestClient(create_app(tmp_path / "room", contacts)) as client, sync_playwright() as driver:
        browser = driver.chromium.launch(args=["--host-resolver-rules=MAP * ~NOTFOUND"])
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        def respond(route):
            request = route.request
            parts = urlsplit(request.url)
            request_record = {
                "method": request.method,
                "path": parts.path,
                "headers": request.headers,
                "body": (
                    request.post_data
                    if request.headers.get("content-type", "").startswith("application/json")
                    else None
                ),
                "query": parts.query,
            }
            state["requests"].append(request_record)
            if parts.hostname != "lifeboat.invalid":
                external.append(request.url)
                route.abort()
            elif (
                (parts.path == "/api/room" and state["offline"])
                or (parts.path == "/api/messages" and state["fail_send"])
                or (parts.path == "/api/attachments" and state["fail_upload"])
                or (
                    request.method == "DELETE"
                    and parts.path.startswith("/api/attachments/")
                    and state["fail_remove"]
                )
            ):
                route.abort()
            else:
                # The browser owns its cookies; TestClient must not lend it another session.
                client.cookies.clear()
                response = client.request(
                    request.method,
                    request.url,
                    headers=request.headers,
                    content=request.post_data_buffer,
                )
                request_record["status"] = response.status_code
                request_record["policy"] = response.headers.get("content-security-policy")
                if parts.path == "/api/messages" and state["drop_send_response"]:
                    route.abort()
                else:
                    route.fulfill(
                        status=response.status_code,
                        headers=dict(response.headers),
                        body=response.content,
                    )
                client.cookies.clear()

        context.route("**/*", respond)
        try:
            page.goto("https://lifeboat.invalid/")
            yield page, client, member, admin, state
            assert external == [], "The lifeboat must not request third-party resources"
        finally:
            context.close()
            browser.close()


def join(page, member):
    page.get_by_label("Access code", exact=True).fill(member)
    page.get_by_role("button", name="Join room", exact=True).click()
    expect(page.locator("#workspace")).to_be_visible()
    expect(page.locator("#status")).not_to_have_text("Connecting")


def invite(client, admin, name="Alex Example", role="member"):
    response = client.post(
        "/api/admin/invites", headers=admin, json={"name": name, "role": role, "hours": 48}
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_join_screen_is_concise_and_branding_loads_without_overflow(incident_room, width):
    page, _, _, _, _ = incident_room
    page.set_viewport_size({"width": width, "height": 844})
    assert len(page.locator("body").inner_text().split()) <= 30
    for source in ("/logo.png", "/lifebuoy.png"):
        assert page.locator(f"img[src='{source}']").first.evaluate(
            "image => image.complete && image.naturalWidth > 0"
        )
    assert page.locator("#join-submit").evaluate(
        "button => button.getBoundingClientRect().bottom <= innerHeight"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_text("Need a code?", exact=True).click()
    expect(page.locator("#token-help")).to_be_visible()
    expect(page.locator("#token-help")).to_contain_text("incident lead")


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
    page.get_by_label("Your name (unverified)", exact=True).fill("Volunteer")
    page.get_by_label("Your message", exact=True).fill(message)
    state["offline"] = True
    expect(page.locator("#status")).to_have_text("Reconnecting")
    expect(page.get_by_role("button", name="Send", exact=True)).to_be_disabled()
    expect(page.get_by_label("Your message", exact=True)).to_have_value(message)
    state["offline"] = False
    page.get_by_role("button", name="Retry now").click()
    expect(page.locator("#status")).to_have_text("Chat open")
    page.get_by_label("Your message", exact=True).press("Control+Enter")
    expect(page.locator(".message-body").last).to_have_text(message)
    expect(page.locator("#messages img")).to_have_count(0)
    expect(page.get_by_label("Your message", exact=True)).to_have_value("")
    page.get_by_role("button", name="Sign out", exact=True).click()
    expect(page.locator("#messages")).to_be_empty()
    expect(page.locator("#workspace")).to_be_hidden()


def test_mobile_failure_is_visible_after_poll_and_standby_keeps_draft(incident_room):
    page, client, member, admin, state = incident_room
    page.set_viewport_size({"width": 390, "height": 844})
    client.post("/api/status", headers=admin, json={"active": True})
    join(page, member)
    expect(page.locator("#room-title")).to_be_in_viewport(ratio=1)
    page.get_by_role("tab", name="Contacts", exact=True).click()
    expect(page.locator("#room-main")).to_be_hidden()
    expect(page.locator("#contacts-panel")).to_be_visible()
    page.get_by_role("searchbox", name="Search contacts").fill("network")
    page.get_by_role("tab", name="Contacts", exact=True).press("ArrowLeft")
    expect(page.get_by_role("tab", name="Chat", exact=True)).to_be_focused()
    expect(page.locator("#contacts-panel")).to_be_hidden()
    expect(page.locator("#room-main")).to_be_visible()
    page.get_by_role("tab", name="Chat", exact=True).press("End")
    expect(page.get_by_role("searchbox", name="Search contacts")).to_have_value("network")
    page.set_viewport_size({"width": 1280, "height": 900})
    expect(page.locator("#room-main")).to_be_visible()
    expect(page.locator("#contacts-panel")).to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#room-main")).to_be_hidden()
    page.get_by_role("tab", name="Chat", exact=True).click()
    page.get_by_label("Your name (unverified)", exact=True).fill("Volunteer")
    page.get_by_label("Your message", exact=True).fill("Checking in from my phone.")
    page.get_by_role("tab", name="Contacts", exact=True).click()
    page.get_by_role("tab", name="Chat", exact=True).click()
    expect(page.get_by_label("Your message", exact=True)).to_have_value(
        "Checking in from my phone."
    )
    state["fail_send"] = True
    page.get_by_role("button", name="Send", exact=True).click()
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
    expect(page.get_by_label("Your message", exact=True)).to_have_value(
        "Checking in from my phone."
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.reload()
    expect(page.locator("#workspace")).to_be_visible()
    expect(page.get_by_label("Your message", exact=True)).to_have_value("")
    assert page.evaluate("localStorage.length + sessionStorage.length") == 0


@pytest.mark.parametrize("width", [390, 1280])
def test_new_messages_do_not_move_someone_reading_older_check_ins(incident_room, width):
    page, client, member, admin, _ = incident_room
    page.set_viewport_size({"width": width, "height": 900})
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
    page.locator("#messages").evaluate("list => list.scrollTop = 120")
    first = page.locator(".message").first.element_handle()
    if width == 390:
        page.get_by_role("tab", name="Contacts", exact=True).click()
    client.post(
        "/api/messages",
        headers=headers,
        json={
            "author": "Incident desk",
            "message": "A new update has arrived.",
        },
    )
    expect(page.locator("#message-count")).to_have_text("16")
    if width == 390:
        page.get_by_role("tab", name="Chat", exact=True).click()
    expect(page.get_by_role("button", name="Jump to latest", exact=True)).to_be_visible()
    assert first.evaluate("item => item.isConnected")
    assert page.locator("#messages").evaluate("list => list.scrollTop") == 120
    page.get_by_role("button", name="Jump to latest", exact=True).click()
    expect(page.locator("#jump-latest")).to_be_hidden()


def test_personal_invitation_session_restores_and_signout_revokes_cookie(incident_room):
    page, client, _, admin, state = incident_room
    client.post("/api/status", headers=admin, json={"active": True})
    invitation = invite(client, admin)
    page.goto(f"https://lifeboat.invalid/#invite={quote(invitation['credential'])}")
    expect(page.get_by_label("Access code", exact=True)).to_have_value(invitation["credential"])
    assert page.url == "https://lifeboat.invalid/"
    expect(page.locator("#workspace")).to_be_hidden()
    page.get_by_role("button", name="Join room", exact=True).click()
    expect(page.locator("#identity")).to_have_text("As Alex Example")
    expect(page.locator("#author")).to_be_hidden()
    expect(page.locator("#tab-admin")).to_be_hidden()
    expect(page.locator("#admin-panel")).to_be_hidden()
    expect(page.get_by_label("Access code", exact=True)).to_have_value("")
    page.get_by_label("Your message", exact=True).fill("Personal message.")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(".message-header strong").last).to_have_text("Alex Example")
    page.get_by_label("Your status", exact=True).select_option("help")
    page.get_by_label("Note (optional)", exact=True).fill("Please call my known number.")
    page.get_by_role("button", name="Save check-in", exact=True).click()
    expect(page.locator("#checkin-feedback")).to_have_text("Check-in saved.")
    expect(page.locator("#checkins")).to_contain_text("Alex Example")
    expect(page.locator("#checkins")).to_contain_text("Needs help")
    cookies = page.context.cookies()
    assert cookies and all(cookie["httpOnly"] for cookie in cookies)
    page.reload()
    expect(page.locator("#workspace")).to_be_visible()
    expect(page.locator("#identity")).to_have_text("As Alex Example")
    expect(page.locator("#checkins")).to_contain_text("Please call my known number.")
    expect(page.get_by_label("Your status", exact=True)).to_have_value("help")
    assert page.evaluate("localStorage.length + sessionStorage.length") == 0
    page.get_by_role("button", name="Sign out", exact=True).click()
    expect(page.locator("#workspace")).to_be_hidden()
    assert not page.context.cookies()
    page.reload()
    expect(page.locator("#join")).to_be_visible()
    assert page.evaluate("fetch('/api/session').then(response => response.status)") == 401
    posts = [
        request
        for request in state["requests"]
        if request["method"] == "POST" and request["path"] != "/api/session"
    ]
    assert {request["path"] for request in posts} >= {
        "/api/messages",
        "/api/checkins",
        "/api/logout",
    }
    assert all(request["headers"].get("x-csrf-token") for request in posts)
    assert all(request["headers"].get("cookie") for request in posts)
    assert all("authorization" not in request["headers"] for request in state["requests"])
    assert all(not request["query"] for request in state["requests"])


def test_accepted_message_with_lost_response_is_not_resent(incident_room):
    page, client, member, admin, state = incident_room
    client.post("/api/status", headers=admin, json={"active": True})
    join(page, member)
    page.get_by_label("Your name (unverified)", exact=True).fill("Volunteer")
    page.get_by_label("Your message", exact=True).fill("One message, not two.")
    state["drop_send_response"] = True
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator("#send-feedback")).to_contain_text("Not confirmed")
    expect(page.locator("#messages")).to_contain_text("One message, not two.")
    for _ in range(2):
        with page.expect_response("**/api/room"):
            pass
    expect(page.get_by_label("Your message", exact=True)).to_have_value("One message, not two.")
    expect(page.locator("#send-feedback")).to_contain_text("Check chat before retrying")
    sent = [request for request in state["requests"] if request["path"] == "/api/messages"]
    assert len(sent) == 1


def test_stale_poll_cannot_reopen_room_and_send_keeps_a_newer_draft(incident_room):
    page, client, member, admin, _ = incident_room
    client.post("/api/status", headers=admin, json={"active": True})
    join(page, member)
    page.get_by_label("Your name (unverified)", exact=True).fill("Volunteer")
    page.get_by_label("Your message", exact=True).fill("Original draft.")
    page.evaluate(
        """() => {
          const original = window.fetch;
          let holdSend = true;
          window.fetch = async (...args) => {
            const hold = args[0] === "/api/messages" && holdSend;
            if (hold) holdSend = false;
            const response = await original(...args);
            if (!hold) return response;
            return new Promise(resolve => {
              window.releaseSend = () => resolve(response);
              document.body.dataset.sendHeld = "true";
            });
          };
        }"""
    )
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator("body")).to_have_attribute("data-send-held", "true")
    page.get_by_label("Your message", exact=True).fill("A newer draft.")
    page.evaluate("window.releaseSend()")
    expect(page.locator("#send-feedback")).to_have_text("Message sent.")
    expect(page.get_by_label("Your message", exact=True)).to_have_value("A newer draft.")
    page.evaluate(
        """() => {
          const original = window.fetch;
          let holdRoom = true;
          window.fetch = async (...args) => {
            const hold = args[0] === "/api/room" && holdRoom;
            if (hold) holdRoom = false;
            const response = await original(...args);
            if (!hold) return response;
            return new Promise(resolve => {
              window.releaseRoom = () => resolve(response);
              document.body.dataset.roomHeld = "true";
            });
          };
          void refresh();
        }"""
    )
    expect(page.locator("body")).to_have_attribute("data-room-held", "true")
    client.post("/api/status", headers=admin, json={"active": False})
    page.evaluate("void refresh()")
    expect(page.locator("#status")).to_have_text("Standby")
    page.evaluate("window.releaseRoom()")
    page.evaluate(
        "new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    expect(page.locator("#status")).to_have_text("Standby")
    expect(page.locator("#draft-notice")).to_be_visible()
    expect(page.get_by_label("Your message", exact=True)).to_have_value("A newer draft.")


@pytest.mark.parametrize("width", [320, 1280])
def test_admin_invites_people_and_edits_incident_and_contacts(incident_room, width):
    page, client, _, admin, _ = incident_room
    page.set_viewport_size({"width": width, "height": 900})
    page.on("dialog", lambda dialog: dialog.accept())
    client.post("/api/status", headers=admin, json={"active": True})
    join(page, admin["Authorization"][7:])
    if width == 1280:
        assert page.locator("#send-submit").evaluate(
            "button => button.getBoundingClientRect().bottom <= innerHeight"
        )
    page.get_by_role("tab", name="Admin", exact=True).click()
    expect(page.locator("#admin-panel")).to_be_visible()
    page.get_by_label("Room control", exact=True).select_option("open")
    page.get_by_label("Title", exact=True).fill("Phone bridge")
    page.get_by_label("Update", exact=True).fill("Use the independent bridge.")
    page.get_by_label("Incident lead", exact=True).fill("Alex")
    page.get_by_label("Next update", exact=True).fill("15:30 UTC")
    page.get_by_role("button", name="Save incident", exact=True).click()
    expect(page.locator("#incident-feedback")).to_have_text("Incident saved.")
    expect(page.locator("#status")).to_have_text("Chat open")
    expect(page.locator("#incident-banner")).to_contain_text("Phone bridge")
    expect(page.locator("#incident-banner")).to_contain_text("Next update: 15:30 UTC")
    page.get_by_label("Person's name", exact=True).fill("Backup lead")
    page.get_by_label("Role", exact=True).select_option("admin")
    page.get_by_role("button", name="Create invitation", exact=True).click()
    expect(page.locator("#new-invite")).to_be_visible()
    code = page.locator("#invite-code").input_value()
    assert code and page.locator("#invite-link").input_value().endswith(f"/#invite={code}")
    expect(page.locator("#new-invite-expiry")).to_contain_text("Expires")
    expect(page.locator("#invites")).to_contain_text("Pending")
    expect(page.locator("#invites")).to_contain_text("Backup lead")
    page.get_by_role("button", name="Add contact", exact=True).click()
    last_row = page.locator(".contact-edit-row").last
    last_row.get_by_label("Contact name", exact=True).fill("Backup desk")
    last_row.get_by_label("Contact detail", exact=True).fill("+1 555 010 1234")
    page.get_by_role("tab", name="Chat", exact=True).click()
    page.get_by_role("tab", name="Admin", exact=True).click()
    expect(last_row.get_by_label("Contact name", exact=True)).to_have_value("Backup desk")
    page.get_by_role("button", name="Save contacts", exact=True).click()
    expect(page.locator("#contacts-edit-feedback")).to_have_text("Contacts saved.")
    page.get_by_role("tab", name="Contacts", exact=True).click()
    expect(page.locator("#contacts")).to_contain_text("Backup desk")
    expect(page.get_by_role("link", name="+1 555 010 1234", exact=True)).to_have_attribute(
        "href", "tel:+15550101234"
    )
    page.get_by_role("tab", name="Admin", exact=True).click()
    page.get_by_role("button", name="Revoke invitation for Backup lead", exact=True).click()
    expect(page.locator("#invites")).to_contain_text("Revoked")
    with page.expect_download() as downloaded:
        page.get_by_role("button", name="Export incident JSON", exact=True).click()
    assert downloaded.value.suggested_filename == "lifeboat-incident.json"
    page.get_by_label("Room control", exact=True).select_option("standby")
    page.get_by_role("button", name="Save incident", exact=True).click()
    expect(page.locator("#status")).to_have_text("Standby")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    response = client.get("/api/admin", headers=admin)
    assert response.status_code == 200
    assert any(entry["name"] == "Backup lead" for entry in response.json()["invites"])


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_populated_bulletin_keeps_desktop_send_visible_without_phone_overflow(incident_room, width):
    page, client, _, admin, _ = incident_room
    page.set_viewport_size({"width": width, "height": 900})
    bulletin = {
        "mode": "open",
        "title": "Offline exercise",
        "update": "Use this room for check-ins. Call the incident desk if you need help.",
        "lead": "Incident desk",
        "next_update": "16:00 UTC",
    }
    response = client.post("/api/admin/incident", headers=admin, json=bulletin)
    assert response.status_code == 200, response.text
    join(page, admin["Authorization"][7:])
    expect(page.locator("#chat")).to_be_visible()
    for field in ("title", "update", "lead", "next_update"):
        expect(page.locator("#incident-banner")).to_contain_text(bulletin[field])
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if width == 1280:
        expect(page.locator("#send-submit")).to_be_in_viewport(ratio=1)
        assert page.locator("#send-submit").evaluate(
            "button => button.getBoundingClientRect().bottom <= innerHeight"
        )


def test_member_has_no_admin_controls_and_personal_roster_is_shared(incident_room):
    page, client, member, admin, _ = incident_room
    client.post("/api/status", headers=admin, json={"active": True})
    invitation = invite(client, admin)
    join(page, invitation["credential"])
    page.get_by_role("button", name="Save check-in", exact=True).click()
    expect(page.locator("#checkin-count")).to_have_text("1")
    page.get_by_role("button", name="Sign out", exact=True).click()
    expect(page.locator("#join")).to_be_visible()
    join(page, member)
    expect(page.locator("#tab-admin")).to_be_hidden()
    expect(page.locator("#checkin-form")).to_be_hidden()
    expect(page.locator("#checkin-help")).to_contain_text("personal invitation")
    expect(page.locator("#checkins")).to_contain_text("Alex Example")
    assert page.evaluate("fetch('/api/admin').then(response => response.status)") == 403


@pytest.mark.parametrize("file_format", ["html", "json", "txt"])
def test_explicit_offline_download_contains_contacts_not_credentials_or_messages(
    incident_room, tmp_path, file_format
):
    page, client, member, admin, state = incident_room
    contacts = [
        {"name": "<script>alert('x')</script>", "contact": "javascript:alert(1)"},
        {"name": 'A & "B"', "contact": "desk@fallback.example"},
        {"name": "Phone", "contact": "tel:+1 555 010 1234"},
        {"name": "Not a safe mail link", "contact": "mailto:desk@example.org?subject=private"},
    ]
    result = client.post("/api/admin/contacts", headers=admin, json={"contacts": contacts})
    assert result.status_code == 200, result.text
    client.post("/api/status", headers=admin, json={"active": True})
    client.post(
        "/api/messages",
        headers={"Authorization": f"Bearer {member}"},
        json={"author": "Incident desk", "message": "PRIVATE MESSAGE MUST NOT BE EXPORTED"},
    )
    join(page, member)
    page.get_by_role("tab", name="Contacts", exact=True).click()
    expect(page.locator("#contacts a")).to_have_count(2)
    expect(page.locator("#contacts script")).to_have_count(0)
    page.get_by_text("Download contacts offline", exact=True).click()
    page.get_by_label("File format", exact=True).select_option(file_format)
    with page.expect_download() as downloaded:
        page.get_by_role("button", name="Download contacts", exact=True).click()
    path = tmp_path / downloaded.value.suggested_filename
    downloaded.value.save_as(path)
    content = path.read_text()
    assert member not in content
    assert admin["Authorization"][7:] not in content
    assert "PRIVATE MESSAGE MUST NOT BE EXPORTED" not in content
    assert page.evaluate("localStorage.length + sessionStorage.length") == 0
    assert not page.evaluate("caches.keys().then(keys => keys.length)")
    if file_format == "html":
        assert "<script>" not in content
        assert "&lt;script&gt;" in content
        # Navigate the actual downloaded file with every network request blocked.
        offline = page.context.new_page()
        offline.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith("file:") else route.abort()
            ),
        )
        offline.goto(path.as_uri())
        expect(offline.get_by_role("heading", name="Lifeboat contacts")).to_be_visible()
        expect(offline.locator("tbody tr")).to_have_count(4)
        expect(offline.get_by_role("rowheader").first).to_have_text(contacts[0]["name"])
        expect(offline.locator("script")).to_have_count(0)
        expect(offline.locator("a")).to_have_count(2)
        offline.close()
    elif file_format == "json":
        assert json.loads(content) == {"contacts": contacts}
    else:
        assert all(entry["contact"] in content for entry in contacts)
    assert not any(request["path"].endswith("/export") for request in state["requests"])


def small_png():
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\x00\xff\x00"))
        + chunk(b"IEND", b"")
    )


def small_webm():
    # One VP9 frame generated locally from a 16x16 black image.
    return base64.b64decode(
        "GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwH/////////EU2bdKtNu4tTq4QV"
        "SalmU6yBoU27i1OrhBZUrmtTrIHNTbuMU6uEElTDZ1OsggET7AEAAAAAAABoAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmpyrXsYMPQkBNgI1MYXZmNTkuMjcuMTAwV0GN"
        "TGF2ZjU5LjI3LjEwMBZUrmvBrgEAAAAAAAA414EBc8WIYR2GwYdbPE+cgQAitZyDdW5kiIEAhoVWX1ZQ"
        "OYOBASPjg4Q7msoA4ImwgRC6gRCagQISVMNn3HNzoGPAgGfImkWjh0VOQ09ERVJEh41MYXZmNTkuMjcu"
        "MTAwc3O2Y8CLY8WIYR2GwYdbPE9nyKVFo4dFTkNPREVSRIeYTGF2YzU5LjM3LjEwMCBsaWJ2cHgtdnA5"
        "H0O2daXngQCjoIEAAICCSYNCAADwAPYAOCQcGEoAADBgAAAQv//9SIwA"
    )


@pytest.mark.parametrize("width", [390, 1280])
def test_personal_member_uploads_images_and_downloads_other_files(incident_room, tmp_path, width):
    page, client, _, admin, state = incident_room
    page.set_viewport_size({"width": width, "height": 900})
    client.post("/api/status", headers=admin, json={"active": True})
    join(page, invite(client, admin)["credential"])
    expect(page.locator("#attachment-hint")).to_have_text("5 files · 25 MiB each")
    expect(page.locator("#attachment-details > p")).to_be_hidden()
    expect(page.locator("#checkins")).to_have_text("No check-ins yet")
    if width == 1280:
        assert page.locator("#send-submit").evaluate(
            "button => button.getBoundingClientRect().bottom <= innerHeight"
        )
    page.get_by_text("File safety", exact=True).click()
    expect(page.locator("#attachment-details")).to_contain_text("private to you and admins")
    expect(page.locator("#attachment-details")).to_contain_text("expire after 24 hours")
    expect(page.locator("#attachment-details")).to_contain_text("aren't malware-scanned")
    page.get_by_text("File safety", exact=True).click()
    image = {"name": "photo <one>.png", "mimeType": "image/png", "buffer": small_png()}
    video = {"name": "bridge.webm", "mimeType": "application/octet-stream", "buffer": small_webm()}
    document = {
        "name": "instructions <safe>.html",
        "mimeType": "text/html",
        "buffer": b"<!doctype html><script>window.untrustedAttachment = true</script>",
    }
    page.locator("#attachments").set_input_files([image, video, document])
    expect(page.locator("#draft-attachments li")).to_have_count(3)
    expect(page.locator("#draft-attachments")).not_to_contain_text("Uploading")
    expect(page.get_by_role("button", name="Send", exact=True)).to_be_enabled()
    page.get_by_role("tab", name="Contacts", exact=True).click()
    page.get_by_role("tab", name="Chat", exact=True).click()
    expect(page.locator("#draft-attachments")).to_contain_text("photo <one>.png")
    page.get_by_role(
        "button", name="Remove attachment instructions <safe>.html", exact=True
    ).click()
    expect(page.locator("#draft-attachments li")).to_have_count(2)
    page.locator("#attachments").set_input_files(document)
    expect(page.get_by_role("button", name="Send", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator("#message-count")).to_have_text("1")
    expect(page.locator("#draft-attachments")).to_be_empty()
    expect(page.locator(".message-attachments img")).to_have_count(1)
    expect(page.locator(".message-attachments img")).to_have_attribute("alt", "photo <one>.png")
    expect(page.locator(".message-attachments video")).to_have_count(1)
    expect(page.locator(".message-attachments video")).to_have_js_property("videoWidth", 16)
    expect(page.locator(".message-attachments video")).to_have_attribute("controls", "")
    expect(page.locator(".message-attachments script")).to_have_count(0)
    assert not page.evaluate("Boolean(window.untrustedAttachment)")
    expect(page.locator(".message-attachments img")).to_have_js_property("naturalWidth", 2)
    with page.expect_download() as downloaded:
        page.get_by_role("link", name="Download instructions <safe>.html", exact=False).click()
    destination = tmp_path / "downloaded-document.html"
    assert downloaded.value.failure() is None, "\n".join(
        f"{request['method']} {request['path']} -> {request.get('status')}"
        for request in state["requests"]
        if request["path"].startswith("/api/attachments")
    )
    downloaded.value.save_as(destination)
    assert destination.read_bytes() == document["buffer"]
    page.reload()
    expect(page.locator(".message-attachments img")).to_be_visible()
    expect(page.locator("#draft-attachments")).to_be_empty()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    mutations = [
        request
        for request in state["requests"]
        if request["path"].startswith("/api/attachments")
        and request["method"] in {"POST", "DELETE"}
    ]
    assert len(mutations) == 5
    assert all(request["headers"].get("x-csrf-token") for request in mutations)
    assert all(request["headers"].get("cookie") for request in mutations)
    assert all("authorization" not in request["headers"] for request in mutations)


def test_upload_removal_failures_and_uncertain_send_keep_attachment_drafts(incident_room):
    page, client, _, admin, state = incident_room
    page.on("dialog", lambda dialog: dialog.accept())
    client.post("/api/status", headers=admin, json={"active": True})
    join(page, invite(client, admin)["credential"])
    image = {"name": "photo.png", "mimeType": "image/png", "buffer": small_png()}
    page.locator("#attachments").set_input_files([image] * 6)
    expect(page.locator("#attachment-feedback")).to_contain_text("At most 5 files")
    expect(page.locator("#draft-attachments")).to_be_empty()
    assert not any(request["path"] == "/api/attachments" for request in state["requests"])
    state["fail_upload"] = True
    page.locator("#attachments").set_input_files(image)
    expect(page.locator("#draft-attachments")).to_contain_text("Upload not confirmed")
    expect(page.get_by_role("button", name="Send", exact=True)).to_be_disabled()
    page.get_by_role("button", name="Remove attachment photo.png", exact=True).click()
    expect(page.locator("#draft-attachments")).to_be_empty()
    state["fail_upload"] = False
    page.locator("#attachments").set_input_files(image)
    expect(page.locator("#draft-attachments")).to_contain_text("Ready to send")
    state["fail_remove"] = True
    page.get_by_role("button", name="Remove attachment photo.png", exact=True).click()
    expect(page.locator("#draft-attachments")).to_contain_text("Removal not confirmed")
    state["fail_remove"] = False
    page.get_by_role("button", name="Discard from draft", exact=True).click()
    expect(page.locator("#draft-attachments")).to_be_empty()
    page.locator("#attachments").set_input_files(image)
    expect(page.get_by_role("button", name="Send", exact=True)).to_be_enabled()
    state["drop_send_response"] = True
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator("#send-feedback")).to_contain_text("Not confirmed")
    expect(page.locator("#draft-attachments")).to_contain_text("Already in chat")
    expect(page.get_by_role("button", name="Send", exact=True)).to_be_disabled()
    for _ in range(2):
        with page.expect_response("**/api/room"):
            pass
    sent = [request for request in state["requests"] if request["path"] == "/api/messages"]
    assert len(sent) == 1
    assert len(json.loads(sent[0]["body"])["attachment_ids"]) == 1
    expect(page.locator(".message-attachments img")).to_have_count(1)
    page.get_by_role("button", name="Remove attachment photo.png", exact=True).click()
    expect(page.locator("#draft-attachments")).to_be_empty()
    page.locator("#attachments").set_input_files(image)
    expect(page.locator("#draft-attachments")).to_contain_text("Ready to send")
    page.get_by_role("button", name="Sign out", exact=True).click()
    expect(page.locator("#workspace")).to_be_hidden()
    expect(page.locator("#draft-attachments")).to_be_empty()
