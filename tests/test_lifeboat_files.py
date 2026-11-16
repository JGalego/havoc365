import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import quote

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.testclient import TestClient

from havoc365 import lifeboat_files
from havoc365.errors import HavocError
from havoc365.lifeboat import COOKIE, Room, backup, create_app, restore
from havoc365.lifeboat_access import Access

MEMBER = "member-" + "m" * 40
ADMIN = "admin-" + "a" * 40


def png():
    def chunk(kind, value):
        return (
            struct.pack(">I", len(value))
            + kind
            + value
            + struct.pack(">I", zlib.crc32(kind + value))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def files(monkeypatch, tmp_path):
    monkeypatch.setenv("HAVOC365_LIFEBOAT_TOKEN", MEMBER)
    monkeypatch.setenv("HAVOC365_LIFEBOAT_ADMIN_TOKEN", ADMIN)
    monkeypatch.delenv("HAVOC365_LIFEBOAT_LEGACY_ACCESS", raising=False)
    directory = tmp_path / "live"
    app = create_app(directory)
    access = Access(directory / "lifeboat.sqlite3", MEMBER, ADMIN)
    administrator = access.login(ADMIN)
    first = access.login(
        access.invite(administrator.user, "First responder", "member", 24)["credential"]
    )
    second = access.login(
        access.invite(administrator.user, "Second responder", "member", 24)["credential"]
    )
    room = Room(directory)
    room.activate(True)
    with TestClient(app, base_url="https://lifeboat.test") as client:
        yield client, room, access, administrator, first, second, tmp_path


def headers(session):
    return {"Cookie": f"{COOKIE}={session.token}", "X-CSRF-Token": session.csrf}


def upload(client, session, data=None, name="photo.png", kind="image/png"):
    return client.post(
        "/api/attachments",
        headers={**headers(session), "X-File-Name": quote(name, safe=""), "Content-Type": kind},
        content=png() if data is None else data,
    )


def publish(client, session, *attachments, message="", **extra):
    return client.post(
        "/api/messages",
        headers=headers(session),
        json={"message": message, "attachment_ids": list(attachments), **extra},
    )


def test_image_upload_message_download_and_personal_author_binding(files):
    client, room, _, admin, first, second, _ = files
    response = upload(client, first, name='café "status".png', kind="text/html")
    assert response.status_code == 201, response.text
    file = response.json()
    assert file == {
        "id": file["id"],
        "filename": 'café "status".png',
        "content_type": "image/png",
        "size": len(png()),
        "url": f"/api/attachments/{file['id']}",
    }
    assert publish(client, first, file["id"], author="Impersonator").status_code == 201
    message = room.snapshot()["messages"][-1]
    assert message["author"] == "First responder"
    assert message["body"] == ""
    assert message["attachments"] == [file]
    response = client.get(file["url"], headers=headers(second))
    assert response.content == png()
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"].startswith("inline;")
    assert (
        "filename*=UTF-8''caf%C3%A9%20%22status%22.png" in response.headers["content-disposition"]
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert publish(client, first, file["id"]).status_code == 400
    assert client.delete(file["url"], headers=headers(admin)).status_code == 409
    assert len(room.snapshot()["messages"]) == 1
    assert room.export()["messages"][0]["attachments"] == [file]


@pytest.mark.parametrize(
    ("data", "name", "kind"),
    [
        (b"<!doctype html><script>alert('not an image')</script>", "spoof.png", "image/png"),
        (
            b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>',
            "drawing.svg",
            "image/svg+xml",
        ),
        (b"MZ\x00offline-file-transport-test", "program.exe", "video/mp4"),
        (b"", "empty.txt", "text/plain"),
    ],
)
def test_untrusted_or_active_content_is_always_a_sandboxed_download(files, data, name, kind):
    client, _, _, _, first, _, _ = files
    response = upload(client, first, data, name, kind)
    assert response.status_code == 201
    file = response.json()
    assert file["content_type"] == "application/octet-stream"
    response = client.get(file["url"], headers=headers(first))
    assert response.content == data
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "sandbox allow-downloads" in response.headers["content-security-policy"]
    assert "script-src 'self'" in response.headers["content-security-policy"]


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (png(), "image/png"),
        (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00", "image/jpeg"),
        (b"GIF89a\x01\x00\x01\x00", "image/gif"),
        (b"RIFF\x04\x00\x00\x00WEBP", "image/webp"),
        (struct.pack(">I", 20) + b"ftypisom\x00\x00\x00\x00mp42", "video/mp4"),
        (struct.pack(">I", 16) + b"ftypqt  \x00\x00\x00\x00", "video/quicktime"),
        (b"\x1a\x45\xdf\xa3\x87\x42\x82\x84webm", "video/webm"),
    ],
)
def test_magic_signature_recognizes_supported_inline_media(data, kind):
    assert lifeboat_files.sniff(data) == kind


def test_upload_requires_authentication_csrf_and_nonlegacy_identity(files):
    client, _, _, admin, first, _, _ = files
    assert client.post("/api/attachments", content=png()).status_code == 401
    assert (
        client.post(
            "/api/attachments",
            headers={"Cookie": f"{COOKIE}={first.token}", "X-File-Name": "photo.png"},
            content=png(),
        ).status_code
        == 403
    )
    for token in (MEMBER, ADMIN):
        response = client.post(
            "/api/attachments",
            headers={"Authorization": f"Bearer {token}", "X-File-Name": "photo.png"},
            content=png(),
        )
        assert response.status_code == 403
        assert "personal invitation" in response.json()["error"]
    response = client.post(
        "/api/attachments",
        headers={**headers(first), "Origin": "https://elsewhere.test", "X-File-Name": "photo.png"},
        content=png(),
    )
    assert response.status_code == 403
    assert upload(client, admin).status_code == 201


def test_pending_files_are_private_and_can_only_be_deleted_by_owner_or_admin(files):
    client, room, _, admin, first, second, _ = files
    file = upload(client, first).json()
    url = file["url"]
    assert client.get(url).status_code == 401
    assert client.head(url).status_code == 401
    for method in (client.get, client.head, client.delete):
        assert method(url, headers=headers(second)).status_code == 404
    assert publish(client, second, file["id"]).status_code == 400
    assert not room.snapshot()["messages"]
    assert client.get(url, headers=headers(admin)).content == png()
    assert client.delete(url, headers={"Cookie": f"{COOKIE}={first.token}"}).status_code == 403
    assert client.get(url, headers=headers(first)).status_code == 200
    assert client.delete(url, headers=headers(first)).json() == {"deleted": True}
    assert client.get(url, headers=headers(first)).status_code == 404
    another = upload(client, first).json()
    assert client.delete(another["url"], headers=headers(admin)).status_code == 200


def test_revocation_removes_pending_uploads_and_blocks_published_fetches(files):
    client, room, _, admin, first, second, _ = files
    pending = upload(client, first).json()
    published = upload(client, first).json()
    assert publish(client, first, published["id"]).status_code == 201
    response = client.post(
        f"/api/admin/people/{first.user.id}/revoke", headers=headers(admin), json={}
    )
    assert response.status_code == 200
    assert upload(client, first).status_code == 401
    for method in (client.get, client.head):
        assert method(published["url"], headers=headers(first)).status_code == 401
    assert client.get(published["url"], headers=headers(second)).content == png()
    with room.database() as db:
        assert (
            db.execute("SELECT id FROM attachments WHERE id=?", (pending["id"],)).fetchone() is None
        )


@pytest.mark.parametrize(
    "value", [None, "1", [True], [0], [-1], [1.0], [1, 1], [2**63], list(range(1, 7)), [{}]]
)
def test_attachment_lists_reject_invalid_ambiguous_and_oversized_ids(files, value):
    client, room, _, _, first, _, _ = files
    response = client.post(
        "/api/messages",
        headers=headers(first),
        json={"message": "A message", "attachment_ids": value},
    )
    assert response.status_code == 400
    assert not room.snapshot()["messages"]


def test_five_files_attach_atomically_and_legacy_text_is_unchanged(files):
    client, room, _, _, first, second, _ = files
    owned = [upload(client, first).json() for _ in range(5)]
    foreign = upload(client, second).json()
    assert publish(client, first, owned[0]["id"], foreign["id"]).status_code == 400
    with room.database() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM attachments WHERE message_id IS NOT NULL").fetchone()[
                0
            ]
            == 0
        )
    assert publish(client, first, *(file["id"] for file in owned)).status_code == 201
    assert len(room.snapshot()["messages"][0]["attachments"]) == 5
    assert publish(client, first).status_code == 400
    bearer = {"Authorization": f"Bearer {MEMBER}"}
    assert client.get(owned[0]["url"], headers=bearer).content == png()
    assert (
        client.post(
            "/api/messages", headers=bearer, json={"author": "Legacy", "message": "Still here"}
        ).status_code
        == 201
    )
    assert room.snapshot()["messages"][-1]["attachments"] == []
    assert (
        client.post(
            "/api/messages",
            headers=bearer,
            json={"author": "Legacy", "message": "", "attachment_ids": [foreign["id"]]},
        ).status_code
        == 403
    )


@pytest.mark.parametrize("name", ["", "../file", "folder\\file", ".", "\r\nheader", "\x00file"])
def test_invalid_filenames_never_become_paths_or_response_headers(files, name):
    client, _, _, _, first, _, _ = files
    assert upload(client, first, name=name).status_code == 400


def test_file_and_total_quota_enforced_and_deleting_pending_reclaims_space(files, monkeypatch):
    client, room, _, _, first, _, _ = files
    monkeypatch.setattr(lifeboat_files, "MAX_FILE_BYTES", 8)
    monkeypatch.setattr(lifeboat_files, "MAX_TOTAL_BYTES", 12)
    assert upload(client, first, b"123456789").status_code == 413
    first_file = upload(client, first, b"12345678").json()
    assert upload(client, first, b"12345").status_code == 507
    assert upload(client, first, b"1234").status_code == 201
    assert client.delete(first_file["url"], headers=headers(first)).status_code == 200
    assert upload(client, first, b"12345678").status_code == 201
    with room.database() as db:
        assert db.execute("SELECT SUM(size) FROM attachments").fetchone()[0] == 12


def test_empty_pending_uploads_are_bounded_and_expiry_frees_capacity(files, monkeypatch):
    client, room, _, _, first, _, _ = files
    monkeypatch.setattr(lifeboat_files, "MAX_PENDING_UPLOADS", 1)
    expired = upload(client, first, b"").json()
    assert upload(client, first, b"").status_code == 507
    with room.database() as db:
        db.execute(
            "UPDATE attachments SET created=created-? WHERE id=?",
            (lifeboat_files.PENDING_SECONDS, expired["id"]),
        )
    assert upload(client, first, b"").status_code == 201
    with room.database() as db:
        assert db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1


def test_standby_does_not_consume_pending_files(files):
    client, room, _, _, first, _, _ = files
    room.activate(False)
    file = upload(client, first).json()
    assert publish(client, first, file["id"]).status_code == 409
    assert client.get(file["url"], headers=headers(first)).content == png()
    room.activate(True)
    assert publish(client, first, file["id"]).status_code == 201


def test_concurrent_uploads_cannot_exceed_quota(files, monkeypatch):
    _, room, _, _, first, _, _ = files
    monkeypatch.setattr(lifeboat_files, "MAX_TOTAL_BYTES", 10)
    barrier = Barrier(2)

    def store(_):
        barrier.wait(timeout=5)
        try:
            return room.upload(first.user, "file.bin", b"12345678")
        except HTTPException as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(store, range(2)))
    assert sum(isinstance(item, dict) for item in results) == 1
    failures = [item for item in results if isinstance(item, HTTPException)]
    assert len(failures) == 1
    assert failures[0].status_code == 507
    with room.database() as db:
        assert db.execute("SELECT SUM(size) FROM attachments").fetchone()[0] == 8


@pytest.mark.anyio
async def test_stream_limit_aborts_on_the_first_extra_chunk_without_consuming_rest(monkeypatch):
    monkeypatch.setattr(lifeboat_files, "MAX_FILE_BYTES", 4)
    chunks = iter([b"1234", b"5", b"never read"])
    received = []

    async def receive():
        chunk = next(chunks)
        received.append(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}

    request = Request({"type": "http", "method": "POST", "headers": []}, receive)
    with pytest.raises(HTTPException) as failure:
        await lifeboat_files.upload_body(request)
    assert failure.value.status_code == 413
    assert received == [b"1234", b"5"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize(
    ("value", "expected", "content_range"),
    [
        ("bytes=0-3", b"0123", "bytes 0-3/10"),
        ("bytes=6-", b"6789", "bytes 6-9/10"),
        ("bytes=-3", b"789", "bytes 7-9/10"),
        ("bytes=-30", b"0123456789", "bytes 0-9/10"),
        ("bytes=8-99", b"89", "bytes 8-9/10"),
        (" Bytes=0-3 ", b"0123", "bytes 0-3/10"),
        ("bytes=0-" + "9" * 100, b"0123456789", "bytes 0-9/10"),
        ("bytes=-" + "9" * 100, b"0123456789", "bytes 0-9/10"),
        ("bytes=" + "0" * 100 + "8-9", b"89", "bytes 8-9/10"),
    ],
)
def test_single_byte_ranges_and_head(files, value, expected, content_range):
    client, _, _, _, first, _, _ = files
    file = upload(client, first, b"0123456789", "clip.mp4").json()
    request_headers = {**headers(first), "Range": value}
    response = client.get(file["url"], headers=request_headers)
    assert response.status_code == 206
    assert response.content == expected
    assert response.headers["content-range"] == content_range
    assert response.headers["content-length"] == str(len(expected))
    assert response.headers["accept-ranges"] == "bytes"
    head = client.head(file["url"], headers=request_headers)
    assert head.status_code == 206
    assert head.content == b""
    assert head.headers["content-length"] == str(len(expected))
    full_head = client.head(file["url"], headers=headers(first))
    assert full_head.status_code == 200
    assert full_head.headers["content-length"] == "10"


@pytest.mark.parametrize(
    "value",
    [
        "bytes=10-",
        "bytes=4-2",
        "bytes=-0",
        "bytes=-",
        "bytes=0-1,4-5",
        "items=0-1",
        "bytes=0-1x",
        "bytes=" + "9" * 100 + "-",
    ],
)
def test_invalid_and_multiple_ranges_are_416_without_body_leaks(files, value):
    client, _, _, _, first, _, _ = files
    file = upload(client, first, b"0123456789").json()
    response = client.get(file["url"], headers={**headers(first), "Range": value})
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */10"
    response = client.get(
        file["url"],
        headers=[*headers(first).items(), ("Range", "bytes=0-1"), ("Range", "bytes=4-5")],
    )
    assert response.status_code == 416


def test_pending_expiry_reclaims_quota_and_retention_prunes_message_blobs(files):
    client, room, _, _, first, _, _ = files
    pending = upload(client, first, b"pending").json()
    posted = upload(client, first, b"posted").json()
    assert publish(client, first, posted["id"]).status_code == 201
    with room.database() as db:
        db.execute(
            "UPDATE attachments SET created=created-? WHERE id=?",
            (lifeboat_files.PENDING_SECONDS + 1, pending["id"]),
        )
    room.snapshot()
    assert client.get(pending["url"], headers=headers(first)).status_code == 404
    with room.database() as db:
        assert db.execute("SELECT SUM(size) FROM attachments").fetchone()[0] == len(b"posted")
        db.executemany(
            "INSERT INTO messages(author, body, created) VALUES ('Test', 'retention', '')",
            [()] * 999,
        )
    assert room.post("Test", "Newest")
    assert client.get(posted["url"], headers=headers(first)).status_code == 404
    with room.database() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1000
        assert db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0


def test_online_backup_and_fresh_restore_preserve_published_and_pending_file_bytes(files):
    client, room, _, _, first, _, root = files
    posted = upload(client, first).json()
    pending = upload(client, first, b"offline report", "report.txt").json()
    assert publish(client, first, posted["id"]).status_code == 201
    saved = root / "backup.sqlite3"
    backup(room.path.parent, saved)
    restored_directory = root / "restored"
    restore(restored_directory, saved)
    restored = Room(restored_directory)
    assert restored.snapshot()["messages"][-1]["attachments"] == [posted]
    with TestClient(create_app(restored_directory), base_url="https://lifeboat.test") as fresh:
        assert fresh.get(posted["url"], headers=headers(first)).content == png()
        assert fresh.get(pending["url"], headers=headers(first)).content == b"offline report"
        assert publish(fresh, first, pending["id"]).status_code == 201


def test_direct_room_post_checks_ownership_revocation_and_rolls_back_associations(files):
    _, room, access, admin, first, second, _ = files
    first_file = room.upload(first.user, "first", b"first")
    second_file = room.upload(second.user, "second", b"second")
    with pytest.raises(HavocError, match="belongs"):
        room.post(
            "Impersonator",
            "",
            attachment_ids=[first_file["id"], second_file["id"]],
            user=first.user,
        )
    assert not room.snapshot()["messages"]
    access.revoke_person(admin.user, first.user.id)
    with pytest.raises(HTTPException) as failure:
        room.post("Impersonator", "", attachment_ids=[first_file["id"]], user=first.user)
    assert failure.value.status_code == 401
