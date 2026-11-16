import argparse
import signal
import sys
from pathlib import Path

from havoc365.auth import Auth
from havoc365.drills import plan, report, revert, run
from havoc365.errors import HavocError
from havoc365.graph import Graph
from havoc365.mapping import map_tenant
from havoc365.storage import state_dir, write_private


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="havoc365", description="Downtime by appointment.")
    root.add_argument("--version", action="version", version="havoc365 0.1.0.dev0")
    commands = root.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Sign in with a device code")
    login.add_argument("--tenant", required=True)
    login.add_argument("--client-id", required=True)
    commands.add_parser("map", help="Map tenant dependencies")
    drill = commands.add_parser("drill", help="Plan, run, revert or report an outage drill")
    drills = drill.add_subparsers(dest="action", required=True)
    for name in ("plan", "run"):
        command = drills.add_parser(name)
        command.add_argument("--group", required=True)
        command.add_argument("--duration", default="30m")
    drills.add_parser("revert")
    drills.add_parser("report")
    return root


def _interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    graph: Graph | None = None
    previous_handler = None
    try:
        directory = state_dir()
        auth = Auth(directory)
        if args.command == "login":
            auth.login(args.tenant, args.client_id, print)
            print(f"Signed in to {auth.profile['tenant']}.")
            return 0
        drill = args.command == "drill"
        token = auth.token(drill)
        first_token = [token]

        def access_token() -> str:
            return first_token.pop() if first_token else auth.token(drill)

        graph = Graph(access_token)
        if args.command == "map":
            output = map_tenant(graph, auth.profile["tenant"])
            write_private(
                Path("blast-radius.md"), "# Tenant dependencies\n\n```text\n" + output + "\n```\n"
            )
            print(output)
            print("\nReport written to blast-radius.md")
        elif args.action in {"plan", "run"}:
            pilot = plan(graph, args.group, args.duration, auth.profile["oid"])
            print(pilot.describe())
            if args.action == "run":
                print("This blocks real access and revokes sessions. Tell the volunteers first.")
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
                run(graph, pilot, directory, auth.profile["tid"])
        elif args.action == "revert":
            removed = revert(graph)
            for name in removed:
                print(f"Removed {name}.")
            if not removed:
                print("No havoc365 drill policies found in this tenant.")
        elif args.action == "report":
            print(report(graph, directory, auth.profile["tid"]))
        return 0
    except (HavocError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. If cleanup did not finish, run havoc365 drill revert.", file=sys.stderr)
        return 130
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)
        if graph is not None:
            graph.close()
