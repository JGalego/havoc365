from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import dns.exception
import dns.resolver

from havoc365.errors import GraphError, HavocError
from havoc365.graph import NO_PREMIUM, Graph
from havoc365.storage import JsonObject

GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"


def mx_hosts(domain: str) -> list[str]:
    try:
        return [
            str(answer.exchange).rstrip(".").lower()
            for answer in dns.resolver.resolve(domain, "MX", lifetime=10)
        ]
    except dns.resolver.NoAnswer:
        return []
    except (dns.exception.DNSException, UnicodeError) as error:
        raise HavocError(f"MX lookup failed for {domain}: {error}. Check DNS and retry.") from error


def search_runbooks(graph: Graph) -> list[JsonObject]:
    offset = 0
    results: dict[str, JsonObject] = {}
    while True:
        response = graph.request(
            "POST",
            "search/query",
            body={
                "requests": [
                    {
                        "entityTypes": ["driveItem"],
                        "query": {
                            "queryString": 'runbook OR "major incident" OR "incident response"'
                        },
                        "from": offset,
                        "size": 200,
                    }
                ]
            },
        )
        try:
            containers = response["value"][0]["hitsContainers"]
            if len(containers) != 1:
                raise ValueError("unexpected number of search containers")
            container = containers[0]
            hits = container.get("hits", [])
            more = container["moreResultsAvailable"]
            if not isinstance(hits, list) or not isinstance(more, bool):
                raise ValueError("invalid search page")
            previous = len(results)
            for hit in hits:
                resource = hit["resource"]
                url = resource["webUrl"]
                hostname = urlsplit(url).hostname or ""
                if hostname.endswith(".sharepoint.com"):
                    results[url] = resource
            if not more:
                return list(results.values())
            if not hits or len(results) == previous:
                raise ValueError("search paging made no progress")
            offset += len(hits)
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise HavocError(
                "Graph file search returned an invalid or repeated page. Retry later."
            ) from error


def _names(items: list[str]) -> str:
    labels = sorted({str(item).replace("\n", " ").replace("\r", " ") for item in items})
    return ", ".join(labels[:3]) + (f", +{len(labels) - 3}" if len(labels) > 3 else "")


def map_tenant(graph: Graph, tenant: str) -> str:
    domains = list(graph.collection("domains"))
    principals = list(
        graph.collection(
            "servicePrincipals",
            params={"$select": "id,appId,displayName,accountEnabled,servicePrincipalType"},
        )
    )
    applications = list(
        graph.collection(
            "applications", params={"$select": "id,displayName,requiredResourceAccess"}
        )
    )
    notes = [
        "SMTP observations cover the last 7 days, not every sender or device.",
        "Mail.Send entries are requested permissions, not proof of consent or recent use.",
        "File search covers only files this account can read; matches need manual review.",
        "No matches does not prove that a dependency is absent.",
    ]
    smtp: set[str] = set()
    since = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    try:
        for event in graph.collection(
            "auditLogs/signIns", params={"$filter": f"createdDateTime ge {since}"}
        ):
            if event.get("clientAppUsed") == "Authenticated SMTP":
                smtp.add(
                    event.get("userPrincipalName") or event.get("appDisplayName") or event["id"]
                )
        smtp_line = f"  {len(smtp)} identities used SMTP AUTH"
        if smtp:
            smtp_line += f"   {_names(list(smtp))}"
    except GraphError as error:
        if error.code != NO_PREMIUM:
            raise
        smtp_line = "  SKIPPED: SMTP AUTH observations (sign-in logs unavailable)"
        notes.append(
            f"Graph returned {NO_PREMIUM}. Check Entra ID P1/P2 licensing; "
            "SMTP sign-in checks were skipped, not passed."
        )
    mail_ids: set[str] = set()
    graph_principals = [item for item in principals if item.get("appId") == GRAPH_APP_ID]
    if len(graph_principals) == 1:
        permissions = graph.request(
            "GET",
            f"servicePrincipals/{graph_principals[0]['id']}",
            params={"$select": "appRoles,oauth2PermissionScopes"},
        )
        mail_ids = {
            entry["id"]
            for entry in permissions.get("appRoles", [])
            + permissions.get("oauth2PermissionScopes", [])
            if entry.get("value") == "Mail.Send"
        }
        if not mail_ids:
            raise HavocError(
                "Graph's service principal returned no Mail.Send permission definitions."
            )
    else:
        notes.append(
            "SKIPPED: Mail.Send checks; the Microsoft Graph service principal was not found."
        )
    mail_apps = [
        app["displayName"]
        for app in applications
        if any(
            resource.get("resourceAppId") == GRAPH_APP_ID
            and any(access.get("id") in mail_ids for access in resource.get("resourceAccess", []))
            for resource in app.get("requiredResourceAccess", [])
        )
    ]
    files = search_runbooks(graph)
    entra = [
        item.get("displayName", item.get("id", "(unnamed app)"))
        for item in principals
        if item.get("accountEnabled") and item.get("servicePrincipalType") == "Application"
    ]
    lines = [tenant, "", "If Exchange Online goes down", smtp_line]
    if mail_ids:
        lines.append(
            f"  {len(mail_apps)} apps request Microsoft Graph Mail.Send"
            + (f"   {_names(mail_apps)}" if mail_apps else "")
        )
    for domain in domains:
        if domain.get("isVerified") and "Email" in domain.get("supportedServices", []):
            name = domain["id"]
            hosts = mx_hosts(name)
            direct = [host for host in hosts if host.endswith(".mail.protection.outlook.com")]
            if direct:
                lines.append(f"  {name} has an MX route straight to Exchange   {', '.join(direct)}")
            elif not hosts:
                notes.append(f"{name} has no MX records.")
    lines += [
        "",
        "If SharePoint goes down",
        f"  {len(files)} runbook search matches in SharePoint"
        + (f"   {_names([item['name'] for item in files])}" if files else ""),
        "",
        "If Entra ID sign-in goes down",
        f"  {len(entra)} enabled enterprise apps use Entra ID"
        + (f"   {_names(entra)}" if entra else ""),
        "",
        "Limits and skipped checks",
        *(f"  - {note}" for note in notes),
    ]
    return "\n".join(lines)
