import argparse
import signal
import sys
from pathlib import Path

from havoc365.auth import Auth
from havoc365.drills import plan, report, revert, run
from havoc365.errors import HavocError
from havoc365.graph import Graph
from havoc365.mapping import MapReport, map_tenant
from havoc365.storage import state_dir, write_private
from havoc365.ui import UI
from havoc365.watch import watch
from havoc365.workflows import add_workflows


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="havoc365", description="Downtime by appointment.")
    root.add_argument("--version", action="version", version="havoc365 0.1.0.dev0")
    commands = root.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Sign in with a device code")
    login.add_argument("--tenant", required=True)
    login.add_argument("--client-id", required=True)
    mapping = commands.add_parser("map", help="Map tenant dependencies")
    mapping.add_argument(
        "--no-interactive", action="store_true", help="Show the map without the terminal explorer"
    )
    mapping.add_argument(
        "--flows",
        type=Path,
        action="append",
        default=[],
        help="Inspect a workflow JSON/ZIP export (repeatable)",
    )
    mapping.add_argument(
        "--exports-only",
        action="store_true",
        help="Inspect supplied exports without signing in or making network calls",
    )
    drill = commands.add_parser("drill", help="Plan, run, revert or report an outage drill")
    drills = drill.add_subparsers(dest="action", required=True)
    for name in ("plan", "run"):
        command = drills.add_parser(name)
        command.add_argument("--group", required=True)
        command.add_argument("--duration", default="30m")
    drills.add_parser("revert")
    drills.add_parser("report")
    monitor = commands.add_parser(
        "watch", help="Probe service URLs without relying on Microsoft-hosted monitoring"
    )
    monitor.add_argument(
        "--url", action="append", required=True, help="HTTPS endpoint to probe (repeatable)"
    )
    monitor.add_argument(
        "--interval", type=float, default=60, help="Seconds between checks (default: 60)"
    )
    monitor.add_argument(
        "--failures", type=int, default=3, help="Consecutive failures before an alert"
    )
    monitor.add_argument(
        "--recoveries", type=int, default=2, help="Consecutive successes before recovery"
    )
    monitor.add_argument(
        "--once", action="store_true", help="Check once and exit; nonzero if any probe fails"
    )
    monitor.add_argument(
        "--sms", action="store_true", help="Enable paid SMS through configured Twilio credentials"
    )
    return root


def _interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def _show_map(ui: UI, result: MapReport, interactive: bool) -> None:
    output = result.plain()
    write_private(
        Path("blast-radius.md"), "# Tenant dependencies\n\n```text\n" + output + "\n```\n"
    )
    ui.show_map(result)
    ui.message("\nReport written to blast-radius.md")
    if ui.interactive and interactive:
        ui.explore(result)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    ui = UI()
    graph: Graph | None = None
    previous_handler = None
    try:
        if args.command == "watch":
            return watch(
                args.url,
                state_dir(),
                interval=args.interval,
                failures=args.failures,
                recoveries=args.recoveries,
                once=args.once,
                sms=args.sms,
                announce=ui.message,
            )
        if args.command == "map" and args.exports_only:
            if not args.flows:
                raise HavocError("--exports-only requires at least one --flows JSON/ZIP export.")
            result = add_workflows(MapReport("Local workflow exports", {}, ()), args.flows)
            _show_map(ui, result, not args.no_interactive)
            return 0
        directory = state_dir()
        auth = Auth(directory)
        if args.command == "login":
            auth.login(args.tenant, args.client_id, ui.message)
            ui.message(f"Signed in to {auth.profile['tenant']}.")
            return 0
        drill = args.command == "drill"
        token = auth.token(drill)
        first_token = [token]

        def access_token() -> str:
            return first_token.pop() if first_token else auth.token(drill)

        graph = Graph(access_token)
        if args.command == "map":
            with ui.working("Reading tenant dependencies (Graph + DNS)..."):
                result = map_tenant(graph, auth.profile["tenant"])
                if args.flows:
                    result = add_workflows(result, args.flows)
            _show_map(ui, result, not args.no_interactive)
        elif args.action in {"plan", "run"}:
            with ui.working("Checking pilot membership and admin roles..."):
                pilot = plan(graph, args.group, args.duration, auth.profile["oid"])
            ui.show_pilot(pilot)
            if args.action == "run":
                ui.message(
                    "This blocks real access and revokes sessions. Tell the volunteers first."
                )
                try:
                    confirmation = input(f"Type the group name ({pilot.name}) to start: ")
                except EOFError as error:
                    raise HavocError("No confirmation received. No drill was started.") from error
                if confirmation != pilot.name:
                    raise HavocError("Group name did not match. No drill was started.")
                current = plan(graph, args.group, args.duration, auth.profile["oid"])
                if current != pilot:
                    raise HavocError("The pilot changed during confirmation. Run drill plan again.")
                previous_handler = signal.signal(signal.SIGTERM, _interrupt)
                run(graph, pilot, directory, auth.profile["tid"], announce=ui.message)
        elif args.action == "revert":
            with ui.working("Finding and removing drill policies..."):
                removed = revert(graph)
            for name in removed:
                ui.message(f"Removed {name}.")
            if not removed:
                ui.message("No havoc365 drill policies found in this tenant.")
        elif args.action == "report":
            with ui.working("Reading sign-in attempts..."):
                result_text = report(graph, directory, auth.profile["tid"])
            ui.show_report(result_text)
        return 0
    except (HavocError, OSError) as error:
        ui.error(str(error))
        return 1
    except KeyboardInterrupt:
        print("Interrupted. If cleanup did not finish, run havoc365 drill revert.", file=sys.stderr)
        return 130
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)
        if graph is not None:
            graph.close()
