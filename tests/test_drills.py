import multiprocessing
import os
import signal

import pytest

from havoc365 import cli
from havoc365.drills import (
    POLICIES,
    Pilot,
    duration_seconds,
    plan,
    policy_body,
    report,
    revert,
    run,
)
from havoc365.errors import GraphError, HavocError
from havoc365.storage import read_json, write_json

OPERATOR = "00000000-0000-0000-0000-000000000000"


class PilotGraph:
    """Offline state machine seeded with published examples, never a tenant capture."""

    def __init__(self, fixture):
        self.fixture = fixture
        self.members = fixture("group-list-transitivemembers")["value"]
        self.groups = fixture("group-list")["value"][:1]
        self.roles = fixture("rbacapplication-list-roleassignments")["value"]
        self.eligible = fixture("rbacapplication-list-roleeligibilityscheduleinstances")["value"]
        self.policies = []
        self.calls = []
        self.creation_error = None
        self.deletion_error = None
        self.revocation_error = None

    def collection(self, path, *, params=None):
        self.calls.append(("GET", path, params))
        if path == "groups":
            if params and params.get("$filter") == "isAssignableToRole eq true":
                return iter([])
            return iter(self.groups)
        if path.endswith("/transitiveMembers"):
            return iter(self.members)
        if path.endswith("/roleAssignments"):
            return iter(self.roles)
        if path.endswith("/roleEligibilityScheduleInstances"):
            return iter(self.eligible)
        if path == POLICIES:
            return iter(list(self.policies))
        if path == "auditLogs/signIns":
            return iter(self.fixture("signin-list")["value"])
        raise AssertionError(path)

    def request(self, method, path, *, body=None, retry_safe=True):
        self.calls.append((method, path, body))
        if method == "POST" and path == POLICIES:
            response = self.fixture("conditionalaccessroot-post-policies")
            # Model the API applying the actual request; do not pretend this is a capture.
            response.update(body)
            self.policies.append(response)
            if self.creation_error:
                raise self.creation_error
            return response
        if method == "POST" and path.endswith("/revokeSignInSessions"):
            if self.revocation_error:
                raise self.revocation_error
            return self.fixture("user-revokesigninsessions")
        if method == "DELETE":
            if self.deletion_error:
                raise self.deletion_error
            self.policies = [item for item in self.policies if item["id"] != path.rsplit("/", 1)[1]]
            return {}
        raise AssertionError((method, path))


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def pilot(graph):
    return plan(graph, "Golf Assist", "2s", OPERATOR)


def test_plan_is_read_only_and_shows_people(fixture):
    graph = PilotGraph(fixture)
    result = pilot(graph)
    assert result.seconds == 2
    assert "group1@contoso.com" in result.describe()
    assert all(call[0] == "GET" for call in graph.calls)


@pytest.mark.parametrize("duration", ["0m", "-1m", "30", "tomorrow", "1.5h"])
def test_invalid_duration_is_refused(duration):
    with pytest.raises(HavocError, match="positive duration"):
        duration_seconds(duration)


def test_refuses_large_and_empty_pilot(fixture):
    graph = PilotGraph(fixture)
    for members in ([], graph.members * 26):
        graph.members = members
        with pytest.raises(HavocError, match="between 1 and 25"):
            pilot(graph)
    assert not any(call[0] == "POST" for call in graph.calls)


@pytest.mark.parametrize("eligible", [False, True])
def test_refuses_active_or_eligible_admin(fixture, eligible):
    graph = PilotGraph(fixture)
    # Use a principal ID from a published role example as the pilot member.
    graph.members[0]["id"] = (graph.eligible if eligible else graph.roles)[0]["principalId"]
    with pytest.raises(HavocError, match="active or eligible admin"):
        pilot(graph)


def test_refuses_operator_and_non_user_members(fixture):
    graph = PilotGraph(fixture)
    with pytest.raises(HavocError, match="signed-in operator"):
        plan(graph, "Golf Assist", "30m", graph.members[0]["id"])
    graph.members[0] = fixture("group-list")["value"][0]
    with pytest.raises(HavocError, match="user members only"):
        pilot(graph)


def test_refuses_group_inherited_admin_role(fixture):
    graph = PilotGraph(fixture)
    original = graph.collection

    def collection(path, *, params=None):
        if path == "groups" and params == {"$filter": "isAssignableToRole eq true"}:
            return iter([{"id": graph.roles[0]["principalId"]}])
        return original(path, params=params)

    graph.collection = collection
    with pytest.raises(HavocError, match="role-assignable group"):
        pilot(graph)


def test_incomplete_role_checks_fail_closed(fixture):
    graph = PilotGraph(fixture)
    original = graph.collection

    def collection(path, *, params=None):
        if path.endswith("/roleEligibilityScheduleInstances"):
            raise GraphError(403, "Forbidden", "Cannot read role eligibility")
        return original(path, params=params)

    graph.collection = collection
    with pytest.raises(GraphError, match="role eligibility"):
        pilot(graph)
    assert not any(call[0] == "POST" for call in graph.calls)


def test_timer_cleans_up_and_records_history(fixture, tmp_path):
    graph = PilotGraph(fixture)
    selected = pilot(graph)
    clock = Clock()
    run(graph, selected, tmp_path, "test-tenant", sleep=clock.sleep, monotonic=clock.monotonic)
    assert not graph.policies
    creation = next(call for call in graph.calls if call[:2] == ("POST", POLICIES))
    assert creation[2]["conditions"]["applications"]["includeApplications"] == ["Office365"]
    assert creation[2]["conditions"]["users"] == {"includeUsers": list(selected.members)}
    assert [call[1] for call in graph.calls if call[0] == "POST"] == [
        POLICIES,
        f"users/{selected.members[0]}/revokeSignInSessions",
    ]
    record = read_json(next((tmp_path / "drills").glob("*.json")))
    assert record["status"] == "ended"
    assert "policy_id" in record
    assert clock.now == 2


def test_ctrl_c_still_removes_policy(fixture, tmp_path):
    graph = PilotGraph(fixture)

    def interrupt(_):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run(graph, pilot(graph), tmp_path, "test-tenant", sleep=interrupt)
    assert not graph.policies


def test_failed_revocation_aborts_and_cleans_up(fixture, tmp_path):
    graph = PilotGraph(fixture)
    graph.revocation_error = GraphError(403, "Forbidden", "Cannot revoke sessions")
    with pytest.raises(GraphError, match="Cannot revoke"):
        run(graph, pilot(graph), tmp_path, "test-tenant")
    assert not graph.policies


def test_lost_creation_response_recovers_policy_by_name(fixture, tmp_path):
    graph = PilotGraph(fixture)
    graph.creation_error = HavocError("Injected lost response")
    with pytest.raises(HavocError, match="lost response"):
        run(graph, pilot(graph), tmp_path, "test-tenant")
    assert not graph.policies
    assert any(call[:2] == ("GET", POLICIES) for call in graph.calls)


def test_unresolved_creation_never_claims_success(fixture, tmp_path, capsys):
    graph = PilotGraph(fixture)
    original = graph.request

    def request(method, path, **kwargs):
        if method == "POST" and path == POLICIES:
            raise HavocError("Injected timeout before a response")
        return original(method, path, **kwargs)

    graph.request = request
    with pytest.raises(HavocError, match="may still exist"):
        run(graph, pilot(graph), tmp_path, "test-tenant", sleep=lambda _: None)
    assert read_json(next((tmp_path / "drills").glob("*.json")))["status"] == "uncertain"
    assert "removed" not in capsys.readouterr().out


def test_cleanup_failure_identifies_the_exact_remaining_policy(fixture, tmp_path, capsys):
    graph = PilotGraph(fixture)
    graph.revocation_error = HavocError("Injected revocation outage")
    graph.deletion_error = GraphError(503, "ServiceUnavailable", "Injected cleanup outage")
    with pytest.raises(HavocError, match="may still block") as error:
        run(graph, pilot(graph), tmp_path, "test-tenant")
    assert graph.policies[0]["id"] in str(error.value)
    assert graph.policies[0]["displayName"] in str(error.value)
    assert "havoc365 drill revert" in str(error.value)
    assert read_json(next((tmp_path / "drills").glob("*.json")))["status"] == "running"
    assert "removed" not in capsys.readouterr().out


def test_revert_works_without_local_state_and_preserves_unrelated_policies(fixture):
    graph = PilotGraph(fixture)
    graph.policies = fixture("conditionalaccessroot-list-policies")["value"]
    original = list(graph.policies)
    owned = fixture("conditionalaccessroot-post-policies")
    owned.update(policy_body("havoc365-drill-" + "a" * 32, (graph.members[0]["id"],)))
    graph.policies.append(owned)
    assert revert(graph) == [owned["displayName"]]
    assert graph.policies == original


def test_modified_drill_is_not_silently_ignored(fixture):
    graph = PilotGraph(fixture)
    policy = fixture("conditionalaccessroot-post-policies")
    policy["displayName"] = "havoc365-drill-" + "a" * 32
    graph.policies.append(policy)
    with pytest.raises(HavocError, match="no longer matches a drill"):
        revert(graph)
    assert len(graph.policies) == 1


def test_revert_refuses_all_users_even_with_a_drill_name(fixture):
    graph = PilotGraph(fixture)
    policy = fixture("conditionalaccessroot-post-policies")
    policy.update(policy_body("havoc365-drill-" + "a" * 32, ("All",)))
    graph.policies.append(policy)
    with pytest.raises(HavocError, match="no longer matches a drill"):
        revert(graph)
    assert len(graph.policies) == 1


def test_denied_creation_is_recorded_as_not_started(fixture, tmp_path, capsys):
    graph = PilotGraph(fixture)
    original = graph.request

    def request(method, path, **kwargs):
        if method == "POST" and path == POLICIES:
            raise GraphError(403, "Forbidden", "Cannot create policy")
        return original(method, path, **kwargs)

    graph.request = request
    with pytest.raises(GraphError, match="Cannot create"):
        run(graph, pilot(graph), tmp_path, "test-tenant")
    assert read_json(next((tmp_path / "drills").glob("*.json")))["status"] == "not-started"
    assert "removed" not in capsys.readouterr().out


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="POSIX kill test")
def test_sigkill_leaves_durable_recovery_record(fixture, tmp_path):
    remote = tmp_path / "remote-policy.json"

    def child():
        graph = PilotGraph(fixture)
        original = graph.request

        def request(method, path, **kwargs):
            response = original(method, path, **kwargs)
            if method == "POST" and path == POLICIES:
                write_json(remote, response)
                os.kill(os.getpid(), signal.SIGKILL)
            return response

        graph.request = request
        run(graph, pilot(graph), tmp_path, "test-tenant")

    worker = multiprocessing.get_context("fork").Process(target=child)
    worker.start()
    worker.join(timeout=10)
    if worker.is_alive():
        worker.terminate()
        worker.join(timeout=5)
        pytest.fail("Offline kill-test worker did not exit")
    assert worker.exitcode == -signal.SIGKILL
    journal = read_json(next((tmp_path / "drills").glob("*.json")))
    assert journal["status"] == "creating"
    graph = PilotGraph(fixture)
    graph.policies = [read_json(remote)]
    assert revert(graph) == [journal["name"]]
    assert not graph.policies


def test_report_filters_to_pilot_and_uses_recorded_window(fixture, tmp_path):
    graph = PilotGraph(fixture)
    event = fixture("signin-list")["value"][0]
    write_json(
        tmp_path / "drills" / "history.json",
        {
            "tenant_id": "test-tenant",
            "name": "example-drill",
            "members": [event["userId"]],
            "started": "2023-12-01T16:00:00+00:00",
            "ends": "2023-12-01T16:30:00+00:00",
            "status": "ended",
        },
    )
    output = report(graph, tmp_path, "test-tenant")
    assert "Graph explorer" in output
    assert "1 sign-in attempts" in output
    assert "2023-12-01T16:00:00+00:00" in graph.calls[-1][2]["$filter"]
    with pytest.raises(HavocError, match="No drill history"):
        report(graph, tmp_path, "another-tenant")


def test_cli_refuses_run_without_exact_confirmation(monkeypatch, fixture, tmp_path, capsys):
    graph = PilotGraph(fixture)
    graph.close = lambda: None
    fake_pilot = Pilot("group-id", "Golf Assist", (), (), 1800)

    class SignedIn:
        def __init__(self, directory):
            self.profile = {"oid": OPERATOR, "tid": "test-tenant"}

        def token(self, drill):
            return "offline-token"

    monkeypatch.setattr(cli, "Auth", SignedIn)
    monkeypatch.setattr(cli, "Graph", lambda _: graph)
    monkeypatch.setattr(cli, "plan", lambda *args: fake_pilot)
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert cli.main(["drill", "run", "--group", "Golf Assist"]) == 1
    assert "No drill was started" in capsys.readouterr().err
    assert not graph.policies
