<h1 align="center">
  <picture>
    <source media="(prefers-reduced-motion: reduce)" srcset="assets/logo.png">
    <img src="assets/logo.gif" alt="" width="220">
  </picture>
  <br>
  havoc365
</h1>

<p align="center"><strong>Downtime by appointment.</strong></p>

<p align="center">
  <img alt="Status: pre-alpha" src="https://img.shields.io/badge/status-pre--alpha-orange">
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green"></a>
  <br>
  <img alt="Chaos: scheduled" src="https://img.shields.io/badge/chaos-scheduled-7C3AED">
  <img alt="3 a.m. pages: 0" src="https://img.shields.io/badge/3%20a.m.%20pages-0-DB2777">
  <img alt="Sleep: restored" src="https://img.shields.io/badge/sleep-restored-00B4F0">
</p>

When Microsoft 365 went down on 12 November 2026, people lost access to email, Teams and SharePoint. By the next morning, delayed messages were still arriving. Microsoft had [extended the mail delivery timeout to 48 hours](https://www.bleepingcomputer.com/news/microsoft/massive-microsoft-365-outage-disrupts-outlook-teams-copilot-worldwide/) while it worked through the backlog. Admins trying to find out what was wrong [couldn't reach Intune or the Microsoft 365 Apps Admin Center either](https://www.theregister.com/saas/2026/11/12/microsoft-struggling-to-fix-massive-cloud-outage-thats-taken-down-email-and-intune/5309531).

If your monitoring sends alerts through Exchange, those alerts may be waiting in the same queue. If the incident runbook lives in SharePoint, you may not be able to open it. You need to know about those dependencies before you're trying to work around them during an outage.

havoc365 maps these dependencies in your tenant and runs outage drills with a small group of volunteers.

<picture>
  <source media="(prefers-reduced-motion: reduce)" srcset="assets/map.png">
  <img src="assets/map.gif" alt="Offline map demo: inspect SharePoint dependencies and coverage limits." width="900">
</picture>

*Offline demo using Microsoft Learn examples, not a live tenant. Domains are omitted; no DNS queries are made.*

## Getting Started

You need [uv](https://docs.astral.sh/uv/) and a Microsoft 365 account with the Global Reader role. Drills require the additional roles listed under [Permissions](#permissions).

1. Install havoc365:

   ```sh
   uv tool install git+https://github.com/JGalego/havoc365
   ```

2. [Register an app](https://learn.microsoft.com/entra/identity-platform/quickstart-register-app) in Microsoft Entra. Allow public client flows, add the `map` permissions listed under [Permissions](#permissions), and grant admin consent.

3. Sign in and map your tenant:

   ```sh
   havoc365 login --tenant contoso.onmicrosoft.com --client-id <app-id>
   havoc365 map
   ```

   ```text
   contoso.onmicrosoft.com

   If Exchange Online goes down
     14 identities used SMTP AUTH   alerts@contoso.com, monitoring-prod@contoso.com, scanner@contoso.com, +11
     6 apps request Microsoft Graph Mail.Send   HR Portal, Notifications, Ticketing, +3
     contoso.com has an MX route straight to Exchange   contoso-com.mail.protection.outlook.com

   If SharePoint goes down
     9 runbook search matches in SharePoint   Major Incident.docx, Outage Runbook.docx, Runbook.docx, +6

   If Entra ID sign-in goes down
     47 enabled enterprise apps use Entra ID   Payroll, VPN, Wiki, +44

   Limits and skipped checks
     - SMTP observations cover the last 7 days, not every sender or device.
     - Mail.Send entries are requested permissions, not proof of consent or recent use.
     - File search covers only files this account can read; matches need manual review.
     - No matches does not prove that a dependency is absent.

   Report written to blast-radius.md
   ```

The report uses SMTP AUTH sign-in logs, app registrations, enterprise apps, domain MX records, and a file search for `runbook`, `major incident` or `incident response`. It cannot identify every device behind a shared mail account or runbooks that don't match those terms. It doesn't count tenant users; that would need another Graph permission.

In a terminal, `map` shows a table and lets you inspect each service's full findings with `1`, `2` or `3`. Press `l` for coverage limits or `q` to leave. Browsing the results makes no further network requests. Use `havoc365 map --no-interactive` to skip the explorer. Redirected output stays plain text; the Markdown report is saved before the explorer opens.

Drill previews show the pilot roster as a table. Mapping, pilot checks, revert and report show progress while waiting for Graph. Terminal colours follow `NO_COLOR` when set.

### Teams workflows and Power Automate

Export the flows you want to check as JSON or a ZIP package, then include them in the map:

```sh
havoc365 map --flows outage-notifications.zip
havoc365 map --exports-only --flows definition.json --no-interactive
```

`--flows` can be repeated. The export reader finds Outlook, SharePoint and Teams connector references, including nested workflow actions, and legacy Teams incoming-webhook URLs. It reports workflow names, never webhook URLs or connection credentials. `--exports-only` needs no login and makes no network calls.

This is export analysis, not automatic tenant-wide flow discovery. [Teams Workflows replaced classic connectors](https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/what-are-webhooks-and-connectors), and Graph doesn't expose a complete webhook inventory. Export the relevant workflows from Power Automate and refresh the exports when they change. Treat exports as credentials and keep them outside this repository.

## Commands

| Command | What it does | Changes your tenant |
|---|---|---|
| `havoc365 login` | Signs in with a device code | No |
| `havoc365 map` | Reports dependencies on Exchange Online, SharePoint and Entra ID | No |
| `havoc365 drill plan` | Shows who a drill would lock out, and for how long | No |
| `havoc365 drill run` | Locks a pilot group out of Microsoft 365 until the timer ends | Adds one Conditional Access policy |
| `havoc365 drill revert` | Ends a drill early | Removes that policy |
| `havoc365 drill report` | Reports sign-in attempts during the drill | No |
| `havoc365 watch` | Probes configured HTTPS URLs and optionally sends outage/recovery SMS | No |
| `havoc365 lifeboat serve` | Hosts independent fallback chat and contacts | No |
| `havoc365 lifeboat activate` | Opens the lifeboat's chat | No |
| `havoc365 lifeboat standby` | Returns chat to standby; contacts stay available | No |

## Watching for outages

Run the watcher on a machine outside Microsoft's network. It doesn't need a tenant login:

```sh
havoc365 watch --url https://outlook.office.com/owa/ --interval 60
havoc365 watch --url https://outlook.office.com/owa/ --once
```

Repeat `--url` for more endpoints. HTTP 2xx/3xx responses count as reachable; redirects aren't followed. Timeouts and other status codes count as failures. This tests endpoint reachability, not email delivery or authenticated access. Choose URLs that represent your own service paths.

An outage starts after three consecutive failed checks; recovery needs two successful checks. Change these with `--failures` and `--recoveries`. State survives restarts. `--once` exits nonzero if any endpoint fails, regardless of the alert threshold.

For SMS, set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` and `TWILIO_TO_NUMBER` in the watcher's environment, then add `--sms`. Numbers must use E.164 format. SMS is paid and opt-in; without that flag the watcher only prints results. Each state change submits one message through the [Twilio Messages API](https://www.twilio.com/docs/messaging/api/message-resource). API acceptance doesn't guarantee delivery.

An interrupted or ambiguous SMS submission isn't automatically replayed. The error identifies the watch state file: check Twilio's message logs before removing that one file and restarting. Keep provider credentials out of shell history and version control.

## Lifeboat

Host the lifeboat on a machine outside Microsoft, with its own DNS and HTTPS. It has no Entra dependency and loads no third-party scripts, fonts or assets. Contacts are available on standby; the watcher opens chat during an outage and returns it to standby after recovery.

Generate two different random tokens, then start the server:

```sh
export HAVOC365_LIFEBOAT_TOKEN="$(uv run --no-project python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export HAVOC365_LIFEBOAT_ADMIN_TOKEN="$(uv run --no-project python -c 'import secrets; print(secrets.token_urlsafe(32))')"
havoc365 lifeboat serve --port 8080
```

```text
Lifeboat listening at http://127.0.0.1:8080. Expose it only behind an HTTPS reverse proxy.
Room starts in its saved state. Names are self-reported; share the member token out of band.
```

Open **http://127.0.0.1:8080** on the machine running the server and enter the member token. From this checkout, use `uv run havoc365` in place of `havoc365`. If you're connected over SSH or VS Code Remote, forward port 8080 to your own machine; the remote host's loopback address isn't your laptop's.

To try chat locally, keep the server running and use another terminal with the **same administrator token** in `HAVOC365_LIFEBOAT_ADMIN_TOKEN`:

```sh
havoc365 lifeboat activate --url http://127.0.0.1:8080
havoc365 lifeboat standby --url http://127.0.0.1:8080
```

Activation commands allow HTTP only for numeric loopback addresses (`127.0.0.1` and `[::1]`). Remote lifeboats require HTTPS.

Keep these tokens in your host's secret store so restarts use the same values. Share only the member token with participants, through a channel that doesn't depend on Microsoft. Give the administrator token to the watcher. Both must be at least 32 characters; names in chat are self-reported, not verified identities.

Contacts are optional. Installation doesn't create a contacts file. To import contacts, save a JSON file outside this checkout, then restart with `--contacts` pointing to that real file. For example, create `$HOME/.config/havoc365/contacts.json` with your own contact details:

```json
{"contacts": [{"name": "Incident desk", "contact": "Your independent contact number"}]}
```

```sh
havoc365 lifeboat serve --contacts "$HOME/.config/havoc365/contacts.json" --port 8080
```

Omitting `--contacts` keeps any previously imported contacts. The file is local configuration, not a tenant export.

The room shows its state separately from its connection: **Standby**, **Chat open**, or **Reconnecting**. An open room isn't proof of a Microsoft outage; it can also be a drill. On phones, switch between the **Chat** and **Contacts** tabs. Search contacts by name or contact detail and use the copy icon beside a contact to copy a number or address. Room information and token help expand when needed.

Press Ctrl+Enter (Cmd+Enter on macOS) to send a check-in. Incoming messages don't pull you away from older messages you're reading; **Jump to latest** brings you back. A failed send keeps the draft and its warning visible. Drafts also survive a connection failure or a switch to standby, but leaving or reloading clears them. There is no automatic message retry.

The server binds to `127.0.0.1` only. Put an HTTPS reverse proxy in front of it. For example, with [Caddy](https://caddyserver.com/docs/quick-starts/reverse-proxy):

```caddyfile
fallback.example.org {
    reverse_proxy 127.0.0.1:8080
}
```

Use your own domain and supervise the server and watcher with your host's service manager. Never expose the loopback HTTP port directly. Browser tokens stay in the tab's memory, not URLs or local storage. Reloading or leaving signs the participant out.

### Deployment recommendations

For a small team, use one always-on Linux VPS outside Microsoft, with a persistent local disk. Run the server as an unprivileged user, keep its tokens in a protected environment file, and use systemd or your provider's process supervisor to restart it on failure and boot. Install havoc365 on that host; running the command on your laptop doesn't deploy it elsewhere.

Point a domain such as `fallback.example.org` at the VPS, replace that example name in Caddy's configuration, and allow inbound HTTPS. Participants open **https://your-lifeboat-domain/** in any modern browser and enter the member token. They don't install havoc365. Keep DNS, domain recovery, hosting access and token distribution independent of Microsoft too; an Entra-only VPN or sign-in page in front of the room defeats the fallback.

For private administration over SSH, forward the loopback port instead of opening it publicly:

```sh
ssh -N -L 8080:127.0.0.1:8080 your-user@your-server
```

Then open http://127.0.0.1:8080 locally. In VS Code Remote, the **Ports** view provides the same local forwarding. Keep that forward private.

Avoid hosts that sleep when idle and deployments with ephemeral disks. SQLite needs persistent storage; use one server instance rather than independent replicas with different databases. Back up the database with SQLite's backup API or while the service is stopped, not by copying a live database without its WAL. Test a restore and token rotation before relying on it.

The cheapest setup puts the watcher on the same VPS, but a failed VPS then takes out both chat and detection. A watcher on another independent host avoids that shared failure. Monitor both processes from outside that host. `GET /healthz` is an unauthenticated liveness check only; it doesn't prove Microsoft 365 or the room's storage is healthy. This repository doesn't provision hosting, DNS or TLS for you.

Point the watcher at the HTTPS address:

```sh
havoc365 watch --url https://outlook.office.com/owa/ --lifeboat-url https://fallback.example.org
havoc365 lifeboat activate --url https://fallback.example.org
havoc365 lifeboat standby --url https://fallback.example.org
```

The watcher synchronizes the room after every check, including after a restart. Its state takes precedence over manual activation while it is running. SMS and lifeboat updates are independent: a failed SMS still attempts activation. A failed lifeboat update stops the watcher with an error; supervise and alert on that process too.

Room state, imported contacts and the newest 1,000 messages survive restarts in an owner-only SQLite database under the havoc365 state directory. The browser shows the newest 200 messages. Chat is plain text, limited to 2,000 characters per message; keep secrets and sensitive incident data elsewhere. Anyone holding the shared member token can read the room, so rotate it when membership changes. This is a small incident room, not a replacement for a managed messaging service.

## Drills

A drill blocks a pilot group's access to Microsoft 365. It doesn't reproduce a failure inside Microsoft's services. Use volunteers and agree on a time beforehand.

```sh
havoc365 drill plan --group "Outage Drill" --duration 30m
havoc365 drill run  --group "Outage Drill" --duration 30m
```

- The drill creates one Conditional Access policy, named `havoc365-drill-<id>`, targeting the group's validated user roster and the Office 365 application suite. Later group joins don't expand the drill.
- It refuses empty groups, groups with more than 25 members, nested groups, dynamic or hidden-membership groups, and anyone with an active or eligible admin role, including roles inherited through groups. It also refuses to target the signed-in operator. If it can't complete those checks, it won't start.
- `drill run` shows the roster and requires you to type the group name before it creates the policy.
- It revokes the group's sessions at the start, though existing access tokens may continue to work until they expire.
- The process removes the policy when the timer runs out, you press Ctrl-C, or it receives SIGTERM. Removal can be delayed if Graph is unavailable. The policy has no server-side expiry: if the process is killed, the policy stays in place.
- Run `havoc365 drill revert` to remove drill policies, including from another machine signed in to the same tenant. It doesn't need the original machine's state. If removal fails after retries, the error names the remaining policy and explains how to remove it in Entra.
- `drill report` uses the initiating machine's saved drill window and roster. It shows sign-in attempts, not every app or document someone opened. Sign-in logs can arrive late.

Sign-in tokens and drill history are stored under `$XDG_STATE_HOME/havoc365`, or `~/.local/state/havoc365` by default. The token cache is not encrypted; the directory is owner-only and files are mode `0600`. Treat them as credentials. Drill recovery details are saved before policy creation, so a lost response doesn't erase the policy's name.

## Permissions

havoc365 uses delegated Microsoft Graph permissions. Access is limited by both the app's consented permissions and the signed-in account's privileges.

| Command | Microsoft Graph permissions | Entra roles |
|---|---|---|
| `map` | `AuditLog.Read.All` `Application.Read.All` `Domain.Read.All` `Files.Read.All` | Global Reader |
| `drill` | Those of `map`, plus `GroupMember.Read.All` `RoleManagement.Read.Directory` `Policy.Read.All` `Policy.ReadWrite.ConditionalAccess` `User.RevokeSessions.All` | Global Reader, Conditional Access Administrator, Helpdesk Administrator |

Reading sign-in logs requires Entra ID P1 or higher. Without it, `map` skips the checks that need those logs and reports which checks it skipped.

Drill commands request their additional scopes at sign-in if needed; grant admin consent for them before running a drill. Conditional Access requires Entra ID P1 for the targeted users. A forbidden response fails the command rather than being treated as an empty result. Only the [documented non-premium-tenant error](https://learn.microsoft.com/en-us/troubleshoot/entra/entra-id/users-groups-entra-apis/b2c-or-tenant-premium-license-sign-in-activities) skips the mapper's sign-in checks; that error can also occur when Graph hasn't cached licensing information.

### Testing with a real tenant

Use a dedicated non-production tenant. You don't need an Azure VM or a custom domain: run havoc365 on your own machine and use the tenant's `onmicrosoft.com` domain.

| Option | Cost for a short test | What to check |
|---|---|---|
| [Microsoft 365 E5 developer sandbox](https://learn.microsoft.com/en-us/office/developer-program/microsoft-365-developer-program-faq) | Free if you qualify; 25 test licenses | Sandbox eligibility is restricted, including qualifying Visual Studio subscriptions and partner/support programs. Joining the Developer Program alone doesn't qualify you. |
| [Microsoft 365 Business Premium trial](https://learn.microsoft.com/en-us/microsoft-365/commerce/try-or-buy-microsoft-365?view=o365-worldwide) | Free for 30 days if offered at signup | Includes Exchange, SharePoint and [Entra ID P1](https://learn.microsoft.com/en-us/entra/fundamentals/licensing). Select a plan with Teams if you want to test Teams too. Turn off recurring billing: trials otherwise convert to an annual subscription by default. |

For a complete drill test, also account for [PIM licensing](https://learn.microsoft.com/en-us/entra/id-governance/privileged-identity-management/pim-getting-started). havoc365 checks eligible administrator assignments before it allows a drill; PIM requires Entra ID P2 or ID Governance, not just the P1 needed for Conditional Access. An eligible E5 developer sandbox includes P2. With a Business Premium trial, check which P2/Governance trial or add-on is offered to that tenant before paying. Microsoft's [Entra Suite trial](https://learn.microsoft.com/en-us/entra/fundamentals/try-microsoft-entra-suite) includes ID Governance and requires a P1-containing base plan. Trial availability and commercial terms vary. The tool won't bypass a forbidden eligibility check.

Start with one licensed operator, one licensed ordinary pilot, and a separate emergency administrator outside the pilot. License each account for the features it uses. Register the app and consent to the documented scopes, create a static pilot group containing only the ordinary user, and put a harmless test runbook in SharePoint. Start with `login`, `map` and `drill plan`; run the existing read-only live check after sign-in. Only then deliberately run a short drill in that test tenant, verify the pilot is blocked, and verify both timed cleanup and `drill revert` from another process. Keep the emergency administrator available throughout. Don't weaken or skip the administrator exclusions to get a test through.

## Roadmap

- [ ] `map` for Exchange Online, SharePoint and Entra ID
- [ ] `drill`
- [ ] `map` for Teams webhooks and Power Automate flows
- [ ] `watch`: spots outages from outside Microsoft's network and pages you by SMS
- [ ] `lifeboat`: a standby chat and contact list, hosted outside Microsoft, that opens when Microsoft 365 goes down

## Documentation

Follow the [prose guidelines](WRITING.md) when editing this README or other project documentation.

## Development

```sh
uv sync
uv run playwright install --only-shell chromium
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest
```

[CI](.github/workflows/ci.yml) runs these checks on Python 3.12 and 3.13 for pushes and pull requests, builds the source distribution and wheel, and smoke-tests the installed CLI and packaged lifeboat assets. Ruff covers the CLI and tests, not the standalone logo renderer. CI uses the lockfile and doesn't run live tests or need tenant credentials. The Playwright tests run the actual Lifeboat UI against its in-process ASGI app, with browser requests intercepted and third-party requests rejected. On Linux, add `--with-deps` to the browser install command if system libraries are missing.

Tests run offline by default and block network connections. Response fixtures come from published Microsoft Learn examples; [sources.json](tests/fixtures/sources.json) records the source URLs and normalization applied to each. Tests inject failures and modify copies of those examples to exercise safeguards; these are not tenant captures.

The read-only live check needs a dedicated test tenant, a real app registration and a cached sign-in for that tenant:

```sh
HAVOC365_TEST_TENANT=<test-tenant-id> uv run pytest -m live
```

No live test starts a drill. Do not run drills against production.

To try the recorded interaction without credentials, run `uv run python assets/demo.py`. It runs the real `map` command with selected [published responses](tests/fixtures/sources.json), blocks network connections and omits domains because the examples contain no DNS responses. It writes the report in your current directory.

Regenerate the animation and reduced-motion still with `uv run assets/record_map.py`. Run it from the repository root with [VHS](https://github.com/charmbracelet/vhs), `ffmpeg`, `ttyd`, Chromium and the Noto Sans Mono font available. The script preserves readable pauses even when headless capture drops unchanged frames.

## License

[MIT](LICENSE). havoc365 is not affiliated with or endorsed by Microsoft. Microsoft 365 is a trademark of Microsoft Corporation.

*Microsoft 365. Give or take a day.*
