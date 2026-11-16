import re
from io import StringIO

from rich.console import Console

from havoc365 import cli
from havoc365.drills import Pilot
from havoc365.mapping import Finding, MapReport
from havoc365.ui import UI


def example_report():
    return MapReport(
        "contoso.com",
        {
            "Exchange Online": (
                Finding("SKIPPED: sign-in logs unavailable", skipped=True),
                Finding(
                    "4 mail dependencies", ("alerts", "scanner", "billing", "[red]literal[/red]")
                ),
            ),
            "SharePoint": (Finding("0 runbook matches"),),
        },
        ("No matches does not prove independence.",),
    )


def terminal_ui():
    ui = UI()
    stream = StringIO()
    ui.console = Console(file=stream, width=100, force_terminal=True, color_system="standard")
    return ui, stream


def test_terminal_map_shows_skipped_checks_without_claiming_health():
    ui, stream = terminal_ui()
    ui.show_map(example_report())
    output = stream.getvalue()
    assert "dependency map" in output
    assert "not a live service-health check" in output
    assert "SKIPPED" in output
    assert "1 check(s) skipped" in output
    assert "No matches does not prove independence." in output
    assert "\x1b[" in output


def test_redirected_output_stays_plain_and_complete(capsys):
    ui = UI()
    report = example_report()
    ui.show_map(report)
    assert capsys.readouterr().out == report.plain() + "\n"


def test_explorer_displays_all_details_and_treats_names_as_literal(monkeypatch):
    ui, stream = terminal_ui()
    choices = iter(["bad", "1", "2", "l", "q"])
    monkeypatch.setattr(ui.console, "input", lambda _, **kwargs: next(choices))
    ui.explore(example_report())
    output = stream.getvalue()
    assert "Choose a listed number" in output
    assert "billing" in output
    assert "[red]literal[/red]" in output
    assert "Not checked" in output
    assert "This does not prove independence" in output
    assert "Coverage and limits" in output


def test_explorer_exits_cleanly_on_closed_input(monkeypatch):
    ui, _ = terminal_ui()

    def eof(_, **kwargs):
        raise EOFError

    monkeypatch.setattr(ui.console, "input", eof)
    ui.explore(example_report())


def test_explorer_displays_default_quit_key(monkeypatch):
    ui, stream = terminal_ui()
    monkeypatch.setattr("sys.stdin", StringIO("q\n"))
    ui.explore(example_report())
    assert "View [q]:" in re.sub(r"\x1b\[[0-9;]*m", "", stream.getvalue())


def test_no_color_environment_is_respected(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    stream = StringIO()
    ui = UI()
    ui.console = Console(file=stream, force_terminal=True)
    ui.show_map(example_report())
    assert not re.search(r"\x1b\[(?:3\d|9\d|38;|4\d|10\d|48;)", stream.getvalue())


def test_pilot_preview_lists_roster_and_token_limit():
    ui, stream = terminal_ui()
    ui.show_pilot(Pilot("group-id", "Volunteers", ("user-id",), ("Someone",), 1800))
    assert "Someone" in stream.getvalue()
    assert "1800 seconds" in stream.getvalue()
    assert "tokens" in stream.getvalue()


def test_report_and_errors_have_terminal_presentation():
    ui, stream = terminal_ui()
    ui.errors = ui.console
    ui.show_report("0 sign-in attempts")
    ui.error("Cannot read sign-in logs. Check your roles.")
    assert "Drill report" in stream.getvalue()
    assert "Check your roles" in stream.getvalue()


def test_map_no_interactive_never_opens_explorer(monkeypatch, tmp_path):
    ui, _ = terminal_ui()
    ui.interactive = True
    monkeypatch.setattr(cli, "UI", lambda: ui)

    def no_explorer(_):
        raise AssertionError("Explorer must not open")

    monkeypatch.setattr(ui, "explore", no_explorer)

    class Auth:
        def __init__(self, _):
            self.profile = {"tenant": "contoso.com"}

        def token(self, _):
            return "offline-token"

    class Graph:
        def __init__(self, _):
            pass

        def close(self):
            pass

    monkeypatch.setattr(cli, "Auth", Auth)
    monkeypatch.setattr(cli, "Graph", Graph)
    monkeypatch.setattr(cli, "map_tenant", lambda *_: example_report())
    monkeypatch.chdir(tmp_path)
    assert cli.main(["map", "--no-interactive"]) == 0
    assert (tmp_path / "blast-radius.md").exists()


def test_explorer_searches_evidence_and_accepts_service_names(monkeypatch):
    ui, stream = terminal_ui()
    choices = iter(["/billing", "sharepoint", "/missing", "q"])
    monkeypatch.setattr(ui.console, "input", lambda _, **kwargs: next(choices))
    ui.explore(example_report())
    output = stream.getvalue()
    assert "billing" in output
    assert "0 runbook matches" in output
    assert "No matches for" in output


def test_monitor_interrupt_does_not_suggest_reverting_a_drill(monkeypatch, capsys):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "watch", interrupt)
    assert cli.main(["watch", "--url", "https://example.org"]) == 130
    message = capsys.readouterr().err
    assert "Interrupted." in message
    assert "drill revert" not in message
