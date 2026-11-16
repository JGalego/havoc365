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

## What's inside?

- **Dependency maps.** Find Microsoft dependencies in sign-in logs, app registrations, DNS and runbooks. Inspect the results in the terminal or save a report.
- **Outage drills.** Preview a small volunteer group, temporarily block its Microsoft 365 access, then restore it. Admin accounts are excluded.
- **Workflow checks.** Inspect exported Teams and Power Automate flows for Microsoft connectors and notification dependencies.
- **Independent monitoring.** Probe HTTPS endpoints outside Microsoft, with optional outage and recovery SMS.
- **Lifeboat.** Coordinate incidents with personal invitations, mobile administration, check-ins, shared files and an offline contact card.
- **Deployment choices.** Plan or install on AWS, GCP, Alibaba Cloud and existing cloud or on-prem hosts. Share one authoritative room across URLs or keep rooms separate.

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

In a terminal, `map` shows a table and lets you inspect each service's full findings by number or name. Enter `/billing` to search service names, summaries and full evidence for “billing”. Press `l` for coverage limits or `q` to leave. Browsing the results makes no further network requests. Use `havoc365 map --no-interactive` to skip the explorer. Redirected output stays plain text; the Markdown report is saved before the explorer opens.

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
| `havoc365 lifeboat activate` | Opens chat and holds it open, overriding the watcher | No |
| `havoc365 lifeboat standby` | Closes chat until an administrator changes control | No |
| `havoc365 lifeboat auto` | Returns room control to the watcher | No |
| `havoc365 lifeboat backup` | Makes a consistent private backup, including files and accounts | No |
| `havoc365 lifeboat restore` | Restores a backup while the server is stopped | No |
| `havoc365 lifeboat deploy` | Plans or installs Lifeboat on cloud VMs and existing hosts | No; confirmed apply can create billable infrastructure |

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

Lifeboat is the independent part of the incident response: a browser room with personal invitations, mobile administration, incident updates, check-ins, contacts and shared files. It has no Entra dependency and loads no third-party scripts, fonts or assets.

### First administrator

Generate two different random secrets and start the server. Keep these values in a protected environment file for subsequent starts; don't generate new ones on every boot.

```sh
export HAVOC365_LIFEBOAT_TOKEN="$(uv run --no-project python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export HAVOC365_LIFEBOAT_ADMIN_TOKEN="$(uv run --no-project python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export HAVOC365_LIFEBOAT_LEGACY_ACCESS=0
havoc365 lifeboat serve --port 8080
```

```text
Lifeboat listening at http://127.0.0.1:8080. Expose it only behind an HTTPS reverse proxy.
Room starts in its saved state. Sign in with an invitation or recovery admin code. Keep recovery codes outside Microsoft.
```

Open **http://127.0.0.1:8080** on that machine and enter the administrator secret in **Access code**. Retrieve it privately from the environment file; don't paste it into chat or put it in a URL. From this checkout, use `uv run havoc365` instead of `havoc365`. Over SSH or VS Code Remote, forward port 8080 privately to your laptop.

In **Admin**, create personal invitations for at least two incident leads. Select the administrator role for those leads and member for participants. Keep the recovery secret offline, outside Microsoft's identity and password-recovery systems.

### Invitations and phones

Each invitation has an expiry and can be used once. Copy its link into an independent communication channel. The credential is in the URL fragment, not the server's query string, and the browser removes it before joining. An administrator can revoke a pending invitation or an enrolled person; revoking a person invalidates their sessions.

Browser sessions last up to 30 days and survive reloads. Credentials stay in host-only HttpOnly, SameSite cookies, marked Secure over HTTPS, rather than local storage. **Sign out** ends access on that device. Treat an unlocked enrolled device as room access. Invite names are assigned by the administrator, not verified against a directory.

The responsive interface has **Chat**, **Contacts** and, for administrators, **Admin**. Participants don't install anything. Download a contact card explicitly if you need it offline; it contains private contact details, so save it only on a trusted device. A browser without connectivity cannot send messages or fetch fresh updates. Keep a separate voice/SMS fallback.

`HAVOC365_LIFEBOAT_LEGACY_ACCESS=0` disables the old shared-member code. Omitting that setting preserves compatibility, but shared access has unverified names and no individual check-in identity. The administrator recovery secret also remains available to the CLI and watcher. Both configured secrets must be different and at least 32 characters.

### Running an incident

An administrator can open the room, publish a short update, name the incident lead and set the next-update time from a phone. Enrolled participants can check in as safe or needing help, with a note. These are coordination signals, not emergency dispatch or delivery receipts. Check-ins start fresh for each new incident.

**Open** holds chat open despite watcher recovery. **Standby** holds it closed. **Automatic** follows the watcher's last observation. Contacts remain available in all modes; an open room can be an incident or a drill.

```sh
havoc365 lifeboat activate --url http://127.0.0.1:8080
havoc365 lifeboat standby --url http://127.0.0.1:8080
havoc365 lifeboat auto --url http://127.0.0.1:8080
```

Use the same administrator secret as the server. HTTP activation is limited to numeric loopback addresses; remote deployments require HTTPS.

Messages are plain text. Ctrl+Enter (Cmd+Enter on macOS) sends; **Jump to latest** returns to incoming messages without disturbing someone reading older ones. A failed or uncertain send keeps its draft and warning. There is no automatic retry that could duplicate a message.

Enrolled members can attach images, video clips and other files, up to five per message and 25 MiB per file. Recognized images and videos get inline previews; other types download. Files use the room's SQLite storage and share a 250 MiB quota. Pending uploads expire after 24 hours; files attached to retained messages are removed when those messages age out. Downloads require current room access. Files are not malware-scanned: treat them as untrusted and keep secrets elsewhere.

Contacts can be edited in Admin without restarting. The optional `--contacts` JSON import still accepts `{"contacts":[{"name":"Incident desk","contact":"Your independent contact number"}]}` from a real file outside this checkout. Omitting the option preserves stored contacts.

### Deployment recommendations

Run the interactive wizard to choose one or more targets:

```sh
havoc365 lifeboat deploy
```

By default it writes an offline plan, not infrastructure. Review the generated files, then use `--apply` and explicitly confirm `APPLY` to authorize installation and possible charges. Cloud credentials stay in their providers' normal local configuration, not the deployment JSON.

The wizard accepts provider numbers or names, retries invalid fields and shows each target's role, URL and activation mode. Enter `q` at any prompt to cancel. To check a saved plan without installing anything:

```sh
havoc365 lifeboat deploy --config deploy/generated/config.json --check
```

`--check` makes read-only SSH/cloud calls. It checks SSH access and passwordless sudo on existing hosts, Ubuntu 24.04, Python 3.12+, systemd and at least 1 GiB free on `/var/lib` and `/opt`. Cloud targets require OpenTofu/Terraform 1.6+ and get a provider plan; deletion/replacement is refused. New VM host checks remain **PENDING** until allocation. Apply checks every target before the first change and checks new VMs before any installation. DNS/TLS, inbound ports, durable disks and available cloud capacity are not proven by pre-flight. See the [pre-flight details](deploy/README.md#pre-flight-checks).

Enter explicit HTTPS probe URLs to install a supervised watcher that opens the room when those endpoints fail. Leave them blank for manual-only activation. Deployment does not enable SMS or send invitations; the paid, opt-in roster workflow is described below.

**Shared** uses one authoritative server and HTTPS gateways: every URL sees the same room, but the primary is still a single point of failure. This is shared state, not replicated databases or automatic failover. **Independent** gives each target its own room and access. See the [deployment runbook](deploy/README.md) and [architectures as Mermaid code](deploy/architectures.md) for AWS, GCP, Alibaba Cloud and on-prem configurations.

Start with one always-on Linux VPS outside Microsoft, persistent storage, Caddy HTTPS and an unprivileged systemd service. Lifeboat listens on `127.0.0.1:8080`; never expose that HTTP port directly. For example:

```caddyfile
fallback.example.org {
    reverse_proxy 127.0.0.1:8080
}
```

Replace the example with your own hostname and point its DNS to your server. Keep DNS, hosting recovery and secret distribution independent of Microsoft. An Entra-only VPN in front of the room defeats the fallback. For private administration:

```sh
ssh -N -L 8080:127.0.0.1:8080 your-user@your-server
```

Then open http://127.0.0.1:8080 locally. VS Code Remote's **Ports** view provides the same private forwarding.

Avoid sleeping hosts and ephemeral disks. One authoritative server owns a room's SQLite state; independent SQLite copies are not replicas. A watcher on another host avoids losing chat and detection together. Supervise both processes, monitor them externally and rehearse recovery.

`GET /healthz` checks liveness; `/readyz` checks that the room can be read from storage. Neither tests Microsoft 365 or proves that a disk can accept writes.

### Automatic activation and invitations

```sh
havoc365 watch --url https://outlook.office.com/owa/ --lifeboat-url https://fallback.example.org
```

The watcher reports its state after every check, including after a restart, without overriding an administrator's hold. Reachable endpoints don't establish that authenticated Microsoft services work.

For personal invitations on an outage, provide a private JSON roster such as `{"people":[{"name":"Incident volunteer","phone":"+15551234567"}]}` and configure Twilio's account, auth token and sender:

```sh
havoc365 watch --url https://outlook.office.com/owa/ \
  --lifeboat-url https://fallback.example.org \
  --sms --invite-roster "$HOME/.config/havoc365/invite-roster.json"
```

This opts into paid SMS for every roster entry, up to 50 unique numbers. Each outage creates a 24-hour, one-use invitation per recipient; recovery sends the room URL without another invitation. Acceptance by Twilio is not proof of delivery. Ambiguous submissions aren't replayed automatically; check provider logs before resuming the watcher. Without `--sms`, no messages are sent.

Revoking someone in Admin ends their current access. Also remove them from the private roster to stop invitations on future outages.

### Backup and recovery

```sh
havoc365 lifeboat backup --output "$HOME/lifeboat-backup.sqlite3"
# Stop the Lifeboat service before restoring.
havoc365 lifeboat restore --backup "$HOME/lifeboat-backup.sqlite3"
```

Backup works while the server is running and uses SQLite's backup API, not an unsafe copy of the live database. Restore refuses a running service or an existing database; after taking a separate backup, use `--replace` deliberately to replace stopped state. Keep backups private and off the primary host. Restore the protected environment file separately, then test sign-in, a contact lookup and a message.

The database contains accounts, sessions, contacts, check-ins and shared files. The room retains the newest 1,000 messages and 10,000 audit entries; the browser shows 200 messages. Export the incident from Admin before those limits discard older entries. An export is not a full database backup and contains private incident data. Test recovery and device revocation before relying on the service.

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

Tests cover invitation races and revocation, persistent sessions, incident holds, staged file ownership and quotas, media byte ranges, automatic SMS invitations and backup restoration. Deployment tests render all supported targets without cloud access. Live cloud provisioning, fresh-host installation and physical iOS/Android testing still need an operator-run rehearsal; passing offline tests is not production certification.

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
