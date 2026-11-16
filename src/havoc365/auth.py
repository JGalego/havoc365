import re
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import msal
import requests

from havoc365.errors import HavocError
from havoc365.storage import JsonObject, read_json, state_dir, write_json, write_private

MAP_SCOPES = (
    "AuditLog.Read.All",
    "Application.Read.All",
    "Domain.Read.All",
    "Files.Read.All",
)
DRILL_SCOPES = (
    *MAP_SCOPES,
    "GroupMember.Read.All",
    "RoleManagement.Read.Directory",
    "Policy.Read.All",
    "Policy.ReadWrite.ConditionalAccess",
    "User.RevokeSessions.All",
)


class Auth:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory if directory is not None else state_dir()
        self.profile: JsonObject = {}
        self.cache = msal.SerializableTokenCache()
        self.app: msal.PublicClientApplication | None = None

    def _open(self, profile: JsonObject) -> None:
        tenant = profile.get("tenant", "")
        client_id = profile.get("client_id", "")
        if not isinstance(tenant, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", tenant):
            raise HavocError("Invalid tenant. Use your tenant UUID or verified domain.")
        if tenant.lower() in {"common", "organizations", "consumers"}:
            raise HavocError(
                "Use a specific tenant UUID or verified domain, not a shared authority."
            )
        try:
            UUID(client_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise HavocError(
                "Invalid client ID. Use the Application (client) ID from Entra."
            ) from error
        self.profile = profile
        cache_path = self.directory / "tokens.json"
        if cache_path.exists():
            try:
                self.cache.deserialize(cache_path.read_text())
            except (OSError, ValueError) as error:
                raise HavocError(f"Cannot read token cache {cache_path}. Log in again.") from error
        try:
            self.app = msal.PublicClientApplication(
                client_id,
                authority=f"https://login.microsoftonline.com/{tenant}",
                token_cache=self.cache,
                timeout=30,
            )
        except ValueError as error:
            raise HavocError(
                "Microsoft rejected the sign-in configuration. "
                "Check your tenant and Application (client) ID, then run havoc365 login again."
            ) from error

    def _save(self) -> None:
        write_private(self.directory / "tokens.json", self.cache.serialize())
        write_json(self.directory / "profile.json", self.profile)

    def _acquire(self, scopes: tuple[str, ...], announce: Callable[[str], None]) -> str:
        if self.app is None:
            raise HavocError("No sign-in session. Run havoc365 login first.")
        accounts = self.app.get_accounts()
        account = next(
            (
                entry
                for entry in accounts
                if entry.get("local_account_id") == self.profile.get("oid")
            ),
            None,
        )
        result = self.app.acquire_token_silent(list(scopes), account=account) if account else None
        if not result:
            flow = self.app.initiate_device_flow(scopes=list(scopes))
            if "user_code" not in flow:
                raise HavocError(
                    "Cannot start device sign-in: "
                    f"{flow.get('error_description', flow.get('error', 'no device code'))}. "
                    "Check the tenant, client ID and public client flows setting."
                )
            announce(flow["message"])
            result = self.app.acquire_token_by_device_flow(flow)
        if not result or "access_token" not in result:
            detail = (result or {}).get("error_description", "device sign-in did not finish")
            raise HavocError(
                f"Sign-in failed: {detail}. Check consent and run havoc365 login again."
            )
        claims = result.get("id_token_claims", {})
        oid = claims.get("oid", self.profile.get("oid"))
        tid = claims.get("tid", self.profile.get("tid"))
        if not oid or not tid:
            raise HavocError("Sign-in returned no tenant/user identity. Use an Entra work account.")
        self.profile.update(oid=str(oid), tid=str(tid))
        self._save()
        return result["access_token"]

    def login(self, tenant: str, client_id: str, announce: Callable[[str], None]) -> None:
        try:
            self._open({"tenant": tenant, "client_id": client_id})
            self._acquire(MAP_SCOPES, announce)
        except requests.RequestException as error:
            raise HavocError(
                "Microsoft sign-in is unavailable. Check connectivity and retry."
            ) from error

    def token(self, drill: bool = False, announce: Callable[[str], None] = print) -> str:
        try:
            if self.app is None:
                path = self.directory / "profile.json"
                if not path.exists():
                    raise HavocError(
                        "Not signed in. Run havoc365 login --tenant <tenant> --client-id <app-id>."
                    )
                self._open(read_json(path))
            return self._acquire(DRILL_SCOPES if drill else MAP_SCOPES, announce)
        except requests.RequestException as error:
            raise HavocError(
                "Microsoft sign-in is unavailable. Check connectivity and retry."
            ) from error
