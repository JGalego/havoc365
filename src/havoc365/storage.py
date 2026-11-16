import json
import os
import tempfile
from pathlib import Path
from typing import Any

from havoc365.errors import HavocError

type JsonObject = dict[str, Any]


def state_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    path = base / "havoc365"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path: Path, value: JsonObject) -> None:
    write_private(path, json.dumps(value, indent=2) + "\n")


def read_json(path: Path) -> JsonObject:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise HavocError(
            f"Cannot read {path}: {error}. Restore the file or log in again."
        ) from error
    if not isinstance(value, dict):
        raise HavocError(f"{path} must contain a JSON object. Restore it or log in again.")
    return value
