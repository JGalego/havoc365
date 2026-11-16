import httpx
import pytest

from havoc365.cli import main
from havoc365.errors import HavocError
from havoc365.storage import write_json
from havoc365.watch import SMS, Watcher, positive_seconds, probe


def test_watch_debounces_and_notifies_once_per_transition(tmp_path):
    status = [503]
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status[0])))
    watcher = Watcher(["https://outlook.office.com/"], tmp_path, failures=2, recoveries=2)
    alerts = []
    watcher.tick(client, alerts.append)
    assert alerts == []
    watcher.tick(client, alerts.append)
    watcher.tick(client, alerts.append)
    assert alerts == [True]
    restarted = Watcher(watcher.urls, tmp_path, failures=2, recoveries=2)
    status[0] = 200
    restarted.tick(client, alerts.append)
    restarted.tick(client, alerts.append)
    assert alerts == [True, False]


def test_probe_handles_redirects_without_following_and_does_not_log_query():
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://example.org/"})

    client = httpx.Client(transport=httpx.MockTransport(response))
    result = probe(client, ["https://outlook.office.com/owa?sig=private"])[0]
    assert result.available
    assert "private" not in result.target
    assert len(requests) == 1


def test_timeout_is_an_explicit_failed_probe():
    def timeout(request):
        raise httpx.ReadTimeout("injected", request=request)

    result = probe(httpx.Client(transport=httpx.MockTransport(timeout)), ["https://example.org/"])[
        0
    ]
    assert not result.available
    assert result.detail == "ReadTimeout"


def test_failed_alert_is_not_replayed_after_restart(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    watcher = Watcher(["https://example.org/"], tmp_path, failures=1)

    def uncertain(_):
        raise HavocError("uncertain submission")

    with pytest.raises(HavocError, match="uncertain"):
        watcher.tick(client, uncertain)
    restarted = Watcher(watcher.urls, tmp_path, failures=1)
    with pytest.raises(HavocError, match="Automatic replay is disabled"):
        restarted.tick(client, lambda _: pytest.fail("Must not replay"))


def test_sms_is_opt_in_and_requires_explicit_credentials(monkeypatch, capsys):
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    assert main(["watch", "--url", "https://example.org", "--sms", "--once"]) == 1
    assert "TWILIO_ACCOUNT_SID" in capsys.readouterr().err


def test_sms_sends_form_without_replaying_server_failure():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503)

    provider = SMS("AC" + "0" * 32, "offline-test-token", "+15551234567", "+15557654321")
    with pytest.raises(HavocError, match="not replayed"):
        provider.send(httpx.Client(transport=httpx.MockTransport(handler)), "outage")
    assert len(requests) == 1
    assert b"Body=outage" in requests[0].content
    assert requests[0].url.host == "api.twilio.com"


def test_sms_throttle_honours_retry_after():
    calls = []
    sleeps = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "10"})
            if len(calls) == 1
            else httpx.Response(201)
        )

    SMS("AC" + "0" * 32, "offline-token", "+15551234567", "+15557654321").send(
        httpx.Client(transport=httpx.MockTransport(handler)), "outage", sleep=sleeps.append
    )
    assert sleeps == [10]


@pytest.mark.parametrize("seconds", ["0", "-1", "nan", "inf", "tomorrow"])
def test_invalid_interval_never_reaches_network(seconds):
    with pytest.raises(HavocError, match="positive"):
        positive_seconds(seconds)


def test_watch_refuses_cleartext_or_embedded_credentials(tmp_path):
    for url in ("http://example.org", "https://user:password@example.org"):
        with pytest.raises(HavocError, match="HTTPS"):
            Watcher([url], tmp_path)


def test_corrupt_watch_state_fails_with_recovery_instructions(tmp_path):
    watcher = Watcher(["https://example.org"], tmp_path)
    write_json(watcher.path, {"down": True, "failures": "bad"})
    with pytest.raises(HavocError, match="Restore it"):
        Watcher(watcher.urls, tmp_path)


def test_watch_once_cli_is_credential_free(monkeypatch, capsys):
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    monkeypatch.setattr("havoc365.watch.httpx.Client", lambda **kwargs: client)
    assert main(["watch", "--url", "https://example.org", "--once"]) == 0
    assert "UP  example.org/" in capsys.readouterr().out
