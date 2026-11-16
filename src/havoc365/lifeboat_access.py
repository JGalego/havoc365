"""Local, durable Lifeboat identities and shared bounded audit storage."""

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from havoc365.errors import HavocError
from havoc365.storage import JsonObject

type Role = Literal["member", "admin"]

_SESSION_SECONDS = 30 * 24 * 60 * 60
_DIRECTORY_LIMIT = 1000
_SESSION_LIMIT = 10_000
_RECOVERY_NAME = "Recovery administrator"


def _stamp() -> str:
    return datetime.fromtimestamp(time.time(), UTC).isoformat()


def _hash(value: str) -> str:
    try:
        return hashlib.sha256(value.encode()).hexdigest()
    except UnicodeEncodeError as error:
        raise HavocError("Supply valid Unicode text without unpaired surrogates.") from error


def init_audit(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS audit "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, action TEXT, "
        "detail TEXT, created TEXT)"
    )


def record(db: sqlite3.Connection, actor: str, action: str, detail: str = "") -> None:
    """Append to the caller's transaction; do not pass credentials as audit details."""
    db.execute(
        "INSERT INTO audit(actor, action, detail, created) VALUES (?, ?, ?, ?)",
        (actor, action, detail, _stamp()),
    )
    db.execute(
        "DELETE FROM audit WHERE id <= "
        "COALESCE((SELECT id FROM audit ORDER BY id DESC LIMIT 1 OFFSET 10000), 0)"
    )


@dataclass(frozen=True)
class Principal:
    id: int | None
    name: str
    role: Role
    legacy: bool = False

    def public(self) -> JsonObject:
        return {"id": self.id, "name": self.name, "role": self.role, "legacy": self.legacy}


@dataclass(frozen=True)
class Session:
    token: str
    csrf: str
    expires: int
    user: Principal

    def public(self) -> JsonObject:
        return {"user": self.user.public(), "csrf": self.csrf, "expires": self.expires}


class Access:
    def __init__(self, path: Path, member_token: str, admin_token: str) -> None:
        legacy_access = os.environ.get("HAVOC365_LIFEBOAT_LEGACY_ACCESS", "1")
        if legacy_access not in ("0", "1"):
            raise HavocError(
                "Set HAVOC365_LIFEBOAT_LEGACY_ACCESS to exactly '0' "
                "(personal invitations only) or '1' (allow the shared member token)."
            )
        if (
            not isinstance(member_token, str)
            or not isinstance(admin_token, str)
            or not 32 <= len(member_token) <= 4096
            or not 32 <= len(admin_token) <= 4096
            or member_token == admin_token
        ):
            raise HavocError(
                "Set distinct member and administrator tokens of 32-4096 characters; "
                "generate random tokens and share only the member token."
            )
        self.path = path
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            init_audit(db)
            db.execute(
                "CREATE TABLE IF NOT EXISTS access_people "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
                "role TEXT NOT NULL CHECK(role IN ('member', 'admin')), "
                "created TEXT NOT NULL, revoked TEXT, recovery INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS access_recovery "
                "ON access_people(recovery) WHERE recovery=1"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS access_invites "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, digest TEXT NOT NULL UNIQUE, "
                "name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('member', 'admin')), "
                "created TEXT NOT NULL, expires INTEGER NOT NULL, used TEXT, revoked TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS access_sessions "
                "(digest TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires INTEGER NOT NULL, "
                "person_id INTEGER REFERENCES access_people(id), "
                "source TEXT NOT NULL CHECK(source IN ('enrolled', 'member', 'admin')), "
                "created INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS access_roots "
                "(kind TEXT PRIMARY KEY, digest TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS access_policy "
                "(id INTEGER PRIMARY KEY CHECK(id=1), "
                "legacy_access INTEGER NOT NULL CHECK(legacy_access IN (0, 1)))"
            )
            previous_policy = db.execute(
                "SELECT legacy_access FROM access_policy WHERE id=1"
            ).fetchone()
            db.execute(
                "INSERT INTO access_policy(id, legacy_access) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET legacy_access=excluded.legacy_access",
                (int(legacy_access),),
            )
            if previous_policy is not None and previous_policy["legacy_access"] != int(
                legacy_access
            ):
                record(db, "system", "access.legacy_access_changed", legacy_access)
            db.execute(
                "INSERT INTO access_people(name, role, created, recovery) "
                "SELECT ?, 'admin', ?, 1 "
                "WHERE NOT EXISTS (SELECT 1 FROM access_people WHERE recovery=1)",
                (_RECOVERY_NAME, _stamp()),
            )
            for kind, token in (("member", member_token), ("admin", admin_token)):
                digest = _hash(token)
                previous = db.execute(
                    "SELECT digest FROM access_roots WHERE kind=?", (kind,)
                ).fetchone()
                if previous is not None and not hmac.compare_digest(previous["digest"], digest):
                    db.execute("DELETE FROM access_sessions WHERE source=?", (kind,))
                    record(db, "system", "access.root_rotated", kind)
                db.execute(
                    "INSERT INTO access_roots(kind, digest) VALUES (?, ?) "
                    "ON CONFLICT(kind) DO UPDATE SET digest=excluded.digest",
                    (kind, digest),
                )
            self._prune_sessions(db)

    @contextmanager
    def database(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _credential(value: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise HavocError("Supply a valid Lifeboat credential or sign in again.")
        return _hash(value)

    @staticmethod
    def _person(row: sqlite3.Row, *, legacy: bool = False) -> Principal:
        return Principal(row["id"], row["name"], row["role"], legacy)

    def _bearer(self, db: sqlite3.Connection, digest: str) -> Principal | None:
        roots = db.execute(
            "SELECT kind, digest FROM access_roots WHERE kind='admin' "
            "OR (SELECT legacy_access FROM access_policy WHERE id=1)=1"
        ).fetchall()
        for root in roots:
            if hmac.compare_digest(root["digest"], digest):
                if root["kind"] == "member":
                    return Principal(None, "Legacy member", "member", True)
                recovery = db.execute("SELECT * FROM access_people WHERE recovery=1").fetchone()
                return self._person(recovery, legacy=True)
        return None

    @staticmethod
    def _prune_sessions(db: sqlite3.Connection) -> None:
        db.execute(
            "DELETE FROM access_sessions WHERE expires<=? OR "
            "(source='member' AND (SELECT legacy_access FROM access_policy WHERE id=1)=0)",
            (int(time.time()),),
        )

    def resolve_bearer(self, token: str) -> Principal | None:
        if token == "":
            return None
        digest = self._credential(token)
        with self.database() as db:
            return self._bearer(db, digest)

    def login(self, credential: str) -> Session:
        digest = self._credential(credential)
        with self.database() as db:
            # Serialize redemption with revocation, capacity checks and session creation.
            db.execute("BEGIN IMMEDIATE")
            self._prune_sessions(db)
            user = self._bearer(db, digest)
            if user is not None:
                source = user.role
                if source == "admin":
                    user = Principal(user.id, user.name, user.role)
            else:
                invite = db.execute(
                    "SELECT * FROM access_invites WHERE digest=?", (digest,)
                ).fetchone()
                if invite is None:
                    raise HavocError(
                        "Credential not recognized. Ask an administrator for a new invitation."
                    )
                if invite["revoked"] is not None:
                    raise HavocError(
                        "Invitation revoked. Ask an administrator for a new invitation."
                    )
                if invite["used"] is not None:
                    raise HavocError(
                        "Invitation already used. Use your existing session or ask an "
                        "administrator for a new invitation."
                    )
                if invite["expires"] <= int(time.time()):
                    raise HavocError(
                        "Invitation expired. Ask an administrator for a new invitation."
                    )
                if (
                    db.execute("SELECT COUNT(*) FROM access_people").fetchone()[0]
                    >= _DIRECTORY_LIMIT
                ):
                    raise HavocError(
                        "The directory is full (1000 people). Ask the recovery administrator "
                        "to review the Lifeboat directory."
                    )
                created = _stamp()
                cursor = db.execute(
                    "INSERT INTO access_people(name, role, created) VALUES (?, ?, ?)",
                    (invite["name"], invite["role"], created),
                )
                user = Principal(cursor.lastrowid, invite["name"], invite["role"])
                db.execute("UPDATE access_invites SET used=? WHERE id=?", (created, invite["id"]))
                record(db, str(user.id), "access.invite_redeemed", str(invite["id"]))
                source = "enrolled"
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            now = int(time.time())
            session = Session(token, csrf, now + _SESSION_SECONDS, user)
            db.execute(
                "INSERT INTO access_sessions(digest, csrf, expires, person_id, source, created) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_hash(token), csrf, session.expires, user.id, source, now),
            )
            db.execute(
                "DELETE FROM access_sessions WHERE digest NOT IN "
                "(SELECT digest FROM access_sessions ORDER BY rowid DESC LIMIT ?)",
                (_SESSION_LIMIT,),
            )
            record(db, str(user.id) if user.id is not None else "legacy-member", "access.login")
            return session

    def session(self, token: str) -> Session | None:
        if token == "":
            return None
        digest = self._credential(token)
        with self.database() as db:
            self._prune_sessions(db)
            row = db.execute(
                "SELECT s.csrf, s.expires, s.source, p.id, p.name, p.role, p.revoked "
                "FROM access_sessions s LEFT JOIN access_people p ON p.id=s.person_id "
                "WHERE s.digest=?",
                (digest,),
            ).fetchone()
            if row is None:
                return None
            if row["source"] == "member":
                user = Principal(None, "Legacy member", "member", True)
            elif row["id"] is None or row["revoked"] is not None:
                return None
            else:
                user = self._person(row)
            return Session(token, row["csrf"], row["expires"], user)

    def logout(self, token: str) -> None:
        digest = self._credential(token)
        with self.database() as db:
            db.execute("DELETE FROM access_sessions WHERE digest=?", (digest,))

    @staticmethod
    def _admin(db: sqlite3.Connection, actor: Principal) -> None:
        if (
            not isinstance(actor, Principal)
            or actor.role != "admin"
            or type(actor.id) is not int
            or not 1 <= actor.id <= 2**63 - 1
        ):
            raise HavocError("Administrator access is required. Sign in as an administrator.")
        row = db.execute(
            "SELECT role, revoked FROM access_people WHERE id=?", (actor.id,)
        ).fetchone()
        if row is None or row["role"] != "admin" or row["revoked"] is not None:
            raise HavocError(
                "Administrator access is revoked or unavailable. Sign in as another administrator."
            )

    @staticmethod
    def _identifier(value: int) -> None:
        if type(value) is not int or not 1 <= value <= 2**63 - 1:
            raise HavocError("Supply a positive 64-bit integer directory or invitation ID.")

    def invite(self, actor: Principal, name: str, role: str, hours: int) -> JsonObject:
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            self._admin(db, actor)
            if (
                not isinstance(name, str)
                or not 1 <= len(name.strip()) <= 80
                or any(
                    ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                    for char in name
                )
            ):
                raise HavocError(
                    "Supply a display name of 1-80 characters without control characters."
                )
            if role not in ("member", "admin"):
                raise HavocError("Choose the member or admin invitation role.")
            if type(hours) is not int or not 1 <= hours <= 720:
                raise HavocError("Choose an invitation lifetime of 1-720 whole hours.")
            count = db.execute("SELECT COUNT(*) FROM access_invites").fetchone()[0]
            if count >= _DIRECTORY_LIMIT:
                # Keep history until space is needed; never evict a usable invitation.
                db.execute(
                    "DELETE FROM access_invites WHERE id IN "
                    "(SELECT id FROM access_invites WHERE used IS NOT NULL "
                    "OR revoked IS NOT NULL OR expires<=? ORDER BY id LIMIT ?)",
                    (int(time.time()), count - _DIRECTORY_LIMIT + 1),
                )
                if (
                    db.execute("SELECT COUNT(*) FROM access_invites").fetchone()[0]
                    >= _DIRECTORY_LIMIT
                ):
                    raise HavocError(
                        "There are 1000 pending invitations. "
                        "Revoke an invitation before creating more."
                    )
            credential = secrets.token_urlsafe(32)
            expires = int(time.time()) + hours * 3600
            cursor = db.execute(
                "INSERT INTO access_invites(digest, name, role, created, expires) "
                "VALUES (?, ?, ?, ?, ?)",
                (_hash(credential), name.strip(), role, _stamp(), expires),
            )
            record(db, str(actor.id), "access.invite_created", str(cursor.lastrowid))
            return {
                "id": cursor.lastrowid,
                "credential": credential,
                "expires": expires,
                "name": name.strip(),
                "role": role,
            }

    def directory(self, actor: Principal) -> JsonObject:
        with self.database() as db:
            self._admin(db, actor)
            self._prune_sessions(db)
            people = db.execute(
                "SELECT id, name, role, created, revoked FROM access_people ORDER BY id"
            ).fetchall()
            invites = db.execute(
                "SELECT id, name, role, created, expires, used, revoked "
                "FROM access_invites ORDER BY id DESC"
            ).fetchall()
            return {
                "people": [dict(row) for row in people],
                "invites": [dict(row) for row in invites],
            }

    def revoke_person(self, actor: Principal, id: int) -> None:
        self._identifier(id)
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            self._admin(db, actor)
            person = db.execute("SELECT recovery FROM access_people WHERE id=?", (id,)).fetchone()
            if person is None:
                raise HavocError(
                    "Person not found. Refresh the directory and choose an existing ID."
                )
            if id == actor.id or person["recovery"]:
                raise HavocError(
                    "You cannot revoke yourself or the recovery administrator. "
                    "Ask another administrator to revoke a non-recovery account."
                )
            db.execute(
                "UPDATE access_people SET revoked=COALESCE(revoked, ?) WHERE id=?", (_stamp(), id)
            )
            db.execute("DELETE FROM access_sessions WHERE person_id=?", (id,))
            record(db, str(actor.id), "access.person_revoked", str(id))

    def revoke_invite(self, actor: Principal, id: int) -> None:
        self._identifier(id)
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            self._admin(db, actor)
            invite = db.execute("SELECT id FROM access_invites WHERE id=?", (id,)).fetchone()
            if invite is None:
                raise HavocError(
                    "Invitation not found. Refresh the directory and choose an existing ID."
                )
            db.execute(
                "UPDATE access_invites SET revoked=COALESCE(revoked, ?) WHERE id=?", (_stamp(), id)
            )
            record(db, str(actor.id), "access.invite_revoked", str(id))
