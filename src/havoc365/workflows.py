import json
import re
import zipfile
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

from havoc365.errors import HavocError
from havoc365.mapping import Finding, MapReport
from havoc365.storage import JsonObject

MAX_BYTES = 25 * 1024 * 1024
CONNECTORS = {
    "Exchange Online": ("office365", "shared_office365"),
    "SharePoint": ("sharepointonline", "shared_sharepointonline"),
    "Teams": ("teams", "shared_teams"),
}


def _documents(path: Path) -> Iterator[JsonObject]:
    try:
        if path.stat().st_size > MAX_BYTES:
            raise HavocError(f"Export {path.name} exceeds 25 MiB. Export fewer workflows.")
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                entries = [
                    item for item in archive.infolist() if item.filename.lower().endswith(".json")
                ]
                if sum(item.file_size for item in entries) > MAX_BYTES or len(entries) > 1000:
                    raise HavocError(f"Export {path.name} expands beyond the import limit.")
                for entry in entries:
                    value = json.loads(archive.read(entry))
                    if isinstance(value, dict):
                        yield value
        else:
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise HavocError(f"Export {path.name} must contain a JSON object.")
            yield value
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as error:
        raise HavocError(
            f"Cannot read workflow export {path.name}: {error}. Export it again."
        ) from error


def _definitions(value: object, label: str) -> Iterator[tuple[str, JsonObject]]:
    if isinstance(value, dict):
        properties = value.get("properties", {})
        if isinstance(properties, dict):
            label = str(properties.get("displayName") or value.get("name") or label)
        if isinstance(value.get("actions"), dict) or isinstance(value.get("triggers"), dict):
            yield label, value
        else:
            for child in value.values():
                yield from _definitions(child, label)
    elif isinstance(value, list):
        for child in value:
            yield from _definitions(child, label)


def _references(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        host = value.get("host")
        if isinstance(host, dict):
            yield json.dumps(host)
        for key in ("connectionReferences", "connectorId", "apiId"):
            if key in value:
                yield json.dumps(value[key])
        inputs = value.get("inputs")
        if isinstance(inputs, dict) and isinstance(inputs.get("uri"), str):
            uri = urlsplit(inputs["uri"])
            if uri.hostname and (
                uri.hostname.endswith((".webhook.office.com", ".webhook.office365.com"))
                or (uri.hostname == "outlook.office.com" and uri.path.startswith("/webhook/"))
            ):
                yield "shared_teams"
        for child in value.values():
            yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def add_workflows(report: MapReport, paths: list[Path]) -> MapReport:
    dependencies: dict[str, set[str]] = {service: set() for service in CONNECTORS}
    workflows: list[str] = []
    for path in paths:
        found = False
        for document in _documents(path):
            for label, definition in _definitions(document, path.name):
                found = True
                label = label.replace("\r", " ").replace("\n", " ")[:200]
                workflows.append(label)
                references = " ".join(_references(definition)).lower()
                for service, connectors in CONNECTORS.items():
                    if any(
                        re.search(rf"(?<![a-z0-9_]){re.escape(connector)}(?![a-z0-9_])", references)
                        for connector in connectors
                    ):
                        dependencies[service].add(label)
        if not found:
            raise HavocError(
                f"No workflow definitions found in {path.name}. "
                "Supply a Power Automate JSON/ZIP export or a workflow deployment template."
            )
    sections = dict(report.sections)
    for service, names in dependencies.items():
        sections[service] = (
            *sections.get(service, ()),
            Finding(f"{len(names)} exported workflows reference {service}", tuple(sorted(names))),
        )
    sections["Power Automate"] = (
        Finding(f"{len(workflows)} exported workflow definitions inspected", tuple(workflows)),
    )
    return MapReport(
        report.tenant,
        sections,
        (
            *report.notes,
            "Workflow checks cover supplied exports only, not a tenant-wide flow inventory.",
            "Connector references and legacy Teams webhook URLs don't prove recent use.",
            "Workflow exports can contain secrets. Keep them outside version control.",
        ),
    )
