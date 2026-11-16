import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from havoc365.errors import GraphError, HavocError
from havoc365.storage import JsonObject

BASE = "https://graph.microsoft.com/v1.0"
NO_PREMIUM = "Authentication_RequestFromNonPremiumTenantOrB2CTenant"


class Graph:
    def __init__(
        self,
        token: Callable[[], str],
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.token = token
        self.client = client if client is not None else httpx.Client(timeout=30)
        self.sleep = sleep

    def close(self) -> None:
        self.client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: JsonObject | None = None,
        retry_safe: bool = True,
    ) -> JsonObject:
        url = path if path.startswith("https://") else BASE + "/" + path.lstrip("/")
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != "graph.microsoft.com":
            raise HavocError("Refusing to send credentials outside Microsoft Graph.")
        if not parts.path.startswith("/v1.0/") or parts.fragment:
            raise HavocError("Microsoft Graph returned an unexpected paging URL.")
        for attempt in range(4):
            try:
                response = self.client.request(
                    method,
                    url,
                    params=params,
                    json=body,
                    headers={"Authorization": f"Bearer {self.token()}"},
                )
            except (httpx.TimeoutException, httpx.TransportError) as error:
                if not retry_safe or attempt == 3:
                    raise HavocError(
                        f"Microsoft Graph {method} {parts.path} timed out or lost its connection. "
                        "Check connectivity and retry; use havoc365 drill revert after a drill."
                    ) from error
                self.sleep(2**attempt)
                continue
            if attempt < 3 and (
                response.status_code == 429 or (retry_safe and 500 <= response.status_code < 600)
            ):
                delay = response.headers.get("Retry-After")
                try:
                    seconds = max(0.0, float(delay)) if delay is not None else float(2**attempt)
                except ValueError:
                    try:
                        seconds = max(
                            0.0,
                            (parsedate_to_datetime(delay) - datetime.now(UTC)).total_seconds(),
                        )
                    except (TypeError, ValueError, OverflowError) as error:
                        raise HavocError(
                            "Graph returned an invalid Retry-After header. Retry later."
                        ) from error
                self.sleep(seconds)
                continue
            if response.status_code == 204 or (method == "DELETE" and response.status_code == 404):
                return {}
            try:
                data = response.json()
            except ValueError as error:
                raise HavocError(
                    f"Graph {method} {parts.path} returned HTTP {response.status_code} "
                    "without valid JSON. Retry later."
                ) from error
            if not isinstance(data, dict):
                raise HavocError(f"Graph {parts.path} returned an invalid response object.")
            if not response.is_success:
                detail = data.get("error", {})
                raise GraphError(
                    response.status_code,
                    str(detail.get("code", "unknown")),
                    str(detail.get("message", "request failed")).rstrip("."),
                )
            return data
        raise HavocError("Graph retry budget exhausted. Retry later.")

    def collection(
        self, path: str, *, params: dict[str, str] | None = None
    ) -> Iterator[JsonObject]:
        seen: set[str] = set()
        while path:
            if path in seen:
                raise HavocError(
                    "Graph repeated a paging URL. Results are incomplete; retry later."
                )
            seen.add(path)
            page = self.request("GET", path, params=params)
            values = page.get("value")
            if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
                raise HavocError(f"Graph {path} returned an invalid collection.")
            yield from values
            path = page.get("@odata.nextLink", "")
            if not isinstance(path, str):
                raise HavocError("Graph returned an invalid paging URL.")
            params = None
