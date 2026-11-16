"""Bounded local attachment storage; file signatures are not malware scanning."""

import re
import sqlite3
import time
from urllib.parse import quote, unquote

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from havoc365.errors import HavocError
from havoc365.lifeboat_access import Principal
from havoc365.storage import JsonObject

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 250 * 1024 * 1024
MAX_ATTACHMENTS = 5
MAX_PENDING_UPLOADS = 1000
PENDING_SECONDS = 24 * 60 * 60


def initialize(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS attachments ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL, "
        "message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE, "
        "filename TEXT NOT NULL, content_type TEXT NOT NULL, size INTEGER NOT NULL, "
        "created INTEGER NOT NULL, data BLOB NOT NULL)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS attachments_message ON attachments(message_id)")


def cleanup(db: sqlite3.Connection) -> None:
    db.execute(
        "DELETE FROM attachments WHERE message_id IS NULL AND created<=?",
        (int(time.time()) - PENDING_SECONDS,),
    )
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='access_people'"
    ).fetchone():
        db.execute(
            "DELETE FROM attachments WHERE message_id IS NULL AND NOT EXISTS "
            "(SELECT 1 FROM access_people WHERE id=attachments.owner_id AND revoked IS NULL)"
        )


def person(db: sqlite3.Connection, user: Principal | None) -> tuple[str, str]:
    if user is None or user.id is None or user.legacy:
        raise HTTPException(403, "Join with a personal invitation before attaching files.")
    row = db.execute(
        "SELECT name, role FROM access_people WHERE id=? AND revoked IS NULL", (user.id,)
    ).fetchone()
    if row is None:
        raise HTTPException(401, "Your access was revoked. Ask an administrator for an invitation.")
    return row[0], row[1]


def identifiers(value: object) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) > MAX_ATTACHMENTS
        or any(type(item) is not int or not 1 <= item <= 2**63 - 1 for item in value)
        or len(set(value)) != len(value)
    ):
        raise HavocError("Choose up to 5 different pending attachment IDs (positive integers).")
    return value


def filename(value: str) -> str:
    try:
        name = unquote(value, errors="strict")
        valid = bool(name.strip()) and len(name) <= 255 and len(name.encode("utf-8")) <= 1024
    except (UnicodeError, ValueError) as error:
        raise HavocError("Supply a URI-encoded file name in X-File-Name.") from error
    if (
        not valid
        or name in {".", ".."}
        or any(ord(char) < 32 or ord(char) == 127 or char in "/\\" for char in name)
    ):
        raise HavocError("Use a file name of 1-255 characters without paths or control characters.")
    return name


def sniff(data: bytes | bytearray) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"\x1a\x45\xdf\xa3" and b"\x42\x82\x84webm" in data[4:4096]:
        return "video/webm"
    if data[4:8] == b"ftyp":
        size = int.from_bytes(data[:4], "big")
        if 16 <= size <= min(len(data), 4096) and size % 4 == 0:
            brands = [bytes(data[8:12])]
            brands.extend(bytes(data[index : index + 4]) for index in range(16, size, 4))
            if b"qt  " in brands:
                return "video/quicktime"
            if set(brands) & {b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V "}:
                return "video/mp4"
    return "application/octet-stream"


async def upload_body(request: Request) -> bytearray:
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal():
            raise HTTPException(400, "Supply a valid Content-Length or use a streamed upload.")
        normalized = length.lstrip("0") or "0"
        if len(normalized) > len(str(MAX_FILE_BYTES)) or int(normalized) > MAX_FILE_BYTES:
            raise HTTPException(413, "File exceeds the upload limit. Choose a file up to 25 MiB.")
    data = bytearray()
    async for chunk in request.stream():
        # Check before copying: an oversized chunk must never expand our bounded buffer.
        if len(chunk) > MAX_FILE_BYTES - len(data):
            raise HTTPException(413, "File exceeds the upload limit. Choose a file up to 25 MiB.")
        data.extend(chunk)
    return data


def metadata(row: tuple) -> JsonObject:
    value = dict(zip(("id", "filename", "content_type", "size"), row[:4], strict=True))
    value["url"] = f"/api/attachments/{value['id']}"
    return value


def decorate(db: sqlite3.Connection, messages: list[JsonObject]) -> list[JsonObject]:
    by_id = {message["id"]: message for message in messages}
    for message in messages:
        message["attachments"] = []
    for row in db.execute(
        "SELECT id, filename, content_type, size, message_id FROM attachments "
        "WHERE message_id IS NOT NULL ORDER BY id"
    ):
        if row[4] in by_id:
            by_id[row[4]]["attachments"].append(metadata(row))
    return messages


def store(
    db: sqlite3.Connection, user: Principal, name: str, data: bytes | bytearray
) -> JsonObject:
    person(db, user)
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "File exceeds the upload limit. Choose a file up to 25 MiB.")
    used, pending = db.execute(
        "SELECT COALESCE(SUM(size), 0), COUNT(CASE WHEN message_id IS NULL THEN 1 END) "
        "FROM attachments"
    ).fetchone()
    if used + len(data) > MAX_TOTAL_BYTES or pending >= MAX_PENDING_UPLOADS:
        raise HTTPException(
            507,
            "Attachment storage is full. Delete unused pending uploads or ask an "
            "administrator to review retained messages before retrying.",
        )
    kind = sniff(data)
    cursor = db.execute(
        "INSERT INTO attachments(owner_id, filename, content_type, size, created, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user.id, name, kind, len(data), int(time.time()), data),
    )
    return metadata((cursor.lastrowid, name, kind, len(data)))


def associate(
    db: sqlite3.Connection, user: Principal | None, attachment_ids: list[int], message_id: int
) -> None:
    if not attachment_ids:
        return
    person(db, user)
    for identifier in attachment_ids:
        cursor = db.execute(
            "UPDATE attachments SET message_id=? WHERE id=? AND owner_id=? AND message_id IS NULL",
            (message_id, identifier, user.id),
        )
        if cursor.rowcount != 1:
            raise HavocError(
                "Attachment is unavailable, already posted or belongs to someone else. "
                "Upload your file again."
            )


def _pending_access(db: sqlite3.Connection, user: Principal, owner: int) -> bool:
    if user.id is None:
        return False
    row = db.execute(
        "SELECT role FROM access_people WHERE id=? AND revoked IS NULL", (user.id,)
    ).fetchone()
    return row is not None and (user.id == owner or row[0] == "admin")


def delete(db: sqlite3.Connection, user: Principal, identifier: int) -> None:
    row = db.execute(
        "SELECT owner_id, message_id FROM attachments WHERE id=?", (identifier,)
    ).fetchone()
    if row is None or not _pending_access(db, user, row[0]):
        raise HTTPException(404, "Pending attachment not found. Reload your uploads.")
    if row[1] is not None:
        raise HTTPException(409, "Posted attachments cannot be deleted independently of a message.")
    db.execute("DELETE FROM attachments WHERE id=?", (identifier,))


def byte_range(value: str, size: int) -> tuple[int, int]:
    def bounded_integer(digits: str) -> int:
        normalized = digits.lstrip("0") or "0"
        return size if len(normalized) > len(str(size)) else min(int(normalized), size)

    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value.strip(), re.IGNORECASE)
    if match is not None and size:
        first, last = match.groups()
        if first or last:
            if first:
                start = bounded_integer(first)
                end = min(bounded_integer(last), size - 1) if last else size - 1
                if 0 <= start <= end and start < size:
                    return start, end
            elif bounded_integer(last) > 0:
                return max(0, size - bounded_integer(last)), size - 1
    raise HTTPException(
        416,
        "Choose a single byte range within this file.",
        headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
    )


def download(
    db: sqlite3.Connection,
    user: Principal,
    identifier: int,
    range_header: str | None,
    *,
    head: bool = False,
) -> Response:
    row = db.execute(
        "SELECT id, filename, content_type, size, owner_id, message_id FROM attachments WHERE id=?",
        (identifier,),
    ).fetchone()
    if row is None or (row[5] is None and not _pending_access(db, user, row[4])):
        raise HTTPException(404, "Attachment not found or not yet posted.")
    if (
        user.id is not None
        and not db.execute(
            "SELECT 1 FROM access_people WHERE id=? AND revoked IS NULL", (user.id,)
        ).fetchone()
    ):
        raise HTTPException(401, "Your access was revoked. Ask an administrator for an invitation.")
    size = row[3]
    start, end = byte_range(range_header, size) if range_header is not None else (0, size - 1)
    length = end - start + 1
    disposition = "attachment" if row[2] == "application/octet-stream" else "inline"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Disposition": f'{disposition}; filename="attachment-{row[0]}"; '
        f"filename*=UTF-8''{quote(row[1], safe='')}",
    }
    if range_header is not None:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    content = b""
    if not head:
        content = db.execute(
            "SELECT substr(data, ?, ?) FROM attachments WHERE id=?",
            (start + 1, length, identifier),
        ).fetchone()[0]
    return Response(
        content,
        status_code=206 if range_header is not None else 200,
        headers=headers,
        media_type=row[2],
    )
