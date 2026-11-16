import hmac
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from havoc365.errors import HavocError
from havoc365.storage import JsonObject, read_json
from havoc365.watch import target_label


def tokens() -> tuple[str, str]:
    member = os.environ.get("HAVOC365_LIFEBOAT_TOKEN", "")
    admin = os.environ.get("HAVOC365_LIFEBOAT_ADMIN_TOKEN", "")
    if min(len(member), len(admin)) < 32 or member == admin:
        raise HavocError(
            "Set distinct HAVOC365_LIFEBOAT_TOKEN and HAVOC365_LIFEBOAT_ADMIN_TOKEN "
            "values of at least 32 characters. Generate random tokens and share only "
            "the member token."
        )
    return member, admin


class Room:
    def __init__(self, directory: Path, contacts: Path | None = None) -> None:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.path = directory / "lifeboat.sqlite3"
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        with self.database() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS room "
                "(id INTEGER PRIMARY KEY, active INTEGER, contacts TEXT)"
            )
            db.execute("INSERT OR IGNORE INTO room VALUES (1, 0, '[]')")
            db.execute(
                "CREATE TABLE IF NOT EXISTS messages "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, author TEXT, body TEXT, created TEXT)"
            )
        if contacts is not None:
            entries = read_json(
                contacts,
                recovery="Create or repair the contacts JSON file, "
                "or omit --contacts to start without importing contacts.",
            ).get("contacts")
            if (
                not isinstance(entries, list)
                or len(entries) > 1000
                or any(
                    not isinstance(item, dict)
                    or set(item) != {"name", "contact"}
                    or any(
                        not isinstance(item[key], str)
                        or not item[key].strip()
                        or len(item[key]) > 200
                        for key in ("name", "contact")
                    )
                    for item in entries
                )
            ):
                raise HavocError(
                    "Contacts must contain up to 1000 objects with name and contact strings "
                    "(max 200 characters each)."
                )
            with self.database() as db:
                db.execute("UPDATE room SET contacts=? WHERE id=1", (json.dumps(entries),))

    @contextmanager
    def database(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def snapshot(self) -> JsonObject:
        with self.database() as db:
            row = db.execute("SELECT active, contacts FROM room WHERE id=1").fetchone()
            messages = db.execute(
                "SELECT id, author, body, created FROM messages ORDER BY id DESC LIMIT 200"
            ).fetchall()
        return {
            "active": bool(row[0]),
            "contacts": json.loads(row[1]),
            "messages": [
                dict(zip(("id", "author", "body", "created"), message, strict=True))
                for message in reversed(messages)
            ],
        }

    def activate(self, active: bool) -> None:
        with self.database() as db:
            db.execute("UPDATE room SET active=? WHERE id=1", (int(active),))

    def post(self, author: str, message: str) -> bool:
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT active FROM room WHERE id=1").fetchone()[0]:
                return False
            db.execute(
                "INSERT INTO messages(author, body, created) VALUES (?, ?, ?)",
                (author, message, datetime.now(UTC).isoformat()),
            )
            db.execute(
                "DELETE FROM messages WHERE id NOT IN "
                "(SELECT id FROM messages ORDER BY id DESC LIMIT 1000)"
            )
        return True


def create_app(directory: Path, contacts: Path | None = None) -> Starlette:
    member, admin = tokens()
    room = Room(directory, contacts)

    def authorized(request: Request, *, administrator: bool = False) -> bool:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return False
        supplied = header.removeprefix("Bearer ").encode()
        return hmac.compare_digest(supplied, admin.encode()) or (
            not administrator and hmac.compare_digest(supplied, member.encode())
        )

    async def payload(request: Request) -> JsonObject:
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 16_384:
                raise HavocError("Request body exceeds 16 KiB.")
        try:
            value = json.loads(data)
        except (ValueError, UnicodeDecodeError) as error:
            raise HavocError("Supply a valid JSON object.") from error
        if not isinstance(value, dict):
            raise HavocError("Supply a valid JSON object.")
        return value

    async def snapshot(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "A member token is required."}, status_code=401)
        return JSONResponse(await run_in_threadpool(room.snapshot))

    async def post(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "A member token is required."}, status_code=401)
        try:
            value = await payload(request)
        except HavocError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        author, message = value.get("author"), value.get("message")
        if (
            not isinstance(author, str)
            or not 1 <= len(author.strip()) <= 80
            or any(ord(char) < 32 for char in author)
            or not isinstance(message, str)
            or not 1 <= len(message.strip()) <= 2000
        ):
            return JSONResponse(
                {"error": "Use a name (1-80 characters) and message (1-2000 characters)."},
                status_code=400,
            )
        if not await run_in_threadpool(room.post, author.strip(), message.strip()):
            return JSONResponse(
                {"error": "The room is on standby. Ask an administrator to activate it."},
                status_code=409,
            )
        return JSONResponse({"accepted": True}, status_code=201)

    async def status(request: Request) -> Response:
        if not authorized(request, administrator=True):
            return JSONResponse({"error": "An administrator token is required."}, status_code=403)
        try:
            value = await payload(request)
        except HavocError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        if not isinstance(value.get("active"), bool):
            return JSONResponse({"error": "active must be true or false."}, status_code=400)
        await run_in_threadpool(room.activate, value["active"])
        return JSONResponse({"active": value["active"]})

    async def static(request: Request) -> Response:
        name = request.path_params.get("name", "index.html")
        types = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}
        if name not in types:
            return Response(status_code=404)
        content = files("havoc365").joinpath("web", name).read_bytes()
        return Response(content, media_type=types[name])

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def harden(request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; "
                "script-src 'self'; style-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            }
        )
        return response

    return Starlette(
        routes=[
            Route("/", static),
            Route("/healthz", health),
            Route("/api/room", snapshot),
            Route("/api/messages", post, methods=["POST"]),
            Route("/api/status", status, methods=["POST"]),
            Route("/{name}", static),
        ],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=harden)],
    )


def set_active(client: httpx.Client, url: str, token: str, active: bool) -> None:
    target_label(url)
    if len(token) < 32:
        raise HavocError("Set HAVOC365_LIFEBOAT_ADMIN_TOKEN to the lifeboat administrator token.")
    try:
        response = client.post(
            url.rstrip("/") + "/api/status",
            headers={"Authorization": f"Bearer {token}"},
            json={"active": active},
        )
    except httpx.TransportError as error:
        raise HavocError(
            "Cannot reach the lifeboat. Check its URL, TLS and host availability."
        ) from error
    if response.status_code != 200:
        raise HavocError(
            f"Lifeboat activation returned HTTP {response.status_code}. Check its admin token."
        )
    try:
        value = response.json()
    except ValueError as error:
        raise HavocError(
            "Lifeboat returned invalid JSON. Check the URL and reverse proxy."
        ) from error
    if not isinstance(value, dict) or value.get("active") is not active:
        raise HavocError(
            "Lifeboat did not confirm the requested state. Check its URL and server logs."
        )


def serve(directory: Path, port: int, contacts: Path | None = None) -> None:
    if not 1 <= port <= 65535:
        raise HavocError("Choose a TCP port between 1 and 65535.")
    app = create_app(directory, contacts)
    print(
        f"Lifeboat listening at http://127.0.0.1:{port}. "
        "Expose it only behind an HTTPS reverse proxy."
    )
    print(
        "Room starts in its saved state. Names are self-reported; "
        "share the member token out of band."
    )
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
