import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from havoc365.errors import GraphError, HavocError
from havoc365.graph import Graph
from havoc365.storage import JsonObject, read_json, write_json

POLICIES = "identity/conditionalAccess/policies"
POLICY_NAME = re.compile(r"havoc365-drill-[0-9a-f]{32}\Z")


def duration_seconds(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([smh])", value)
    if not match:
        raise HavocError("Invalid duration. Use a positive duration such as 30m, 1h or 15s.")
    return int(match[1]) * {"s": 1, "m": 60, "h": 3600}[match[2]]


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise HavocError(
            "Graph returned an invalid directory object ID. No drill was started."
        ) from error


@dataclass(frozen=True)
class Pilot:
    group_id: str
    name: str
    members: tuple[str, ...]
    labels: tuple[str, ...]
    seconds: int

    def describe(self) -> str:
        return "\n".join(
            [
                f"Pilot: {self.name} ({self.group_id})",
                f"Duration: {self.seconds} seconds",
                f"Microsoft 365 access will be blocked for {len(self.members)} people:",
                *(
                    f"  {label} ({member})"
                    for label, member in zip(self.labels, self.members, strict=True)
                ),
                "Existing access tokens may keep working until they expire.",
            ]
        )


def plan(graph: Graph, group: str, duration: str, operator_id: str) -> Pilot:
    seconds = duration_seconds(duration)
    groups = list(
        graph.collection(
            "groups",
            params={"$filter": "displayName eq '" + group.replace("'", "''") + "'"},
        )
    )
    if len(groups) != 1:
        raise HavocError(
            f"Expected one group named {group!r}; found {len(groups)}. Use a unique display name."
        )
    pilot_group = groups[0]
    group_id = _uuid(pilot_group["id"])
    if (
        "DynamicMembership" in pilot_group.get("groupTypes", [])
        or pilot_group.get("membershipRule")
        or pilot_group.get("visibility") == "HiddenMembership"
        or pilot_group.get("isAssignableToRole")
    ):
        raise HavocError("Use a static, visible pilot group that cannot be assigned admin roles.")
    members = list(graph.collection(f"groups/{group_id}/transitiveMembers"))
    if not 1 <= len(members) <= 25:
        raise HavocError(f"The pilot has {len(members)} members. Use between 1 and 25 volunteers.")
    if any(member.get("@odata.type") != "#microsoft.graph.user" for member in members):
        raise HavocError(
            "Use a pilot with user members only; nested groups and devices are refused."
        )
    ids = tuple(_uuid(member["id"]) for member in members)
    if operator_id in ids:
        raise HavocError(
            "The signed-in operator is in the pilot. Use an account outside that group."
        )
    assignments = list(graph.collection("roleManagement/directory/roleAssignments"))
    eligible = list(graph.collection("roleManagement/directory/roleEligibilityScheduleInstances"))
    privileged = {item["principalId"] for item in assignments + eligible}
    if privileged.intersection(ids):
        raise HavocError("The pilot includes someone with an active or eligible admin role.")
    role_groups = graph.collection("groups", params={"$filter": "isAssignableToRole eq true"})
    for role_group in role_groups:
        if role_group["id"] in privileged:
            role_members = graph.collection(f"groups/{_uuid(role_group['id'])}/transitiveMembers")
            if any(member.get("id") in ids for member in role_members):
                raise HavocError("The pilot includes an admin through a role-assignable group.")
    return Pilot(
        group_id,
        pilot_group["displayName"],
        ids,
        tuple(
            member.get("displayName") or member.get("mail") or member["id"] for member in members
        ),
        seconds,
    )


def policy_body(name: str, members: tuple[str, ...]) -> JsonObject:
    return {
        "displayName": name,
        "state": "enabled",
        "conditions": {
            "clientAppTypes": ["all"],
            "applications": {"includeApplications": ["Office365"]},
            # Freeze the validated roster: later group joins must not expand the drill.
            "users": {"includeUsers": list(members)},
        },
        "grantControls": {"operator": "OR", "builtInControls": ["block"]},
    }


def is_drill(policy: JsonObject) -> bool:
    if not POLICY_NAME.fullmatch(str(policy.get("displayName", ""))):
        return False
    conditions = policy.get("conditions", {})
    if not isinstance(conditions, dict):
        return False
    users = conditions.get("users", {})
    applications = conditions.get("applications", {})
    controls = policy.get("grantControls", {})
    if not all(isinstance(value, dict) for value in (users, applications, controls)):
        return False
    member_ids = users.get("includeUsers", [])
    if not isinstance(member_ids, list):
        return False
    try:
        for member in member_ids:
            UUID(member)
    except (ValueError, TypeError, AttributeError):
        return False
    return (
        1 <= len(member_ids) <= 25
        and not users.get("includeGroups")
        and not users.get("includeRoles")
        and conditions.get("clientAppTypes") == ["all"]
        and applications.get("includeApplications") == ["Office365"]
        and controls.get("builtInControls") == ["block"]
        and controls.get("operator") == "OR"
    )


def _remove(graph: Graph, policy_id: str, name: str) -> None:
    try:
        graph.request("DELETE", f"{POLICIES}/{_uuid(policy_id)}")
    except HavocError as error:
        raise HavocError(
            f"Could not remove policy {name} (ID {policy_id}) after retries. "
            "It may still block the pilot. Run havoc365 drill revert, or delete this exact policy "
            "in Entra > Protection > Conditional Access > Policies."
        ) from error


def revert(graph: Graph) -> list[str]:
    removed = []
    for policy in graph.collection(POLICIES):
        if POLICY_NAME.fullmatch(str(policy.get("displayName", ""))) and not is_drill(policy):
            raise HavocError(
                f"Policy {policy['displayName']} (ID {policy['id']}) no longer matches a drill. "
                "Inspect it in Entra Conditional Access; it was not removed."
            )
        if is_drill(policy):
            _remove(graph, policy["id"], policy["displayName"])
            removed.append(policy["displayName"])
    return removed


def run(
    graph: Graph,
    pilot: Pilot,
    directory: Path,
    tenant_id: str,
    *,
    announce: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    name = f"havoc365-drill-{uuid4().hex}"
    started = datetime.now(UTC)
    record: JsonObject = {
        "name": name,
        "tenant_id": tenant_id,
        "group_id": pilot.group_id,
        "members": list(pilot.members),
        "started": started.isoformat(),
        "ends": (started + timedelta(seconds=pilot.seconds)).isoformat(),
        "status": "creating",
    }
    path = directory / "drills" / f"{name}.json"
    write_json(path, record)
    policy_id: str | None = None
    uncertain = True
    deadline = monotonic() + pilot.seconds
    try:
        response = graph.request(
            "POST", POLICIES, body=policy_body(name, pilot.members), retry_safe=False
        )
        policy_id = _uuid(response.get("id"))
        uncertain = False
        record.update(policy_id=policy_id, status="running")
        write_json(path, record)
        announce(f"Policy {name} (ID {policy_id}) created. Recovery: havoc365 drill revert")
        for member in pilot.members:
            if monotonic() >= deadline:
                break
            result = graph.request("POST", f"users/{member}/revokeSignInSessions")
            if result.get("value") is not True:
                raise HavocError(f"Session revocation failed for {member}. Ending the drill.")
        while (remaining := deadline - monotonic()) > 0:
            sleep(min(1.0, remaining))
    except GraphError as error:
        if 400 <= error.status < 500 and error.status != 429:
            uncertain = False
        raise
    finally:
        # A timeout can hide a successful creation, so recover by the pre-recorded name.
        removed = False
        if policy_id is not None:
            _remove(graph, policy_id, name)
            removed = True
        elif uncertain:
            try:
                matches = []
                for attempt in range(3):
                    matches = [
                        item
                        for item in graph.collection(POLICIES)
                        if item.get("displayName") == name
                    ]
                    if matches:
                        break
                    sleep(2**attempt)
                for policy in matches:
                    _remove(graph, policy["id"], name)
                    removed = True
                if not matches:
                    record["status"] = "uncertain"
                    write_json(path, record)
                    raise HavocError(
                        "No matching policy is visible yet; creation may still complete."
                    )
            except HavocError as error:
                raise HavocError(
                    f"Policy creation had an uncertain result. Policy {name} may still exist. "
                    "Run havoc365 drill revert or find this name in Entra Conditional Access."
                ) from error
        record.update(
            status="ended" if removed else "not-started", ended=datetime.now(UTC).isoformat()
        )
        write_json(path, record)
        if removed:
            announce(f"Drill ended. Policy {name} removed.")


def report(graph: Graph, directory: Path, tenant_id: str) -> str:
    paths = sorted((directory / "drills").glob("*.json"), key=lambda path: path.stat().st_mtime)
    records = [read_json(path) for path in paths]
    records = [record for record in records if record.get("tenant_id") == tenant_id]
    if not records:
        raise HavocError(
            "No drill history for this tenant on this machine. "
            "Run the report where the drill started."
        )
    record = records[-1]
    end = min(record.get("ended", record["ends"]), datetime.now(UTC).isoformat())
    events = graph.collection(
        "auditLogs/signIns",
        params={"$filter": f"createdDateTime ge {record['started']} and createdDateTime le {end}"},
    )
    lines = [f"Drill: {record['name']}", f"State recorded locally: {record['status']}"]
    count = 0
    for event in events:
        if event.get("userId") in record["members"]:
            status = event.get("status", {})
            lines.append(
                f"  {event['createdDateTime']}  {event.get('userPrincipalName', event['userId'])}  "
                f"{event.get('appDisplayName', '(unknown app)')}  "
                f"error={status.get('errorCode', 'unknown')} "
                f"{status.get('failureReason') or ''}".rstrip()
            )
            count += 1
    lines.append(f"{count} sign-in attempts. Graph logs can arrive late; rerun this report later.")
    return "\n".join(lines)
