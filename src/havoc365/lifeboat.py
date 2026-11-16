import hmac
import json
import logging
import os
import sqlite3
import tempfile
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import asynccontextmanager, closing, contextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from havoc365 import lifeboat_files
from havoc365.errors import HavocError
from havoc365.lifeboat_access import Access, Principal, Session, init_audit, record
from havoc365.storage import JsonObject, read_json

LOG = logging.getLogger(__name__)
COOKIE = "lifeboat_session"


def contact_entries(entries: object) -> list[JsonObject]:
    if (
        not isinstance(entries, list)
        or len(entries) > 1000
        or any(
            not isinstance(item, dict)
            or set(item) != {"name", "contact"}
            or any(
                not isinstance(item[key], str) or not item[key].strip() or len(item[key]) > 200
                for key in ("name", "contact")
            )
            for item in entries
        )
    ):
        raise HavocError(
            "Contacts must contain up to 1000 objects with name and contact strings "
            "(max 200 characters each)."
        )
    return [{"name": item["name"].strip(), "contact": item["contact"].strip()} for item in entries]


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
            lifeboat_files.initialize(db)
            lifeboat_files.cleanup(db)
            init_audit(db)
            db.execute(
                "CREATE TABLE IF NOT EXISTS incident ("
                "id INTEGER PRIMARY KEY CHECK(id=1), number INTEGER NOT NULL, "
                "mode TEXT NOT NULL, title TEXT NOT NULL, update_text TEXT NOT NULL, "
                "lead TEXT NOT NULL, next_update TEXT NOT NULL, changed TEXT NOT NULL, "
                "watch_active INTEGER NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO incident SELECT 1, active, 'auto', '', '', '', '', ?, "
                "active FROM room WHERE id=1",
                (datetime.now(UTC).isoformat(),),
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS checkins ("
                "incident INTEGER NOT NULL, person_id INTEGER NOT NULL, name TEXT NOT NULL, "
                "status TEXT NOT NULL, note TEXT NOT NULL, updated TEXT NOT NULL, "
                "PRIMARY KEY(incident, person_id))"
            )
        if contacts is not None:
            entries = contact_entries(
                read_json(
                    contacts,
                    recovery="Create or repair the contacts JSON file, "
                    "or omit --contacts to start without importing contacts.",
                ).get("contacts")
            )
            with self.database() as db:
                db.execute("UPDATE room SET contacts=? WHERE id=1", (json.dumps(entries),))

    @contextmanager
    def database(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def snapshot(self) -> JsonObject:
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            lifeboat_files.cleanup(db)
            return self._snapshot(db)

    def _snapshot(self, db: sqlite3.Connection) -> JsonObject:
        row = db.execute("SELECT active, contacts FROM room WHERE id=1").fetchone()
        messages = db.execute(
            "SELECT id, author, body, created FROM messages ORDER BY id DESC LIMIT 200"
        ).fetchall()
        incident = self.incident(db)
        checkins = db.execute(
            "SELECT person_id, name, status, note, updated FROM checkins "
            "WHERE incident=? ORDER BY updated DESC",
            (incident["id"],),
        ).fetchall()
        return {
            "active": bool(row[0]),
            "contacts": json.loads(row[1]),
            "messages": lifeboat_files.decorate(
                db,
                [
                    dict(zip(("id", "author", "body", "created"), message, strict=True))
                    for message in reversed(messages)
                ],
            ),
            "incident": incident,
            "checkins": [
                dict(zip(("person_id", "name", "status", "note", "updated"), item, strict=True))
                for item in checkins
            ],
        }

    @staticmethod
    def incident(db: sqlite3.Connection) -> JsonObject:
        row = db.execute(
            "SELECT number, mode, title, update_text, lead, next_update, changed "
            "FROM incident WHERE id=1"
        ).fetchone()
        return dict(
            zip(
                ("id", "mode", "title", "update", "lead", "next_update", "changed"),
                row,
                strict=True,
            )
        )

    def activate(self, active: bool, *, source: str = "manual", actor: str = "CLI") -> JsonObject:
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            if source == "watcher":
                db.execute("UPDATE incident SET watch_active=? WHERE id=1", (int(active),))
                mode = db.execute("SELECT mode FROM incident WHERE id=1").fetchone()[0]
            else:
                mode = "open" if active else "standby"
                db.execute("UPDATE incident SET mode=? WHERE id=1", (mode,))
            effective = active if mode == "auto" else mode == "open"
            self._transition(db, effective, actor)
            return {"active": effective, "requested": active, "mode": mode}

    @staticmethod
    def _transition(db: sqlite3.Connection, active: bool, actor: str) -> None:
        previous = bool(db.execute("SELECT active FROM room WHERE id=1").fetchone()[0])
        if previous == active:
            return
        now = datetime.now(UTC).isoformat()
        if active:
            db.execute("UPDATE incident SET number=number+1 WHERE id=1")
        db.execute("UPDATE incident SET changed=? WHERE id=1", (now,))
        db.execute("UPDATE room SET active=? WHERE id=1", (int(active),))
        record(db, actor, "room.opened" if active else "room.closed")

    def configure(self, value: JsonObject, actor: str) -> JsonObject:
        mode = value.get("mode")
        if not isinstance(mode, str) or mode not in {"auto", "open", "standby"}:
            raise HavocError("Choose automatic, open or standby incident control.")
        fields = {}
        for key, limit in (("title", 120), ("update", 2000), ("lead", 200), ("next_update", 80)):
            text = value.get(key, "")
            if not isinstance(text, str) or len(text) > limit or "\x00" in text:
                raise HavocError(f"Incident {key} must be text of at most {limit} characters.")
            fields[key] = text.strip()
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            watch_active = db.execute("SELECT watch_active FROM incident WHERE id=1").fetchone()[0]
            active = bool(watch_active) if mode == "auto" else mode == "open"
            self._transition(db, active, actor)
            db.execute(
                "UPDATE incident SET mode=?, title=?, update_text=?, lead=?, next_update=?, "
                "changed=? WHERE id=1",
                (
                    mode,
                    fields["title"],
                    fields["update"],
                    fields["lead"],
                    fields["next_update"],
                    datetime.now(UTC).isoformat(),
                ),
            )
            record(db, actor, "incident.updated", json.dumps({"mode": mode, **fields}))
            return self.incident(db)

    def contacts(self, entries: object, actor: str) -> None:
        entries = contact_entries(entries)
        with self.database() as db:
            db.execute("UPDATE room SET contacts=? WHERE id=1", (json.dumps(entries),))
            record(db, actor, "contacts.updated", f"{len(entries)} contacts")

    def checkin(self, user: Principal, status: object, note: object) -> None:
        if user.id is None or user.legacy:
            raise HavocError("Join with your personal invitation before checking in.")
        if (
            not isinstance(status, str)
            or status not in {"safe", "help"}
            or not isinstance(note, str)
            or len(note) > 500
        ):
            raise HavocError("Choose safe or help and a note of at most 500 characters.")
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT active FROM room WHERE id=1").fetchone()[0]:
                raise HavocError("The room is on standby. Ask an administrator to open it.")
            number = self.incident(db)["id"]
            db.execute(
                "INSERT INTO checkins VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(incident, person_id) DO UPDATE SET "
                "status=excluded.status, note=excluded.note, updated=excluded.updated",
                (number, user.id, user.name, status, note.strip(), datetime.now(UTC).isoformat()),
            )
            record(db, user.name, "person.checked_in", f"{status}: {note.strip()}")

    def audit(self, limit: int = 200) -> list[JsonObject]:
        with self.database() as db:
            rows = db.execute(
                "SELECT id, actor, action, detail, created FROM audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            dict(zip(("id", "actor", "action", "detail", "created"), row, strict=True))
            for row in rows
        ]

    def export(self) -> JsonObject:
        with self.database() as db:
            db.execute("BEGIN")
            snapshot = self._snapshot(db)
            rows = db.execute(
                "SELECT id, author, body, created FROM messages ORDER BY id"
            ).fetchall()
            messages = lifeboat_files.decorate(
                db,
                [dict(zip(("id", "author", "body", "created"), row, strict=True)) for row in rows],
            )
        return {
            **snapshot,
            "messages": messages,
            "audit": self.audit(10000),
            "exported": datetime.now(UTC).isoformat(),
            "retention": {"messages": 1000, "audit": 10000},
        }

    def post(
        self,
        author: str,
        message: str,
        *,
        attachment_ids: list[int] | None = None,
        user: Principal | None = None,
    ) -> bool:
        attachment_ids = lifeboat_files.identifiers(
            [] if attachment_ids is None else attachment_ids
        )
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            lifeboat_files.cleanup(db)
            if user is not None and not user.legacy:
                author, _ = lifeboat_files.person(db, user)
            if not db.execute("SELECT active FROM room WHERE id=1").fetchone()[0]:
                return False
            cursor = db.execute(
                "INSERT INTO messages(author, body, created) VALUES (?, ?, ?)",
                (author, message, datetime.now(UTC).isoformat()),
            )
            lifeboat_files.associate(db, user, attachment_ids, cursor.lastrowid)
            db.execute(
                "DELETE FROM messages WHERE id NOT IN "
                "(SELECT id FROM messages ORDER BY id DESC LIMIT 1000)"
            )
        return True

    def upload(self, user: Principal, name: str, data: bytes | bytearray) -> JsonObject:
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            lifeboat_files.cleanup(db)
            return lifeboat_files.store(db, user, name, data)

    def attachment(
        self,
        user: Principal,
        identifier: int,
        range_header: str | None = None,
        *,
        head: bool = False,
    ) -> Response:
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            lifeboat_files.cleanup(db)
            return lifeboat_files.download(db, user, identifier, range_header, head=head)

    def delete_attachment(self, user: Principal, identifier: int) -> None:
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            lifeboat_files.cleanup(db)
            lifeboat_files.delete(db, user, identifier)

    def cleanup_attachments(self) -> None:
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            lifeboat_files.cleanup(db)


def create_app(directory: Path, contacts: Path | None = None) -> Starlette:
    member, admin = tokens()
    room = Room(directory, contacts)
    access = Access(room.path, member, admin)
    failures: OrderedDict[str, tuple[float, int]] = OrderedDict()

    def guard(request: Request, *, administrator: bool = False, write: bool = False) -> Principal:
        user: Principal | None = request.state.user
        if user is None:
            raise HTTPException(401, "Sign in with your invitation or access code.")
        if administrator and user.role != "admin":
            raise HTTPException(403, "An administrator token or administrator session is required.")
        if write and request.state.session is not None:
            session: Session = request.state.session
            supplied = request.headers.get("x-csrf-token", "")
            if not hmac.compare_digest(supplied.encode(), session.csrf.encode()):
                raise HTTPException(403, "Reload the room before trying this action again.")
        return user

    async def payload(request: Request, limit: int = 16_384) -> JsonObject:
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > limit:
                raise HavocError(f"Request body exceeds {limit // 1024} KiB.")
        try:
            value = json.loads(data)
        except (ValueError, UnicodeDecodeError) as error:
            raise HavocError("Supply a valid JSON object.") from error
        if not isinstance(value, dict):
            raise HavocError("Supply a valid JSON object.")
        return value

    async def session_info(request: Request) -> Response:
        if request.method == "GET":
            guard(request)
            session = request.state.session
            if session is None:
                raise HTTPException(401, "Exchange your access code for a browser session.")
            return JSONResponse(session.public())
        host = request.client.host if request.client else "local"
        now = time.monotonic()
        started, count = failures.get(host, (now, 0))
        if now - started >= 60:
            started, count = now, 0
        if count >= 20:
            raise HTTPException(
                429,
                "Too many failed sign-ins. Wait one minute and retry.",
                headers={"Retry-After": "60"},
            )
        value = await payload(request)
        credential = value.get("credential")
        if not isinstance(credential, str) or not 1 <= len(credential) <= 512:
            raise HavocError("Supply the invitation or recovery access code.")
        try:
            session = await run_in_threadpool(access.login, credential)
        except HavocError as error:
            failures[host] = (started, count + 1)
            failures.move_to_end(host)
            while len(failures) > 1000:
                failures.popitem(last=False)
            raise HTTPException(401, str(error)) from error
        failures.pop(host, None)
        response = JSONResponse(session.public())
        response.set_cookie(
            COOKIE,
            session.token,
            max_age=max(0, session.expires - int(time.time())),
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
        )
        return response

    async def logout(request: Request) -> Response:
        guard(request, write=True)
        raw = request.cookies.get(COOKIE, "")
        await run_in_threadpool(access.logout, raw)
        response = JSONResponse({"signed_out": True})
        response.delete_cookie(
            COOKIE, secure=request.url.scheme == "https", httponly=True, samesite="strict"
        )
        return response

    async def snapshot(request: Request) -> Response:
        guard(request)
        return JSONResponse(await run_in_threadpool(room.snapshot))

    async def post(request: Request) -> Response:
        user = guard(request, write=True)
        value = await payload(request)
        author = value.get("author") if user.legacy else user.name
        message = value.get("message", "")
        attachment_ids = lifeboat_files.identifiers(value.get("attachment_ids", []))
        if (
            not isinstance(author, str)
            or not 1 <= len(author.strip()) <= 80
            or any(ord(char) < 32 for char in author)
            or not isinstance(message, str)
            or len(message.strip()) > 2000
            or (not message.strip() and not attachment_ids)
        ):
            return JSONResponse(
                {
                    "error": "Use a name (1-80 characters) and message (1-2000 characters), "
                    "or attach a file."
                },
                status_code=400,
            )
        if not await run_in_threadpool(
            room.post,
            author.strip(),
            message.strip(),
            attachment_ids=attachment_ids,
            user=user,
        ):
            return JSONResponse(
                {"error": "The room is on standby. Ask an administrator to activate it."},
                status_code=409,
            )
        return JSONResponse({"accepted": True}, status_code=201)

    async def upload(request: Request) -> Response:
        user = guard(request, write=True)
        if user.id is None or user.legacy:
            raise HTTPException(403, "Join with a personal invitation before attaching files.")
        name = lifeboat_files.filename(request.headers.get("x-file-name", ""))
        data = await lifeboat_files.upload_body(request)
        return JSONResponse(await run_in_threadpool(room.upload, user, name, data), status_code=201)

    async def attachment(request: Request) -> Response:
        user = guard(request, write=request.method == "DELETE")
        identifier = request.path_params["identifier"]
        if identifier > 2**63 - 1:
            raise HTTPException(404, "Attachment not found.")
        if request.method == "DELETE":
            await run_in_threadpool(room.delete_attachment, user, identifier)
            return JSONResponse({"deleted": True})
        ranges = request.headers.getlist("range")
        return await run_in_threadpool(
            room.attachment,
            user,
            identifier,
            ",".join(ranges) if ranges else None,
            head=request.method == "HEAD",
        )

    async def status(request: Request) -> Response:
        user = guard(request, administrator=True, write=True)
        value = await payload(request)
        if value.get("mode") == "auto":
            current = (await run_in_threadpool(room.snapshot))["incident"]
            await run_in_threadpool(room.configure, {**current, "mode": "auto"}, user.name)
            current_room = await run_in_threadpool(room.snapshot)
            return JSONResponse({"active": current_room["active"], "mode": "auto"})
        if not isinstance(value.get("active"), bool):
            return JSONResponse({"error": "active must be true or false."}, status_code=400)
        source = value.get("source", "manual")
        if source not in ("manual", "watcher"):
            raise HavocError("Use manual or watcher activation.")
        return JSONResponse(
            await run_in_threadpool(room.activate, value["active"], source=source, actor=user.name)
        )

    async def checkin(request: Request) -> Response:
        user = guard(request, write=True)
        value = await payload(request)
        await run_in_threadpool(room.checkin, user, value.get("status"), value.get("note", ""))
        return JSONResponse({"accepted": True})

    async def administration(request: Request) -> Response:
        user = guard(request, administrator=True)
        value = await run_in_threadpool(access.directory, user)
        value["audit"] = await run_in_threadpool(room.audit)
        return JSONResponse(value)

    async def invitation(request: Request) -> Response:
        user = guard(request, administrator=True, write=True)
        value = await payload(request)
        result = await run_in_threadpool(
            access.invite, user, value.get("name"), value.get("role"), value.get("hours", 24)
        )
        return JSONResponse(result, status_code=201)

    async def revoke(request: Request) -> Response:
        user = guard(request, administrator=True, write=True)
        actions = {"people": access.revoke_person, "invites": access.revoke_invite}
        action = actions.get(request.path_params["kind"])
        if action is None:
            raise HTTPException(404, "Unknown directory action.")
        await run_in_threadpool(action, user, request.path_params["identifier"])
        await run_in_threadpool(room.cleanup_attachments)
        return JSONResponse({"revoked": True})

    async def configure(request: Request) -> Response:
        user = guard(request, administrator=True, write=True)
        return JSONResponse(
            await run_in_threadpool(room.configure, await payload(request), user.name)
        )

    async def contacts_update(request: Request) -> Response:
        user = guard(request, administrator=True, write=True)
        value = await payload(request, 512 * 1024)
        await run_in_threadpool(room.contacts, value.get("contacts"), user.name)
        return JSONResponse({"saved": True})

    async def export(request: Request) -> Response:
        user = guard(request, administrator=True)
        value = await run_in_threadpool(room.export)
        value.update(await run_in_threadpool(access.directory, user))
        return JSONResponse(
            value, headers={"Content-Disposition": 'attachment; filename="incident.json"'}
        )

    async def static(request: Request) -> Response:
        name = request.path_params.get("name", "index.html")
        types = {
            "index.html": "text/html",
            "app.js": "text/javascript",
            "style.css": "text/css",
            "logo.png": "image/png",
            "lifebuoy.png": "image/png",
        }
        if name not in types:
            return Response(status_code=404)
        content = files("havoc365").joinpath("web", name).read_bytes()
        return Response(content, media_type=types[name])

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def ready(request: Request) -> Response:
        await run_in_threadpool(room.snapshot)
        return JSONResponse({"status": "ready"})

    async def failure(request: Request, error: Exception) -> Response:
        if isinstance(error, HTTPException):
            return JSONResponse(
                {"error": str(error.detail)}, status_code=error.status_code, headers=error.headers
            )
        if isinstance(error, HavocError):
            return JSONResponse({"error": str(error)}, status_code=400)
        LOG.error("Lifeboat database operation failed", exc_info=error)
        return JSONResponse(
            {
                "error": "Room storage is unavailable. Retry or contact the incident lead; "
                "an administrator must check the disk and database."
            },
            status_code=503,
        )

    async def harden(request: Request, call_next) -> Response:
        origin = request.headers.get("origin")
        expected = str(request.base_url).rstrip("/")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and (
            (origin is not None and origin != expected)
            or request.headers.get("sec-fetch-site") == "cross-site"
        ):
            response = JSONResponse(
                {"error": "Cross-site actions are not allowed."}, status_code=403
            )
        else:
            request.state.user = None
            request.state.session = None
            header = request.headers.get("authorization", "")
            try:
                if header.startswith("Bearer "):
                    request.state.user = access.resolve_bearer(header[7:])
                elif not header and request.cookies.get(COOKIE):
                    session = await run_in_threadpool(access.session, request.cookies[COOKIE])
                    if session is not None:
                        request.state.session = session
                        request.state.user = session.user
                response = await call_next(request)
            except sqlite3.Error as error:
                response = await failure(request, error)
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
        if response.headers.get("content-disposition", "").startswith("attachment;"):
            response.headers["Content-Security-Policy"] += "; sandbox allow-downloads"
        return response

    @asynccontextmanager
    async def lifespan(app: Starlette):
        with service_lock(directory):
            yield

    return Starlette(
        routes=[
            Route("/", static),
            Route("/healthz", health),
            Route("/readyz", ready),
            Route("/api/session", session_info, methods=["GET", "POST"]),
            Route("/api/logout", logout, methods=["POST"]),
            Route("/api/room", snapshot),
            Route("/api/messages", post, methods=["POST"]),
            Route("/api/attachments", upload, methods=["POST"]),
            Route("/api/attachments/{identifier:int}", attachment, methods=["GET", "DELETE"]),
            Route("/api/checkins", checkin, methods=["POST"]),
            Route("/api/status", status, methods=["POST"]),
            Route("/api/admin", administration),
            Route("/api/admin/invites", invitation, methods=["POST"]),
            Route("/api/admin/{kind}/{identifier:int}/revoke", revoke, methods=["POST"]),
            Route("/api/admin/incident", configure, methods=["POST"]),
            Route("/api/admin/contacts", contacts_update, methods=["POST"]),
            Route("/api/admin/export", export),
            Route("/{name}", static),
        ],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=harden)],
        exception_handlers={HTTPException: failure, HavocError: failure, sqlite3.Error: failure},
        lifespan=lifespan,
    )


def validate_url(url: str) -> str:
    guidance = (
        "Use an HTTPS lifeboat origin without a path, query or credentials. "
        "For local testing, HTTP is allowed only at 127.0.0.1 or [::1]."
    )
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise HavocError(guidance) from error
    if (
        not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or any(char.isspace() for char in url)
        or (port is not None and not 1 <= port <= 65535)
        or not (
            parts.scheme == "https"
            or (parts.scheme == "http" and parts.hostname in {"127.0.0.1", "::1"})
        )
    ):
        raise HavocError(guidance)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _control(client: httpx.Client, url: str, token: str, payload: JsonObject) -> JsonObject:
    url = validate_url(url)
    if len(token) < 32:
        raise HavocError("Set HAVOC365_LIFEBOAT_ADMIN_TOKEN to the lifeboat administrator token.")
    try:
        response = client.post(
            url + "/api/status",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            follow_redirects=False,
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
    if not isinstance(value, dict) or not isinstance(value.get("active"), bool):
        raise HavocError(
            "Lifeboat did not confirm the requested state. Check its URL and server logs."
        )
    return value


def set_active(
    client: httpx.Client,
    url: str,
    token: str,
    active: bool,
    *,
    source: str = "manual",
) -> None:
    payload: JsonObject = {"active": active}
    if source == "watcher":
        payload["source"] = source
    elif source != "manual":
        raise HavocError("Use manual or watcher activation.")
    value = _control(client, url, token, payload)
    held = (
        source == "watcher"
        and value.get("mode") in ("open", "standby")
        and value.get("requested") is active
    )
    if value["active"] is not active and not held:
        raise HavocError(
            "Lifeboat did not confirm the requested state. Check its URL and server logs."
        )


def set_automatic(client: httpx.Client, url: str, token: str) -> None:
    value = _control(client, url, token, {"mode": "auto"})
    if value.get("mode") != "auto":
        raise HavocError("Lifeboat did not confirm automatic control. Check its server version.")


@contextmanager
def service_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / ".lifeboat-service.sqlite3"
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    path.chmod(0o600)
    db = sqlite3.connect(path, timeout=0)
    try:
        try:
            db.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as error:
            raise HavocError(
                "Lifeboat is already running or being restored. Stop the service before "
                "restoring; run only one server instance per state directory."
            ) from error
        yield
    finally:
        db.rollback()
        db.close()


def _check_backup(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise HavocError("The backup failed SQLite integrity checks. Use a known-good backup.")
    try:
        row = db.execute("SELECT id, active, contacts FROM room WHERE id=1").fetchone()
        db.execute("SELECT id, author, body, created FROM messages LIMIT 1")
    except sqlite3.Error as error:
        raise HavocError(
            "This is not a Lifeboat backup. Select its SQLite database backup."
        ) from error
    if row is None or row[1] not in (0, 1):
        raise HavocError("This backup has no valid room state. Use a known-good backup.")
    try:
        contact_entries(json.loads(row[2]))
    except (ValueError, TypeError) as error:
        raise HavocError(
            "The backup contains invalid contacts. Use a known-good backup."
        ) from error


def backup(directory: Path, output: Path) -> None:
    source = directory / "lifeboat.sqlite3"
    if not source.is_file():
        raise HavocError("No Lifeboat database exists here. Start the server before backing up.")
    if source.resolve() == output.resolve() or output.exists():
        raise HavocError("Backup destination already exists. Choose a new private backup file.")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".lifeboat-backup-", dir=output.parent)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original:
            destination = sqlite3.connect(temporary)
            try:
                original.backup(destination)
                _check_backup(destination)
            finally:
                destination.close()
        with open(temporary, "rb") as stream:
            os.fsync(stream.fileno())
        os.link(temporary, output)
        descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except sqlite3.Error as error:
        raise HavocError(
            "Cannot create the backup. Check database integrity and disk space."
        ) from error
    finally:
        Path(temporary).unlink(missing_ok=True)


def restore(directory: Path, source: Path, *, replace: bool = False) -> None:
    if not source.is_file():
        raise HavocError("Backup file does not exist. Supply an existing Lifeboat backup.")
    target = directory / "lifeboat.sqlite3"
    if source.resolve() == target.resolve():
        raise HavocError("The backup and live database are the same file. Choose a backup copy.")
    with service_lock(directory):
        if target.exists() and not replace:
            raise HavocError("A database already exists. Back it up first, then use --replace.")
        original = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            _check_backup(original)
            descriptor = os.open(target, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(descriptor)
            target.chmod(0o600)
            destination = sqlite3.connect(target)
            try:
                original.backup(destination)
                _check_backup(destination)
            finally:
                destination.close()
        except sqlite3.Error as error:
            raise HavocError(
                "Cannot restore this backup. Check its integrity and available disk."
            ) from error
        finally:
            original.close()


def serve(directory: Path, port: int, contacts: Path | None = None) -> None:
    if not 1 <= port <= 65535:
        raise HavocError("Choose a TCP port between 1 and 65535.")
    app = create_app(directory, contacts)
    print(
        f"Lifeboat listening at http://127.0.0.1:{port}. "
        "Expose it only behind an HTTPS reverse proxy."
    )
    print(
        "Room starts in its saved state. Sign in with an invitation or recovery admin code. "
        "Keep recovery codes outside Microsoft."
    )
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
