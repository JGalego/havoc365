from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from havoc365.errors import GraphError, HavocError
from havoc365.graph import Graph


def graph_with(handler, sleeps=None):
    return Graph(
        lambda: "offline-test-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=(sleeps if sleeps is not None else []).append,
    )


def test_paging_uses_documented_next_link(fixture):
    first = fixture("signin-list")
    second = fixture("signin-list-2")
    second.pop("@odata.nextLink")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=first if len(requests) == 1 else second)

    graph = graph_with(handler)
    events = list(graph.collection("auditLogs/signIns", params={"$top": "1"}))
    assert len(events) == 2
    assert str(requests[1].url) == first["@odata.nextLink"]
    assert requests[1].headers["Authorization"] == "Bearer offline-test-token"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retries_documented_throttle_body_and_injected_server_status(status, fixture):
    sleeps = []
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(status, json=fixture("throttled"), headers={"Retry-After": "10"})
        return httpx.Response(200, json=fixture("domain-list"))

    assert (
        list(graph_with(handler, sleeps).collection("domains")) == fixture("domain-list")["value"]
    )
    assert sleeps == [10]
    assert len(calls) == 2


def test_retry_after_http_date(fixture):
    sleeps = []
    future = datetime.now(UTC) + timedelta(seconds=30)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                429, json=fixture("throttled"), headers={"Retry-After": format_datetime(future)}
            )
        return httpx.Response(200, json=fixture("domain-list"))

    list(graph_with(handler, sleeps).collection("domains"))
    assert 28 <= sleeps[0] <= 30


def test_timeouts_are_bounded_and_actionable():
    calls = []
    sleeps = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("injected outage", request=request)

    with pytest.raises(HavocError, match="timed out"):
        graph_with(handler, sleeps).request("GET", "domains")
    assert len(calls) == 4
    assert sleeps == [1, 2, 4]


def test_policy_creation_is_not_replayed_after_timeout():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("lost creation response", request=request)

    with pytest.raises(HavocError):
        graph_with(handler).request("POST", "identity/conditionalAccess/policies", retry_safe=False)
    assert len(calls) == 1


def test_paging_cannot_leak_token_to_another_host(fixture):
    page = fixture("domain-list")
    # Fault injection into a published example, not a purported Graph capture.
    page["@odata.nextLink"] = "https://example.org/steal"
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=page)

    with pytest.raises(HavocError, match="outside Microsoft Graph"):
        list(graph_with(handler).collection("domains"))
    assert len(calls) == 1


def test_repeated_page_fails_instead_of_hanging(fixture):
    with pytest.raises(HavocError, match="repeated a paging URL"):
        list(
            graph_with(lambda _: httpx.Response(200, json=fixture("signin-list"))).collection(
                "auditLogs/signIns"
            )
        )


def test_missing_permissions_are_not_success(fixture):
    # Inject a forbidden HTTP status; the error body itself is copied from Learn.
    graph = graph_with(lambda _: httpx.Response(403, json=fixture("throttled")))
    with pytest.raises(GraphError, match="admin consent") as error:
        graph.request("GET", "domains")
    assert error.value.status == 403


def test_revert_delete_is_idempotent():
    graph = graph_with(lambda _: httpx.Response(404))
    assert graph.request("DELETE", "identity/conditionalAccess/policies/missing") == {}
