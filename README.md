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
   contoso.onmicrosoft.com · 1,240 users

   If Exchange Online goes down
     ✗ 14 apps and devices send mail through Exchange   monitoring-prod, scan-to-email, +12
     ✗ 6 apps send mail through Microsoft Graph         HR Portal, Ticketing, +4
     ✗ contoso.com mail goes straight to Exchange       no fallback in front of it

   If SharePoint goes down
     ✗ 9 runbooks are stored in SharePoint              Major Incident.docx, +8

   If Entra ID sign-in goes down
     ✗ 47 apps sign in with Entra ID                    Payroll, VPN, Wiki, +44

   Report written to blast-radius.md
   ```

## Commands

| Command | What it does | Changes your tenant |
|---|---|---|
| `havoc365 login` | Signs in with a device code | No |
| `havoc365 map` | Reports dependencies on Exchange Online, SharePoint and Entra ID | No |
| `havoc365 drill plan` | Shows who a drill would lock out, and for how long | No |
| `havoc365 drill run` | Locks a pilot group out of Microsoft 365 until the timer ends | Adds one Conditional Access policy |
| `havoc365 drill revert` | Ends a drill early | Removes that policy |
| `havoc365 drill report` | Reports sign-in attempts during the drill | No |

## Drills

A drill blocks a pilot group's access to Microsoft 365. It doesn't reproduce a failure inside Microsoft's services. Use volunteers and agree on a time beforehand.

```sh
havoc365 drill plan --group "Outage Drill" --duration 30m
havoc365 drill run  --group "Outage Drill" --duration 30m
```

- The drill creates one Conditional Access policy, named `havoc365-drill-<id>`, to block Microsoft 365 for that group.
- It refuses to run for groups with more than 25 members or anyone holding an admin role.
- It revokes the group's sessions at the start, though existing access tokens may continue to work until they expire.
- The process removes the policy when the timer runs out or you press Ctrl-C. If it crashes, the policy stays in place. Run `havoc365 drill revert` to remove it.

## Permissions

havoc365 uses delegated Microsoft Graph permissions. Access is limited by both the app's consented permissions and the signed-in account's privileges.

| Command | Microsoft Graph permissions | Entra roles |
|---|---|---|
| `map` | `AuditLog.Read.All` `Application.Read.All` `Domain.Read.All` `Files.Read.All` | Global Reader |
| `drill` | Those of `map`, plus `GroupMember.Read.All` `RoleManagement.Read.Directory` `Policy.Read.All` `Policy.ReadWrite.ConditionalAccess` `User.RevokeSessions.All` | Global Reader, Conditional Access Administrator, Helpdesk Administrator |

Reading sign-in logs requires Entra ID P1 or higher. Without it, `map` skips the checks that need those logs and reports which checks it skipped.

## Roadmap

- [ ] `map` for Exchange Online, SharePoint and Entra ID
- [ ] `drill`
- [ ] `map` for Teams webhooks and Power Automate flows
- [ ] `watch`: spots outages from outside Microsoft's network and pages you by SMS
- [ ] `lifeboat`: a standby chat and contact list, hosted outside Microsoft, that opens when Microsoft 365 goes down

## Documentation

Follow the [prose guidelines](WRITING.md) when editing this README or other project documentation.

## License

[MIT](LICENSE). havoc365 is not affiliated with or endorsed by Microsoft. Microsoft 365 is a trademark of Microsoft Corporation.

*Microsoft 365. Give or take a day.*
