import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import requests

from havoc365 import cli
from havoc365.auth import DRILL_SCOPES, MAP_SCOPES, Auth
from havoc365.errors import GraphError, HavocError
from havoc365.graph import NO_PREMIUM
from havoc365.mapping import map_tenant, mx_hosts, search_runbooks
from havoc365.storage import read_json, write_json


class MappingGraph:
    def __init__(self, fixture, error=None):
        self.fixture = fixture
        self.error = error
        self.calls = []

    def collection(self, path, *, params=None):
        self.calls.append(("GET", path, params))
        if path == "auditLogs/signIns":
            if self.error:
                raise self.error
            yield from self.fixture("signin-list")["value"]
        else:
            names = {
                "domains": "domain-list",
                "applications": "application-list",
                "servicePrincipals": "serviceprincipal-list",
            }
            yield from self.fixture(names[path])["value"]

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        assert path == "search/query"
        return self.fixture("search-files")


def test_map_uses_sourced_data_and_reports_limits(fixture, monkeypatch):
    monkeypatch.setattr(
        "havoc365.mapping.mx_hosts", lambda _: ["contoso-com.mail.protection.outlook.com"]
    )
    graph = MappingGraph(fixture)
    output = map_tenant(graph, "contoso.com")
    assert "contoso.com has an MX route straight to Exchange" in output
    assert "1 runbook search matches in SharePoint" in output
    assert "1 enabled enterprise apps use Entra ID" in output
    assert "0 identities used SMTP AUTH" in output
    assert "SKIPPED: Mail.Send checks" in output
    assert "only files this account can read" in output
    assert all(call[0] in {"GET", "POST"} for call in graph.calls)
    assert [call[1] for call in graph.calls if call[0] == "POST"] == ["search/query"]


def test_map_skips_only_documented_premium_license_error(fixture, monkeypatch):
    monkeypatch.setattr("havoc365.mapping.mx_hosts", lambda _: [])
    output = map_tenant(
        MappingGraph(fixture, GraphError(403, NO_PREMIUM, "Tenant doesn't have premium license")),
        "contoso.com",
    )
    assert "SKIPPED: SMTP AUTH" in output
    assert "skipped, not passed" in output
    with pytest.raises(GraphError, match="admin consent"):
        map_tenant(
            MappingGraph(fixture, GraphError(403, "Forbidden", "Access denied")), "contoso.com"
        )


def test_file_search_follows_search_pagination(fixture):
    class Search:
        def __init__(self):
            self.offsets = []

        def request(self, method, path, *, body):
            self.offsets.append(body["requests"][0]["from"])
            page = fixture("search-files")
            page["value"][0]["hitsContainers"][0]["moreResultsAvailable"] = len(self.offsets) == 1
            return page

    graph = Search()
    assert len(search_runbooks(graph)) == 1
    assert graph.offsets == [0, 1]


def test_dns_timeout_is_not_reported_as_no_mx(monkeypatch):
    import dns.exception

    def timeout(*args, **kwargs):
        raise dns.exception.Timeout

    monkeypatch.setattr("dns.resolver.resolve", timeout)
    with pytest.raises(HavocError, match="MX lookup failed"):
        mx_hosts("contoso.com")


def test_readme_permission_table_matches_requested_scopes():
    text = (Path(__file__).parents[1] / "README.md").read_text()
    import re

    table = text.split("## Permissions\n", 1)[1].split("## ", 1)[0]
    rows = {
        line.split("|")[1].strip(): line for line in table.splitlines() if line.startswith("| `")
    }
    documented_map = set(re.findall(r"`([A-Za-z]+\.[A-Za-z.]+)`", rows["`map`"]))
    documented_extra = set(re.findall(r"`([A-Za-z]+\.[A-Za-z.]+)`", rows["`drill`"]))
    assert set(MAP_SCOPES) == documented_map
    assert set(DRILL_SCOPES) == documented_map | documented_extra


def test_private_state_is_atomic_and_permissions_restricted(tmp_path):
    path = tmp_path / "state" / "profile.json"
    write_json(path, {"tenant": "contoso.com"})
    assert read_json(path) == {"tenant": "contoso.com"}
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob(".profile.json-*"))


@pytest.mark.parametrize(
    ("tenant", "client"),
    [
        ("https://example.org", "00000000-0000-0000-0000-000000000000"),
        ("common", "00000000-0000-0000-0000-000000000000"),
        ("contoso.com", "not-an-app-id"),
    ],
)
def test_invalid_login_is_rejected_locally(tenant, client, tmp_path):
    with pytest.raises(HavocError):
        Auth(tmp_path).login(tenant, client, print)


def test_sign_in_network_failure_has_actionable_error(monkeypatch, tmp_path):
    def offline(*args):
        raise requests.ConnectionError("injected outage")

    monkeypatch.setattr(Auth, "_open", offline)
    with pytest.raises(HavocError, match="sign-in is unavailable"):
        Auth(tmp_path).login("contoso.com", "00000000-0000-0000-0000-000000000000", print)


def test_silent_token_uses_documented_response_and_requested_scopes(fixture, tmp_path):
    auth = Auth(tmp_path)
    auth.profile = {"tenant": "contoso.com", "oid": "cached-account", "tid": "cached-tenant"}
    calls = []

    class CachedSession:
        def get_accounts(self):
            return [{"local_account_id": "cached-account"}]

        def acquire_token_silent(self, scopes, account):
            calls.append((scopes, account))
            return fixture("device-token")

    auth.app = CachedSession()
    token = auth._acquire(DRILL_SCOPES, lambda _: pytest.fail("No device flow expected"))
    assert token == fixture("device-token")["access_token"]
    assert calls[0][0] == list(DRILL_SCOPES)
    assert read_json(tmp_path / "profile.json")["tid"] == "cached-tenant"
    assert (tmp_path / "tokens.json").stat().st_mode & 0o777 == 0o600


def test_map_without_login_fails_with_instructions(capsys):
    assert cli.main(["map"]) == 1
    assert "havoc365 login --tenant" in capsys.readouterr().err


def test_cli_map_writes_report(fixture, tmp_path, monkeypatch, capsys):
    class SignedIn:
        def __init__(self, directory):
            self.profile = {"tenant": "contoso.com"}

        def token(self, drill):
            return "offline-test-token"

    class OfflineGraph(MappingGraph):
        def __init__(self, token):
            super().__init__(fixture)

        def close(self):
            pass

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "Auth", SignedIn)
    monkeypatch.setattr(cli, "Graph", OfflineGraph)
    monkeypatch.setattr("havoc365.mapping.mx_hosts", lambda _: [])
    assert cli.main(["map"]) == 0
    assert "Report written to blast-radius.md" in capsys.readouterr().out
    assert "Tenant dependencies" in (tmp_path / "blast-radius.md").read_text()


@pytest.mark.parametrize("arguments", [["--help"], ["drill", "--help"], ["drill", "run", "--help"]])
def test_module_cli_help_without_network(arguments):
    result = subprocess.run(
        [sys.executable, "-m", "havoc365", *arguments],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert "usage:" in result.stdout


def test_fixture_sources_are_recorded_and_published():
    root = Path(__file__).parent / "fixtures"
    sources = json.loads((root / "sources.json").read_text())
    assert set(sources) == {
        path.name for path in root.glob("*.json") if path.name != "sources.json"
    }
    for metadata in sources.values():
        assert metadata["url"].startswith("https://learn.microsoft.com/")
        assert metadata["kind"].startswith("published Microsoft Learn example")


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("HAVOC365_TEST_TENANT"), reason="No dedicated test tenant")
def test_live_domains_read_only():
    from havoc365.graph import Graph

    auth = Auth()
    token = auth.token()
    expected = os.environ["HAVOC365_TEST_TENANT"]
    assert expected.lower() in {auth.profile["tenant"].lower(), auth.profile["tid"].lower()}
    graph = Graph(lambda: token)
    try:
        assert list(graph.collection("domains"))
    finally:
        graph.close()
