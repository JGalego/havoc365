import sys
from collections.abc import Iterator
from contextlib import contextmanager

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from havoc365.drills import Pilot
from havoc365.mapping import MapReport


class UI:
    def __init__(self) -> None:
        self.console = Console(highlight=False, markup=False)
        self.errors = Console(stderr=True, highlight=False, markup=False)
        self.interactive = self.console.is_terminal and sys.stdin.isatty()

    def message(self, message: str) -> None:
        if self.console.is_terminal:
            self.console.print(Text(message))
        else:
            print(message)

    def error(self, message: str) -> None:
        self.errors.print(Text(f"Error: {message}", style="bold red"))

    @contextmanager
    def working(self, message: str) -> Iterator[None]:
        if self.console.is_terminal:
            with self.console.status(Text(message), spinner="dots"):
                yield
        else:
            yield

    def show_map(self, report: MapReport) -> None:
        if not self.console.is_terminal:
            self.message(report.plain())
            return
        self.console.print(
            Panel(
                Text(report.tenant, style="bold"),
                title="havoc365 / dependency map",
                subtitle="Dependencies, not a live service-health check",
                border_style="cyan",
            )
        )
        table = Table(box=box.ROUNDED, expand=True)
        table.add_column("If this fails", style="cyan", no_wrap=True)
        table.add_column("Evidence")
        for service, findings in report.sections.items():
            for index, finding in enumerate(findings):
                table.add_row(
                    service if index == 0 else "",
                    Text(finding.summary, style="yellow" if finding.skipped else ""),
                )
            table.add_section()
        self.console.print(table)
        skipped = sum(
            finding.skipped for findings in report.sections.values() for finding in findings
        )
        if skipped:
            self.console.print(
                Text(f"{skipped} check(s) skipped. Review the limits below.", style="yellow")
            )
        self.console.print(
            Panel(
                Text("\n".join(f"- {note}" for note in report.notes)),
                title="Coverage and limits",
                border_style="yellow",
            )
        )

    def explore(self, report: MapReport) -> None:
        services = list(report.sections)
        menu = "  ".join(f"{index} {service}" for index, service in enumerate(services, 1))
        while True:
            self.console.print(
                Text("\nInspect: " + menu + "  |  l limits  |  q quit", style="cyan")
            )
            try:
                choice = self.console.input("View [q]: ").strip().lower()
            except EOFError:
                return
            if choice in {"", "q", "quit"}:
                return
            if choice == "l":
                self.console.print(
                    Panel(
                        Text("\n".join(report.notes)),
                        title="Coverage and limits",
                        border_style="yellow",
                    )
                )
                continue
            if (
                not choice.isascii()
                or not choice.isdigit()
                or not 1 <= int(choice) <= len(services)
            ):
                self.console.print(Text("Choose a listed number, l, or q.", style="yellow"))
                continue
            service = services[int(choice) - 1]
            table = Table(title=f"If {service} goes down", box=box.ROUNDED, expand=True)
            table.add_column("Check", style="cyan")
            table.add_column("Full evidence")
            for finding in report.sections[service]:
                table.add_row(
                    Text(finding.summary),
                    Text(
                        "\n".join(finding.details)
                        or (
                            "Not checked; see coverage limits."
                            if finding.skipped
                            else "No matches in the data checked. This does not prove independence."
                        )
                    ),
                )
            self.console.print(table)

    def show_pilot(self, pilot: Pilot) -> None:
        if not self.console.is_terminal:
            self.message(pilot.describe())
            return
        self.console.print(
            Panel(
                Text(f"{pilot.name}\n{pilot.seconds} seconds / {len(pilot.members)} people"),
                title="Outage drill / preview",
                border_style="yellow",
            )
        )
        table = Table(box=box.ROUNDED, expand=True)
        table.add_column("Volunteer", style="cyan")
        table.add_column("User ID")
        for label, member in zip(pilot.labels, pilot.members, strict=True):
            table.add_row(Text(label), Text(member))
        self.console.print(table)
        self.console.print(
            Text("Existing access tokens may work until they expire.", style="yellow")
        )

    def show_report(self, text: str) -> None:
        if self.console.is_terminal:
            self.console.print(Panel(Text(text), title="Drill report", border_style="cyan"))
        else:
            self.message(text)
