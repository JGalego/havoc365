"""Full-screen, offline-only deployment configuration."""

import re
from collections.abc import Callable
from typing import ClassVar

from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, Collapsible, Footer, Input, Label, Select, Static

from havoc365 import deployment as deploy
from havoc365.errors import HavocError

PROVIDER_LABELS = {
    "aws": "AWS / EC2",
    "gcp": "GCP / Compute Engine",
    "alibaba": "Alibaba Cloud / ECS",
    "ssh": "Existing host / SSH",
}
PROVIDER_NAMES = {"aws": "AWS", "gcp": "GCP", "alibaba": "Alibaba Cloud", "ssh": "SSH"}
PROVIDER_COLORS = {"aws": "#ffbe6f", "gcp": "#92c5ff", "alibaba": "#ff9f70", "ssh": "#80d9bf"}
LABELS = {
    "name": "Target name",
    "hostname": "Public hostname",
    "ssh_user": "SSH user",
    "ssh_port": "SSH port",
    "host": "SSH host or IP",
    "region": "Region",
    "zone": "Zone",
    "project": "Project",
    "image": "Ubuntu 24.04 image",
    "instance_type": "Instance type",
    "vpc_id": "VPC ID",
    "subnet_id": "Public subnet ID",
}


class FormBody(VerticalScroll):
    def reveal(self, control: Widget) -> None:
        if self in control.ancestors:
            self.call_after_refresh(
                self.scroll_to_widget, control, animate=False, immediate=True, origin_visible=False
            )

    def on_resize(self) -> None:
        if self.app.focused:
            self.reveal(self.app.focused)


class DeploymentApp(App[dict]):
    TITLE = "havoc365 / lifeboat"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+q", "cancel", show=False, priority=True),
        Binding("alt+left", "back", "Back"),
        Binding("alt+right", "next", "Next"),
    ]
    CSS = """
    Screen { background: #0b1017; color: #e6edf5; align-horizontal: center; }
    #frame { width: 100%; max-width: 84; height: 1fr; }
    #brand { height: 2; padding: 0 2; color: #92c5ff; text-style: bold; }
    #step { height: 2; padding: 0 2; color: #a4b3c5; }
    #body { height: 1fr; padding: 0 2; }
    .heading { height: auto; margin-bottom: 1; text-style: bold; }
    .hint { height: auto; color: #a4b3c5; margin-bottom: 1; }
    Label { height: 1; }
    Input, Select { margin-bottom: 1; }
    Input.invalid { border: tall #ff8c82; }
    #target-list { height: auto; }
    .target-row { height: 3; margin-bottom: 1; background: #131e2b; }
    .target-provider-name { width: 24%; min-width: 14; padding: 1 0 0 1; }
    .target-name { width: 1fr; margin: 0 1; }
    .target-role { width: 10; padding-top: 1; }
    .target-remove { width: 3; min-width: 3; padding: 0; }
    #provider { margin-bottom: 1; }
    #fleet-summary { height: auto; margin-bottom: 1; color: #80d9bf; }
    #topology { margin-bottom: 0; }
    #topology-note { height: auto; color: #ffd18a; }
    Collapsible { padding: 0; margin-bottom: 1; }
    #error { height: auto; max-height: 4; color: #ff8c82; padding: 0 2; }
    #error:empty { display: none; }
    #navigation { height: 3; padding: 0 2; margin-top: 1; }
    #cancel { margin-right: 1; }
    #back { margin-right: 1; }
    #next { min-width: 10; }
    #review { height: auto; margin-bottom: 1; }
    Footer { background: #131e2b; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.page = 0
        self.targets: list[dict[str, str]] = []
        self.row_ids: list[int] = []
        self.next_row_id = 0
        self.topology = "shared"
        self.settings = {
            "ssh_key": "~/.ssh/id_ed25519",
            "ssh_cidr": "",
            "repository": deploy.DEFAULT_REPOSITORY,
            "ref": "main",
        }
        self.probes = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="frame"):
            yield Static("havoc365 / lifeboat   Offline planner", id="brand")
            yield Static(id="step")
            yield FormBody(id="body")
            yield Static("", id="error", markup=False)
            with Horizontal(id="navigation"):
                yield Button("Cancel", id="cancel")
                yield Button("Back", id="back")
                yield Button("Next", variant="primary", id="next")
        yield Footer()

    async def on_mount(self) -> None:
        await self.show_page()

    def form_field(self, name: str, label: str, value: str, hint: str = "") -> list[Widget]:
        widgets = [Label(label), Input(value, id=f"field-{name}")]
        if hint:
            widgets.append(Static(hint, classes="hint", markup=False))
        return widgets

    async def show_page(self) -> None:
        body = self.query_one("#body", VerticalScroll)
        await body.remove_children()
        self.query_one("#error", Static).update("")
        self.query_one("#back", Button).disabled = self.page == 0
        last = self.page == len(self.targets) + 3
        self.query_one("#next", Button).label = "Save plan" if last else "Next"
        widgets = []
        if self.page == 0:
            widgets = [
                Static("Where will Lifeboat run?", classes="heading"),
                Vertical(*self.target_rows(), id="target-list"),
                Static(self.fleet_summary(), id="fleet-summary"),
                Label("Room layout"),
                Select(
                    [("One shared room", "shared"), ("Separate rooms", "independent")],
                    value=self.topology,
                    allow_blank=False,
                    id="topology",
                ),
                Static(self.topology_note(), id="topology-note", markup=False),
            ]
        elif self.page == 1:
            widgets = [
                Static("How will you connect?", classes="heading"),
                Static(
                    "Dedicated Ubuntu 24.04 hosts; trusted SSH keys and passwordless sudo.",
                    classes="hint",
                ),
                *self.form_field("ssh_key", "SSH private key", self.settings["ssh_key"]),
                *self.form_field(
                    "ssh_cidr",
                    "Your SSH source IPv4 CIDR",
                    self.settings["ssh_cidr"],
                    "For example 203.0.113.10/32. Not 0.0.0.0/0.",
                ),
                Collapsible(
                    *self.form_field(
                        "repository", "Public GitHub repository", self.settings["repository"]
                    ),
                    *self.form_field(
                        "ref",
                        "Git ref",
                        self.settings["ref"],
                        "main can change. Pin a reviewed commit for reproducible installs.",
                    ),
                    title="Source",
                ),
            ]
        elif self.page < len(self.targets) + 2:
            index = self.page - 2
            target = self.targets[index]
            provider = target["provider"]
            widgets = [
                Static(f"Configure {target['name']}", classes="heading", markup=False),
                Static(
                    f"{PROVIDER_LABELS[provider]} / {self.target_role(index)}",
                    classes="hint",
                    markup=False,
                ),
            ]
            names = ["name", "hostname", *deploy.FIELDS[provider], "ssh_user"]
            if provider == "ssh":
                names.append("ssh_port")
            for name in names:
                widgets.extend(self.form_field(name, LABELS[name], target.get(name, "")))
        elif self.page == len(self.targets) + 2:
            widgets = [
                Static("When should the room open?", classes="heading"),
                *self.form_field("probes", "HTTPS probe URLs (space-separated)", self.probes),
                Static(
                    "Leave blank for manual activation. URLs install a supervised watcher "
                    "on each authority; manual holds override it.",
                    classes="hint",
                ),
                Static("No SMS or invitations are sent by deployment.", classes="hint"),
            ]
        else:
            config = self.config()
            table = Table(expand=True, box=None, padding=(0, 1))
            table.add_column("Target")
            table.add_column("Role / provider")
            table.add_column("Hostname", overflow="fold")
            for index, target in enumerate(self.targets):
                gateway = self.topology == "shared" and index > 0
                table.add_row(
                    Text(target["name"]),
                    Text(("Gateway" if gateway else "Authority") + " / " + target["provider"]),
                    Text(target["hostname"]),
                )
            widgets = [
                Static("Ready to save an offline plan", classes="heading"),
                Static(table, id="review"),
                Static(self.topology_note(), classes="hint", markup=False),
                Static(
                    "Automatic activation / HTTPS watcher"
                    if config["watch"]["urls"]
                    else "Manual activation / no watcher",
                    classes="hint",
                ),
                Static(
                    f"Source ref: {config['ref']}"
                    + (" (mutable)" if not re.fullmatch(r"[a-fA-F0-9]{40}", config["ref"]) else ""),
                    classes="hint",
                    markup=False,
                ),
                Static(
                    "Save writes local files only. No cloud, SSH or DNS calls. "
                    "Check and apply are separate commands.",
                    classes="hint",
                ),
            ]
        self.update_journey()
        await body.mount(*widgets)
        body.scroll_home(animate=False)
        focusable = body.query("Input, Select")
        if focusable:
            focusable.first().focus(scroll_visible=False)
        else:
            self.query_one("#next", Button).focus(scroll_visible=False)
        self.call_after_refresh(body.scroll_home, animate=False)

    def topology_note(self) -> str:
        return (
            "First target owns the database; others are gateways. No replication or failover."
            if self.topology == "shared"
            else "Separate databases, people and invitations. No synchronization."
        )

    def update_journey(self) -> None:
        stage = (
            0
            if self.page == 0
            else 1
            if self.page == 1
            else 2
            if self.page < len(self.targets) + 2
            else 3
            if self.page == len(self.targets) + 2
            else 4
        )
        journey = Text()
        for index, label in enumerate(("Targets", "Access", "Hosts", "Activation", "Review")):
            if index:
                journey.append(" > ", style="#52677f")
            journey.append(
                label,
                style="bold #92c5ff"
                if index == stage
                else "#80d9bf"
                if index < stage
                else "#718198",
            )
        if stage == 2:
            journey.append(f"  {self.page - 1}/{len(self.targets)}", style="#92c5ff")
        self.query_one("#step", Static).update(journey)

    def target_role(self, index: int) -> str:
        return "Gateway" if self.topology == "shared" and index > 0 else "Authority"

    def fleet_summary(self) -> str:
        count = len(self.targets)
        if not count:
            return "Choose a provider. A fresh row appears after each selection."
        authorities = 1 if self.topology == "shared" else count
        gateways = count - authorities
        return (
            f"{count} target{'s' if count != 1 else ''} / "
            f"{authorities} authorit{'y' if authorities == 1 else 'ies'}"
            + (f" / {gateways} gateway{'s' if gateways != 1 else ''}" if gateways else "")
        )

    def target_rows(self) -> list[Widget]:
        rows: list[Widget] = [
            self.target_row(index, row_id, target)
            for index, (row_id, target) in enumerate(zip(self.row_ids, self.targets, strict=True))
        ]
        rows.append(
            Select(
                [(label, provider) for provider, label in PROVIDER_LABELS.items()],
                prompt=(
                    "20-target limit reached"
                    if len(self.targets) == 20
                    else "Choose another destination..."
                    if self.targets
                    else "Choose a destination..."
                ),
                id="provider",
                disabled=len(self.targets) == 20,
            )
        )
        return rows

    def target_row(self, index: int, row_id: int, target: dict[str, str]) -> Horizontal:
        provider = target["provider"]
        return Horizontal(
            Static(
                Text(
                    f"{index + 1:02} {PROVIDER_NAMES[provider]}",
                    style=f"bold {PROVIDER_COLORS[provider]}",
                ),
                id=f"provider-label-{row_id}",
                classes="target-provider-name",
            ),
            Input(
                target["name"],
                id=f"target-name-{row_id}",
                classes="target-name",
                max_length=40,
                tooltip="Unique target name; host settings follow on the Hosts step",
            ),
            Static(
                Text(self.target_role(index), style=PROVIDER_COLORS[provider]),
                id=f"role-{row_id}",
                classes="target-role",
            ),
            Button(
                "x",
                id=f"remove-{row_id}",
                classes="target-remove",
                tooltip=f"Remove {target['name']}",
            ),
            id=f"target-row-{row_id}",
            classes="target-row",
        )

    def collect(self) -> None:
        if self.page == 0:
            for row_id, target in zip(self.row_ids, self.targets, strict=True):
                target["name"] = self.query_one(f"#target-name-{row_id}", Input).value.strip()
            return
        values = {
            field.id.removeprefix("field-"): field.value.strip()
            for field in self.query("#body Input")
            if field.id
        }
        if self.page == 1:
            self.settings.update(values)
        elif 2 <= self.page < len(self.targets) + 2:
            self.targets[self.page - 2].update(values)
        elif self.page == len(self.targets) + 2:
            self.probes = values["probes"]

    def check_field(
        self, name: str, check: Callable[[str], object], *, prefix: str = "field-"
    ) -> None:
        field = self.query_one(f"#{prefix}{name}", Input)
        try:
            check(field.value.strip())
        except (HavocError, OSError):
            field.add_class("invalid")
            field.focus()
            for parent in field.ancestors:
                if isinstance(parent, Collapsible):
                    parent.collapsed = False
            self.query_one("#body", FormBody).reveal(field)
            raise

    def validate_page(self) -> None:
        if self.page == 0:
            if not self.targets:
                raise HavocError("Choose at least one destination.")
            names: list[str] = []
            for row_id, target in zip(self.row_ids, self.targets, strict=True):
                self.check_field(
                    str(row_id), lambda value: deploy._name(value, names), prefix="target-name-"
                )
                names.append(target["name"])
        elif self.page == 1:
            self.check_field("ssh_key", deploy._key_path)
            if any(target["provider"] != "ssh" for target in self.targets):
                self.check_field(
                    "ssh_key", lambda value: deploy._public_key(deploy._key_path(value))
                )
            self.check_field("ssh_cidr", deploy._cidr)
            self.check_field("repository", deploy._repository)
            self.check_field("ref", deploy._ref)
        elif 2 <= self.page < len(self.targets) + 2:
            index = self.page - 2
            target = self.targets[index]
            others = [other for i, other in enumerate(self.targets) if i != index]
            self.check_field(
                "name",
                lambda value: deploy._name(value, [other.get("name", "") for other in others]),
            )
            self.check_field(
                "hostname",
                lambda value: deploy._hostname(
                    value, "Public hostname", used=[other.get("hostname", "") for other in others]
                ),
            )
            self.check_field("ssh_user", deploy._user)
            if target["provider"] == "ssh":
                self.check_field("ssh_port", deploy._port)
            endpoints = [
                (other.get("host", ""), int(other.get("ssh_port", "22")))
                for other in others
                if other["provider"] == "ssh"
                and other.get("ssh_port", "22").isascii()
                and other.get("ssh_port", "22").isdigit()
                and len(other.get("ssh_port", "22")) <= 5
            ]
            for name in deploy.FIELDS[target["provider"]]:
                self.check_field(
                    name,
                    lambda value, name=name: deploy._field(
                        value,
                        name,
                        target["provider"],
                        port=int(target.get("ssh_port", "22")),
                        endpoints=endpoints,
                    ),
                )
        elif self.page == len(self.targets) + 2:
            self.check_field("probes", lambda value: deploy._watch_config({"urls": value.split()}))
            self.config()

    def config(self) -> dict:
        return deploy.validate_config(
            {
                "version": 1,
                "topology": self.topology,
                **self.settings,
                "targets": [
                    {
                        **target,
                        **(
                            {"ssh_port": int(target["ssh_port"])}
                            if target["provider"] == "ssh"
                            else {}
                        ),
                    }
                    for target in self.targets
                ],
                "watch": {"urls": self.probes.split()},
            }
        )

    async def action_next(self) -> None:
        self.collect()
        try:
            self.validate_page()
            if self.page == len(self.targets) + 3:
                self.exit(self.config())
                return
        except (HavocError, OSError) as error:
            self.query_one("#error", Static).update(str(error))
            return
        self.page += 1
        await self.show_page()

    async def action_back(self) -> None:
        if self.page:
            self.collect()
            self.page -= 1
            await self.show_page()

    def action_cancel(self) -> None:
        self.exit()

    async def on_select_changed(self, event: Select.Changed) -> None:
        if self.page != 0 or not isinstance(event.value, str):
            return
        if event.select.id == "topology":
            self.topology = event.value
            self.query_one("#topology-note", Static).update(self.topology_note())
            self.refresh_targets()
        elif event.select.id == "provider" and event.select is self.query_one("#provider"):
            if len(self.targets) == 20:
                self.query_one("#error", Static).update("At most 20 targets per plan.")
                return
            self.collect()
            provider = event.value
            names = {target["name"] for target in self.targets}
            number = 1
            while f"{provider}-{number}" in names:
                number += 1
            self.targets.append(
                {
                    "provider": provider,
                    **deploy.DEFAULTS[provider],
                    "name": f"{provider}-{number}",
                    "ssh_user": "root" if provider == "alibaba" else "ubuntu",
                    **({"ssh_port": "22"} if provider == "ssh" else {}),
                }
            )
            row_id = self.next_row_id
            self.row_ids.append(row_id)
            self.next_row_id += 1
            await self.query_one("#target-list", Vertical).mount(
                self.target_row(len(self.targets) - 1, row_id, self.targets[-1]),
                before=event.select,
            )
            with event.select.prevent(Select.Changed):
                event.select.value = Select.NULL
            self.refresh_targets()
            self.focus_destination()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "next":
                await self.action_next()
            case "back":
                await self.action_back()
            case "cancel":
                self.action_cancel()
            case button_id if button_id and button_id.startswith("remove-"):
                row_id = int(button_id.removeprefix("remove-"))
                if row_id in self.row_ids:
                    self.collect()
                    index = self.row_ids.index(row_id)
                    self.targets.pop(index)
                    self.row_ids.pop(index)
                    await self.query_one(f"#target-row-{row_id}").remove()
                    self.refresh_targets()
                    self.focus_destination()

    def refresh_targets(self) -> None:
        for index, (row_id, target) in enumerate(zip(self.row_ids, self.targets, strict=True)):
            provider = target["provider"]
            self.query_one(f"#provider-label-{row_id}", Static).update(
                Text(
                    f"{index + 1:02} {PROVIDER_NAMES[provider]}",
                    style=f"bold {PROVIDER_COLORS[provider]}",
                )
            )
            self.query_one(f"#role-{row_id}", Static).update(
                Text(self.target_role(index), style=PROVIDER_COLORS[provider])
            )
        selector = self.query_one("#provider", Select)
        selector.disabled = len(self.targets) == 20
        selector.prompt = (
            "20-target limit reached"
            if len(self.targets) == 20
            else "Choose another destination..."
            if self.targets
            else "Choose a destination..."
        )
        self.query_one("#fleet-summary", Static).update(self.fleet_summary())
        self.query_one("#error", Static).update("")

    def focus_destination(self) -> None:
        if len(self.targets) < 20:
            control: Widget = self.query_one("#provider", Select)
        else:
            control = self.query_one("#next", Button)
        control.focus(scroll_visible=False)
        self.query_one("#body", FormBody).reveal(control)

    def on_input_changed(self, event: Input.Changed) -> None:
        event.input.remove_class("invalid")


def run_wizard() -> dict:
    config = DeploymentApp().run()
    if config is None:
        raise KeyboardInterrupt
    return config
