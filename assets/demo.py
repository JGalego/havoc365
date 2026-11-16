"""Run the real map CLI against selected, published Microsoft Learn examples."""

import json
import socket
import sys
from pathlib import Path
from unittest.mock import patch

import httpx

from havoc365 import cli
from havoc365.graph import Graph
from havoc365.storage import JsonObject

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def example(name: str) -> JsonObject:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def respond(request: httpx.Request) -> httpx.Response:
    names = {
        "/v1.0/domains": "domain-list",
        "/v1.0/servicePrincipals": "serviceprincipal-list",
        "/v1.0/applications": "application-list",
        "/v1.0/auditLogs/signIns": "signin-list",
        "/v1.0/search/query": "search-files",
    }
    if request.url.path not in names:
        raise AssertionError(f"Unexpected demo request: {request.method} {request.url.path}")
    response = example(names[request.url.path])
    response.pop("@odata.nextLink", None)
    if request.url.path == "/v1.0/domains":
        # Published Graph examples contain no DNS responses; don't invent MX evidence.
        response["value"] = []
    return httpx.Response(200, json=response)


class ExampleAuth:
    def __init__(self, directory: Path) -> None:
        self.profile = {"tenant": "contoso.com / Microsoft Learn examples"}

    def token(self, drill: bool) -> str:
        if drill:
            raise AssertionError("This demo supports read-only map output only.")
        return "offline-example-not-a-credential"


def example_graph(token) -> Graph:
    return Graph(token, client=httpx.Client(transport=httpx.MockTransport(respond)))


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(argument != "--no-interactive" for argument in arguments):
        raise SystemExit("This recording helper only supports map [--no-interactive].")
    print("OFFLINE DEMO / havoc365 map")
    print("Published Microsoft Learn examples. Domains omitted: no DNS or tenant calls.\n")
    with (
        patch.object(cli, "Auth", ExampleAuth),
        patch.object(cli, "Graph", example_graph),
        patch.object(
            socket.socket, "connect", side_effect=AssertionError("Demo must stay offline")
        ),
        patch.object(socket.socket, "sendto", side_effect=AssertionError("Demo must stay offline")),
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("Demo must stay offline")),
    ):
        return cli.main(["map", *arguments])


if __name__ == "__main__":
    raise SystemExit(main())
