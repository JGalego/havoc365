import hashlib
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Self
from urllib.parse import quote, urlsplit

import httpx

from havoc365.errors import HavocError
from havoc365.lifeboat import set_active, validate_url
from havoc365.storage import read_json, write_json


def positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise HavocError("Use a positive number of seconds.") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise HavocError("Use a positive, finite number of seconds.")
    return seconds


def target_label(url: str) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise HavocError(
            "Watch targets must be HTTPS URLs without embedded credentials or fragments."
        )
    return parts.hostname + (parts.path or "/")


@dataclass(frozen=True)
class Probe:
    target: str
    available: bool
    detail: str


def probe(client: httpx.Client, urls: list[str]) -> list[Probe]:
    results = []
    for url in urls:
        label = target_label(url)
        try:
            response = client.get(url, follow_redirects=False)
            results.append(
                Probe(label, 200 <= response.status_code < 400, f"HTTP {response.status_code}")
            )
        except httpx.TransportError as error:
            results.append(Probe(label, False, type(error).__name__))
    return results


@dataclass(frozen=True, repr=False)
class SMS:
    account: str
    token: str
    sender: str
    recipient: str

    @classmethod
    def from_env(cls, recipient: str | None = None) -> Self:
        names = (
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_FROM_NUMBER",
            "TWILIO_TO_NUMBER",
        )
        values = [os.environ.get(name, "") for name in names]
        if recipient is not None:
            values[-1] = recipient
        missing = [name for name, value in zip(names, values, strict=True) if not value]
        if missing:
            raise HavocError("SMS requires environment variables: " + ", ".join(missing))
        account, token, sender, recipient = values
        if not re.fullmatch(r"AC[0-9a-fA-F]{32}", account):
            raise HavocError("TWILIO_ACCOUNT_SID must be your Twilio account SID.")
        if not all(re.fullmatch(r"\+[1-9]\d{6,14}", number) for number in (sender, recipient)):
            raise HavocError(
                "Twilio sender and recipient must be E.164 numbers, such as +15551234567."
            )
        return cls(account, token, sender, recipient)

    def send(
        self, client: httpx.Client, message: str, *, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account}/Messages.json"
        for attempt in range(4):
            try:
                response = client.post(
                    url,
                    auth=httpx.BasicAuth(self.account, self.token),
                    data={"From": self.sender, "To": self.recipient, "Body": message},
                )
            except httpx.TransportError as error:
                raise HavocError(
                    "SMS submission had an uncertain result. "
                    "Check Twilio message logs before retrying."
                ) from error
            if response.status_code == 201:
                return
            if response.status_code == 429 and attempt < 3:
                delay = response.headers.get("Retry-After", str(2**attempt))
                try:
                    seconds = float(delay)
                except ValueError as error:
                    raise HavocError("Twilio returned an invalid Retry-After value.") from error
                if not math.isfinite(seconds) or seconds < 0:
                    raise HavocError("Twilio returned an invalid Retry-After value.")
                sleep(seconds)
                continue
            raise HavocError(
                f"SMS provider returned HTTP {response.status_code}. Check Twilio credentials, "
                "billing and message logs. Requests are not replayed after ambiguous server errors."
            )


def invite_roster(path: Path) -> list[dict[str, str]]:
    people = read_json(
        path, recovery="Create or repair the private invite roster before starting the watcher."
    ).get("people")
    if not isinstance(people, list) or not 1 <= len(people) <= 50:
        raise HavocError("Invite roster must contain people: a list of 1-50 name/phone objects.")
    recipients = []
    seen = set()
    for person in people:
        if (
            not isinstance(person, dict)
            or set(person) != {"name", "phone"}
            or not isinstance(person["name"], str)
            or not 1 <= len(person["name"].strip()) <= 80
            or any(ord(char) < 32 for char in person["name"])
            or not isinstance(person["phone"], str)
            or not re.fullmatch(r"\+[1-9]\d{6,14}", person["phone"])
            or person["phone"] in seen
        ):
            raise HavocError(
                "Each invite recipient needs a name (1-80 characters) and a unique E.164 phone."
            )
        seen.add(person["phone"])
        recipients.append({"name": person["name"].strip(), "phone": person["phone"]})
    return recipients


def _invitation(client: httpx.Client, url: str, admin: str, name: str) -> str:
    try:
        response = client.post(
            url + "/api/admin/invites",
            headers={"Authorization": f"Bearer {admin}"},
            json={"name": name, "role": "member", "hours": 24},
            follow_redirects=False,
        )
    except httpx.TransportError as error:
        raise HavocError(
            "Invitation creation was not confirmed. Check Lifeboat administration before retrying."
        ) from error
    if response.status_code != 201:
        raise HavocError(
            f"Lifeboat invitation returned HTTP {response.status_code}. Check its admin access "
            "and invitation limits before retrying."
        )
    try:
        value = response.json()
    except ValueError as error:
        raise HavocError(
            "Lifeboat returned an invalid invitation response. Check its URL."
        ) from error
    credential = value.get("credential") if isinstance(value, dict) else None
    if not isinstance(credential, str) or not 32 <= len(credential) <= 512:
        raise HavocError(
            "Lifeboat returned no valid invitation credential. Check its server version."
        )
    return url + "/#invite=" + quote(credential, safe="")


class Watcher:
    def __init__(
        self,
        urls: list[str],
        directory: Path,
        *,
        failures: int = 3,
        recoveries: int = 2,
    ) -> None:
        if not urls or failures < 1 or recoveries < 1:
            raise HavocError("Supply at least one URL and positive failure/recovery thresholds.")
        for url in urls:
            target_label(url)
        self.urls = list(dict.fromkeys(urls))
        identity = "\n".join(sorted(self.urls)).encode()
        self.path = directory / "watch" / f"{hashlib.sha256(identity).hexdigest()[:16]}.json"
        self.failures = failures
        self.recoveries = recoveries
        self.state = (
            read_json(self.path)
            if self.path.exists()
            else {"down": False, "failures": 0, "recoveries": 0, "notification": "clear"}
        )
        if (
            not isinstance(self.state.get("down"), bool)
            or any(
                not isinstance(self.state.get(key), int) or self.state[key] < 0
                for key in ("failures", "recoveries")
            )
            or self.state.get("notification") not in {"clear", "pending"}
        ):
            raise HavocError(f"Invalid watch state in {self.path}. Restore it before monitoring.")

    def tick(
        self,
        client: httpx.Client,
        notify: Callable[[bool], None] | None = None,
    ) -> list[Probe]:
        if self.state.get("notification") != "clear":
            raise HavocError(
                f"A previous alert may have been submitted. Check provider logs, then remove "
                f"only {self.path} to resume. Automatic replay is disabled to avoid duplicate SMS."
            )
        results = probe(client, self.urls)
        available = all(result.available for result in results)
        self.state["failures"] = 0 if available else self.state["failures"] + 1
        self.state["recoveries"] = self.state["recoveries"] + 1 if available else 0
        down = self.state["down"]
        transition = (not down and self.state["failures"] >= self.failures) or (
            down and self.state["recoveries"] >= self.recoveries
        )
        if transition:
            self.state["down"] = not down
            if notify:
                self.state["notification"] = "pending"
            write_json(self.path, self.state)
            if notify:
                notify(not down)
                self.state["notification"] = "clear"
        write_json(self.path, self.state)
        return results


def watch(
    urls: list[str],
    directory: Path,
    *,
    interval: float = 60,
    failures: int = 3,
    recoveries: int = 2,
    once: bool = False,
    sms: bool = False,
    lifeboat_url: str | None = None,
    invite_roster_path: Path | None = None,
    announce: Callable[[str], None] = print,
) -> int:
    interval = positive_seconds(str(interval))
    watcher = Watcher(urls, directory, failures=failures, recoveries=recoveries)
    if invite_roster_path is not None and (not sms or not lifeboat_url):
        raise HavocError("--invite-roster requires --sms and --lifeboat-url; SMS is paid.")
    recipients = invite_roster(invite_roster_path) if invite_roster_path is not None else []
    provider = SMS.from_env(recipients[0]["phone"] if recipients else None) if sms else None
    admin = os.environ.get("HAVOC365_LIFEBOAT_ADMIN_TOKEN", "")
    if lifeboat_url:
        lifeboat_url = validate_url(lifeboat_url)
        if len(admin) < 32:
            raise HavocError("Set HAVOC365_LIFEBOAT_ADMIN_TOKEN before using --lifeboat-url.")
    with httpx.Client(timeout=10) as client:

        def notify(down: bool) -> None:
            if provider:
                if lifeboat_url:
                    try:
                        set_active(client, lifeboat_url, admin, down, source="watcher")
                    except HavocError as error:
                        announce(f"Lifeboat update failed; still attempting SMS: {error}")
                label = "OUTAGE" if down else "RECOVERY"
                targets = ", ".join(target_label(url) for url in urls)
                if recipients and lifeboat_url is not None:
                    for person in recipients:
                        destination = (
                            _invitation(client, lifeboat_url, admin, person["name"])
                            if down
                            else lifeboat_url
                        )
                        message = f"havoc365 {label}: {targets[:500]}\nLifeboat: {destination}"
                        replace(provider, recipient=person["phone"]).send(client, message)
                else:
                    destination = f"\nLifeboat: {lifeboat_url}" if lifeboat_url else ""
                    provider.send(client, f"havoc365 {label}: {targets[:500]}{destination}")
                announce(f"{label} SMS accepted by Twilio; delivery is not yet confirmed.")

        while True:
            try:
                results = watcher.tick(client, notify if provider else None)
            except HavocError as error:
                if lifeboat_url:
                    try:
                        set_active(
                            client, lifeboat_url, admin, watcher.state["down"], source="watcher"
                        )
                    except HavocError as activation_error:
                        raise HavocError(
                            f"{error} Lifeboat update also failed: {activation_error}"
                        ) from error
                raise
            if lifeboat_url:
                set_active(client, lifeboat_url, admin, watcher.state["down"], source="watcher")
            for result in results:
                announce(
                    f"{'UP' if result.available else 'DOWN'}  {result.target}  {result.detail}"
                )
            announce(f"Alert state: {'outage' if watcher.state['down'] else 'normal'}")
            if once:
                return 0 if all(result.available for result in results) else 1
            time.sleep(interval)
