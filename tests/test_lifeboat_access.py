import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime
from threading import Barrier

import pytest

from havoc365 import lifeboat_access
from havoc365.errors import HavocError
from havoc365.lifeboat_access import Access, Principal, Session, init_audit, record

MEMBER = "member-" + "m" * 40
ADMIN = "admin-" + "a" * 40


@pytest.fixture
def access(tmp_path, monkeypatch):
    monkeypatch.delenv("HAVOC365_LIFEBOAT_LEGACY_ACCESS", raising=False)
    return Access(tmp_path / "lifeboat.sqlite3", MEMBER, ADMIN)


@pytest.fixture
def admin(access):
    return access.login(ADMIN).user


def enroll(access, admin, name="Incident lead", role="member"):
    invitation = access.invite(admin, name, role, 24)
    return access.login(invitation["credential"])


def test_one_use_invitation_fixes_identity_and_survives_restart(access, admin):
    invitation = access.invite(admin, "  Incident lead  ", "admin", 24)
    session = access.login(invitation["credential"])
    assert session.user.name == "Incident lead"
    assert session.user.role == "admin"
    assert not session.user.legacy
    assert session.user.id != admin.id
    assert len(session.token) >= 43
    assert len(session.csrf) >= 43
    assert session.token != session.csrf
    assert session.public() == {
        "user": {
            "id": session.user.id,
            "name": "Incident lead",
            "role": "admin",
            "legacy": False,
        },
        "csrf": session.csrf,
        "expires": session.expires,
    }
    with pytest.raises(FrozenInstanceError):
        session.user.name = "Impersonator"
    with pytest.raises(HavocError, match="already used"):
        access.login(invitation["credential"])
    with pytest.raises(HavocError, match="not recognized"):
        access.login(session.token)
    restarted = Access(access.path, MEMBER, ADMIN)
    assert restarted.session(session.token) == session
    directory = restarted.directory(admin)
    assert directory["invites"][0]["used"] is not None
    assert directory["people"][1]["name"] == "Incident lead"
    assert set(directory["people"][1]) == {"id", "name", "role", "created", "revoked"}
    assert set(directory["invites"][0]) == {
        "id",
        "name",
        "role",
        "created",
        "expires",
        "used",
        "revoked",
    }


def test_concurrent_redemption_has_exactly_one_winner(access, admin):
    invitation = access.invite(admin, "Responder", "member", 1)
    barrier = Barrier(2)

    def redeem():
        barrier.wait(timeout=5)
        try:
            return access.login(invitation["credential"])
        except HavocError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: redeem(), range(2)))
    assert sum(isinstance(result, Session) for result in results) == 1
    failures = [result for result in results if isinstance(result, HavocError)]
    assert len(failures) == 1
    assert "already used" in str(failures[0])
    assert len(access.directory(admin)["people"]) == 2


def test_invitation_expiry_boundary_and_revocation_are_actionable(access, admin, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(lifeboat_access.time, "time", lambda: now)
    expired = access.invite(admin, "Responder", "member", 1)
    revoked = access.invite(admin, "Other responder", "member", 2)
    access.revoke_invite(admin, revoked["id"])
    now += 3600
    with pytest.raises(HavocError, match=r"expired.*new invitation"):
        access.login(expired["credential"])
    with pytest.raises(HavocError, match=r"revoked.*new invitation"):
        access.login(revoked["credential"])
    directory = access.directory(admin)
    assert len(directory["invites"]) == 2
    assert not directory["invites"][1]["used"]
    assert directory["invites"][0]["revoked"]


def test_sessions_have_absolute_expiry_are_pruned_and_logout_is_durable(access, admin, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(lifeboat_access.time, "time", lambda: now)
    session = enroll(access, admin)
    assert session.expires == now + 30 * 24 * 3600
    now += 29 * 24 * 3600
    assert access.session(session.token) == session
    now = session.expires
    assert access.session(session.token) is None
    with access.database() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM access_sessions WHERE expires<=?", (now,)).fetchone()[
                0
            ]
            == 0
        )
    fresh = access.login(ADMIN)
    access.logout(fresh.token)
    access.logout(fresh.token)
    assert Access(access.path, MEMBER, ADMIN).session(fresh.token) is None
    assert access.session("unknown session") is None


def test_legacy_bearer_and_login_never_enroll_shared_members(access, admin):
    before = access.directory(admin)
    member = access.resolve_bearer(MEMBER)
    assert member == Principal(None, "Legacy member", "member", True)
    assert access.resolve_bearer(ADMIN) == Principal(admin.id, admin.name, "admin", True)
    assert access.resolve_bearer("invalid") is None
    assert access.resolve_bearer("") is None
    assert access.session("") is None
    session = access.login(MEMBER)
    assert session.user == member
    assert access.session(session.token) == session
    assert access.directory(admin)["people"] == before["people"]
    assert access.resolve_bearer(session.token) is None
    with pytest.raises(HavocError, match="Administrator"):
        access.invite(member, "Someone", "admin", 1)


def test_disabling_legacy_access_invalidates_only_shared_member_sessions(
    access, admin, monkeypatch
):
    legacy = access.login(MEMBER)
    recovery = access.login(ADMIN)
    enrolled = enroll(access, admin)
    revoked = enroll(access, admin, "Revoked responder")
    access.revoke_person(admin, revoked.user.id)
    monkeypatch.setenv("HAVOC365_LIFEBOAT_LEGACY_ACCESS", "0")
    personal_only = Access(access.path, MEMBER, ADMIN)
    assert personal_only.resolve_bearer(MEMBER) is None
    assert personal_only.session(legacy.token) is None
    assert personal_only.session(revoked.token) is None
    assert personal_only.session(enrolled.token) == enrolled
    assert personal_only.session(recovery.token) == recovery
    assert not enrolled.user.legacy
    assert not recovery.user.legacy
    assert personal_only.login(ADMIN).user == admin
    assert personal_only.resolve_bearer(ADMIN) == Principal(admin.id, admin.name, "admin", True)
    assert not enroll(personal_only, admin, "New responder").user.legacy
    for service in (access, personal_only):
        assert service.resolve_bearer(MEMBER) is None
        with pytest.raises(HavocError, match=r"administrator.*invitation"):
            service.login(MEMBER)
    with personal_only.database() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM access_sessions WHERE source='member'").fetchone()[0]
            == 0
        )

    monkeypatch.setenv("HAVOC365_LIFEBOAT_LEGACY_ACCESS", "1")
    compatible = Access(access.path, MEMBER, ADMIN)
    assert compatible.login(MEMBER).user == legacy.user
    assert compatible.session(legacy.token) is None
    assert compatible.session(enrolled.token) == enrolled


@pytest.mark.parametrize("value", ["", "false", "true", " 1", "1 ", "01", "2"])
def test_invalid_legacy_access_setting_fails_before_database_creation(tmp_path, monkeypatch, value):
    monkeypatch.setenv("HAVOC365_LIFEBOAT_LEGACY_ACCESS", value)
    path = tmp_path / "invalid.sqlite3"
    with pytest.raises(HavocError, match="HAVOC365_LIFEBOAT_LEGACY_ACCESS"):
        Access(path, MEMBER, ADMIN)
    assert not path.exists()


def test_all_admin_methods_check_role_and_live_identity(access, admin):
    member = enroll(access, admin).user
    invited = access.invite(admin, "Another responder", "member", 1)
    for actor in (member, Principal(member.id, member.name, "admin")):
        for operation in (
            lambda actor=actor: access.directory(actor),
            lambda actor=actor: access.invite(actor, "Impersonator", "admin", 1),
            lambda actor=actor: access.revoke_person(actor, admin.id),
            lambda actor=actor: access.revoke_invite(actor, invited["id"]),
        ):
            with pytest.raises(HavocError, match="Administrator"):
                operation()
    administrator = enroll(access, admin, "Coordinator", "admin")
    other = enroll(access, admin, "Volunteer")
    access.revoke_person(administrator.user, other.user.id)
    assert access.session(other.token) is None
    access.revoke_person(admin, administrator.user.id)
    assert access.session(administrator.token) is None
    with pytest.raises(HavocError, match="revoked"):
        access.directory(administrator.user)
    people = access.directory(admin)["people"]
    assert all(person["revoked"] for person in people[-2:])


def test_recovery_and_self_cannot_be_revoked_and_root_recovers_lost_sessions(access, admin):
    coordinator = enroll(access, admin, "Coordinator", "admin")
    for actor, target in (
        (admin, admin.id),
        (coordinator.user, coordinator.user.id),
        (coordinator.user, admin.id),
    ):
        with pytest.raises(HavocError, match="cannot revoke"):
            access.revoke_person(actor, target)
    with access.database() as db:
        db.execute("DELETE FROM access_sessions")
    restarted = Access(access.path, MEMBER, ADMIN)
    recovery = restarted.login(ADMIN)
    assert recovery.user == admin
    assert len(restarted.directory(recovery.user)["people"]) == 2
    restarted.revoke_person(recovery.user, coordinator.user.id)


def test_tokens_are_hashed_and_audit_and_public_directory_do_not_expose_secrets(access, admin):
    invitation = access.invite(admin, "Responder", "member", 1)
    session = access.login(invitation["credential"])
    legacy = access.login(MEMBER)
    with access.database() as db:
        dump = "\n".join(db.iterdump())
        assert (
            db.execute(
                "SELECT digest FROM access_invites WHERE id=?", (invitation["id"],)
            ).fetchone()[0]
            == hashlib.sha256(invitation["credential"].encode()).hexdigest()
        )
        stored = db.execute(
            "SELECT digest, csrf FROM access_sessions WHERE person_id=?", (session.user.id,)
        ).fetchone()
        assert stored["digest"] == hashlib.sha256(session.token.encode()).hexdigest()
        assert stored["csrf"] == session.csrf
        audits = db.execute("SELECT actor, action, detail, created FROM audit").fetchall()
    directory = str(access.directory(admin))
    for secret in (ADMIN, MEMBER, invitation["credential"], session.token, legacy.token):
        assert secret not in dump
        assert secret not in directory
        assert secret.encode() not in access.path.read_bytes()
    assert {"access.login", "access.invite_created", "access.invite_redeemed"} <= {
        row["action"] for row in audits
    }
    assert all(
        datetime.fromisoformat(row["created"]).utcoffset().total_seconds() == 0 for row in audits
    )


def test_rotation_only_invalidates_corresponding_root_sessions(access, admin):
    member = access.login(MEMBER)
    recovery = access.login(ADMIN)
    enrolled = enroll(access, admin)
    coordinator = enroll(access, admin, "Coordinator", "admin")
    new_member = "replacement-member-" + "x" * 40
    member_rotated = Access(access.path, new_member, ADMIN)
    assert member_rotated.session(member.token) is None
    assert member_rotated.session(recovery.token) == recovery
    assert member_rotated.session(enrolled.token) == enrolled
    assert member_rotated.session(coordinator.token) == coordinator
    assert access.resolve_bearer(MEMBER) is None
    with pytest.raises(HavocError, match="not recognized"):
        access.login(MEMBER)
    replacement_member_session = member_rotated.login(new_member)
    new_admin = "replacement-admin-" + "y" * 40
    admin_rotated = Access(access.path, new_member, new_admin)
    assert admin_rotated.session(recovery.token) is None
    assert admin_rotated.session(replacement_member_session.token) == replacement_member_session
    assert admin_rotated.session(enrolled.token) == enrolled
    assert admin_rotated.session(coordinator.token) == coordinator
    assert admin_rotated.resolve_bearer(ADMIN) is None
    assert admin_rotated.login(new_admin).user == admin
    assert len(admin_rotated.directory(admin)["people"]) == 3


@pytest.mark.parametrize(
    ("name", "role", "hours"),
    [
        ("", "member", 1),
        ("  ", "member", 1),
        ("x" * 81, "member", 1),
        ("Responder\n", "member", 1),
        ("Responder\x7f", "member", 1),
        ("Responder\ud800", "member", 1),
        (None, "member", 1),
        ("Responder", "owner", 1),
        ("Responder", None, 1),
        ("Responder", "member", 0),
        ("Responder", "member", 721),
        ("Responder", "member", True),
        ("Responder", "member", 1.5),
        ("Responder", "member", "24"),
    ],
)
def test_invalid_invitation_data_is_rejected_without_writes(access, admin, name, role, hours):
    with pytest.raises(HavocError):
        access.invite(admin, name, role, hours)
    assert access.directory(admin)["invites"] == []


@pytest.mark.parametrize("id", [0, -1, True, 1.5, "1", None, 9999, 2**100])
def test_invalid_or_missing_revocation_ids_are_not_silent(access, admin, id):
    for operation in (access.revoke_invite, access.revoke_person):
        with pytest.raises(HavocError):
            operation(admin, id)


@pytest.mark.parametrize("credential", ["", None, 123, "x" * 4097, "unknown", "\ud800"])
def test_invalid_login_credentials_are_actionable(access, credential):
    with pytest.raises(HavocError, match=r"credential|Credential|Unicode"):
        access.login(credential)


@pytest.mark.parametrize(
    ("member", "admin"),
    [(MEMBER, MEMBER), ("short", ADMIN), (MEMBER, None), (None, ADMIN)],
)
def test_invalid_roots_fail_before_creating_database(tmp_path, member, admin):
    path = tmp_path / "invalid.sqlite3"
    with pytest.raises(HavocError, match="distinct"):
        Access(path, member, admin)
    assert not path.exists()


def test_invitation_history_is_bounded_without_evicting_usable_credentials(access, admin):
    with access.database() as db:
        db.executemany(
            "INSERT INTO access_invites(digest, name, role, created, expires) "
            "VALUES (?, 'Responder', 'member', '2026-01-01T00:00:00+00:00', ?)",
            [(f"digest-{number}", 9_999_999_999) for number in range(1000)],
        )
    with pytest.raises(HavocError, match="1000 pending"):
        access.invite(admin, "One too many", "member", 1)
    first = min(item["id"] for item in access.directory(admin)["invites"])
    access.revoke_invite(admin, first)
    invitation = access.invite(admin, "New responder", "member", 720)
    directory = access.directory(admin)
    assert len(directory["invites"]) == 1000
    assert first not in {item["id"] for item in directory["invites"]}
    assert access.login(invitation["credential"]).user.name == "New responder"


def test_directory_capacity_does_not_consume_invitation_on_failed_redemption(access, admin):
    invitation = access.invite(admin, "New responder", "member", 1)
    with access.database() as db:
        db.executemany(
            "INSERT INTO access_people(name, role, created) "
            "VALUES (?, 'member', '2026-01-01T00:00:00+00:00')",
            [(f"Responder {number}",) for number in range(999)],
        )
    with pytest.raises(HavocError, match="directory is full"):
        access.login(invitation["credential"])
    directory = access.directory(admin)
    assert len(directory["people"]) == 1000
    assert directory["invites"][0]["used"] is None


def test_sessions_are_bounded_and_current_login_remains_usable(access, monkeypatch):
    monkeypatch.setattr(lifeboat_access, "_SESSION_LIMIT", 3)
    sessions = [access.login(ADMIN) for _ in range(4)]
    assert access.session(sessions[0].token) is None
    assert all(access.session(session.token) == session for session in sessions[1:])


def test_shared_audit_is_transactional_and_keeps_latest_ten_thousand(access):
    with access.database() as db:
        init_audit(db)
        db.executemany(
            "INSERT INTO audit(actor, action, detail, created) VALUES (?, ?, ?, ?)",
            [
                ("person:2", "checkin", str(number), "2026-01-01T00:00:00+00:00")
                for number in range(10002)
            ],
        )
        record(db, "person:3", "incident.updated")
        rows = db.execute("SELECT actor, action, detail, created FROM audit ORDER BY id").fetchall()
        assert len(rows) == 10_000
        assert rows[0]["detail"] == "3"
        assert rows[-1]["actor"] == "person:3"
        assert rows[-1]["action"] == "incident.updated"
        assert rows[-1]["detail"] == ""
    with pytest.raises(RuntimeError), access.database() as db:
        record(db, "person:4", "must.rollback")
        raise RuntimeError("Rollback the whole incident change")
    db = sqlite3.connect(access.path, timeout=5)
    try:
        assert (
            db.execute("SELECT COUNT(*) FROM audit WHERE action='must.rollback'").fetchone()[0] == 0
        )
    finally:
        db.close()
