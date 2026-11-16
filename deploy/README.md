# Deploy a Lifeboat

Run `havoc365 lifeboat deploy` for the interactive wizard. Choose AWS, GCP, Alibaba Cloud or existing Linux cloud/on-prem SSH hosts. Select multiple providers, or repeat a provider to create multiple nodes.

See [architectures as code](architectures.md) for Mermaid diagrams of all four targets, a mixed shared-authority deployment and independent rooms.

The default only writes a local plan under `deploy/generated/`. It does **not** contact cloud accounts, SSH hosts, DNS or Microsoft. Review those files before:

```sh
havoc365 lifeboat deploy --config deploy/generated/config.json --apply
```

Type `APPLY` to authorize billable VM creation and host configuration. Automation must use `--non-interactive --config PATH --apply --confirm APPLY`. The wizard also asks for explicit HTTPS probe URLs: supplying them installs a supervised automatic-activation watcher on each authority; leaving them blank produces a clearly labeled **manual-only** deployment. There is no implicit yes and no automatic destroy or rollback. A failed apply can leave billable resources: inspect them and rerun the same config/output directory.

## What multiple targets mean

**Shared** is the default. The first target runs the only Lifeboat server and SQLite database. Remaining targets run Caddy HTTPS gateways to that authority. All URLs reach the same people, invitations, contacts and chat state. These gateways are **not data replicas**: the primary is a single point of failure. There is no automatic failover, multi-primary SQLite, or protection against primary disk loss. Gateways depend on working DNS and HTTPS connectivity to the primary hostname. Move a primary manually using a verified backup.

**Independent** runs a server and database on each target. Rooms have separate people, invites, contacts, messages and recovery secrets. Nothing synchronizes. Activate each room separately. Decide ahead of time which URL people should use.

Every public hostname has host-local cookie sessions; there is no cross-domain SSO. Issue invitations from the URL recipients should use. Gateway Caddy checks the incoming browser Origin against its own public HTTPS origin before rewriting Host and Origin for the primary's CSRF checks. Foreign or `null` origins are rejected. The upstream must be a public HTTPS origin, never an IP, credential URL, path, query or private service. TLS verification is never disabled. Direct access to port 8080 is loopback-only. Do not add a generic proxy that blindly rewrites Origin or permits cross-origin cookies. Caddy access logging is not enabled.

## Before apply

Use dedicated **Ubuntu 24.04** hosts, with persistent storage, outbound package repository/GitHub access, Python 3 and passwordless `sudo`. Bootstrap installs Caddy, Python's venv support, Git and uv; the runtime executable is always `/opt/havoc365/runtime/bin/havoc365`. It creates a restricted `havoc365` system user, a systemd service, and `/var/lib/havoc365/havoc365/lifeboat.sqlite3`. It **replaces `/etc/caddy/Caddyfile`**, so do not select a shared application host. Gateways only run Caddy, not an idle second Lifeboat database.

Local requirements:

- OpenSSH (`ssh`) and an existing private key with mode 0600, plus its matching `.pub` file for new VMs. Cloud key import supports RSA and Ed25519; check that the chosen cloud/region/image supports your key type.
- OpenTofu 1.6+ (`tofu`) or Terraform 1.6+ (`terraform`) for cloud targets. Initialization downloads the provider only on explicit apply. Keep the generated `.terraform.lock.hcl` and deployment state privately for later runs.
- Cloud credentials in the provider's normal local environment/profile/ADC, **never in the config, HCL, cloud-init, tfvars, output or command arguments**. Use short-lived operator credentials. Nothing authenticates to Microsoft.
- Deployment scripts/templates are bundled in the installed wheel; a source checkout is not required. The canonical assets live in [`src/havoc365/deploy_assets/`](../src/havoc365/deploy_assets/). `--assets PATH` is an optional override for reviewed customized assets.

Cloud requirements:

| Target | Inputs and prerequisites |
|---|---|
| AWS | Region, reviewed Ubuntu 24.04 AMI ID for that region and CPU architecture, VPC ID and **public** subnet ID. The subnet needs an Internet Gateway route. EC2/VPC/key-pair/security-group/EBS permissions and quotas are required. Default size is `t3.micro` (x86). |
| GCP | Project, region, zone; Compute Engine API, billing and network/instance/disk/firewall permissions. Default Ubuntu 24.04 image and `e2-micro`. Project/organization policy must allow metadata SSH keys and public IPv4; OS Login-only organizations need a separately prepared SSH host instead. Uses a new dedicated VPC/subnet. |
| Alibaba Cloud | Region, zone, reviewed Ubuntu 24.04 image ID and available x86 instance type. ECS/VPC/VSwitch/security-group/key-pair/disk permissions, quota and public bandwidth are required. Default size `ecs.t6-c1m1.large` is not available in every zone: choose a valid type. Uses a dedicated VPC/subnet and a retained encrypted data disk mounted at `/var/lib/havoc365`. Mainland public web hosting may require ICP registration; check local requirements before choosing a region. |
| SSH | Reachable host/IP, SSH user and optional `ssh_port`; provision no VM. Ensure `/var/lib/havoc365` is on persistent storage and arrange host firewall rules yourself. |

Set `ssh_cidr` to your administration network, not `0.0.0.0/0`. Cloud firewalls expose 80/443 publicly and 22 only to that CIDR. No Graph, database or internal application ports are opened.

Prepare DNS A records for **every** public hostname and allow inbound TCP 80/443 for Caddy's certificate issuance. Remove stale AAAA records unless IPv6 actually routes to this machine. Publicly trusted TLS and accurate clocks are required. New VM addresses are printed during apply; set DNS once the address is known. Cloud-assigned IPv4 addresses can change on stop/start; use provider-managed reserved IPs manually if required, and keep DNS current.

SSH uses `StrictHostKeyChecking=yes` and never disables verification or accepts unknown keys automatically. For a new VM, the first apply may stop after VM creation until you verify its SSH host key through a trusted cloud console, add it to your local `known_hosts`, and rerun. Do not blindly trust `ssh-keyscan` output. Configure DNS before rerunning bootstrap. If SSH/cloud initialization fails, output is suppressed to avoid leaking credentials: check connectivity, provider identity/permissions/quotas, host `journalctl -u havoc365 -u caddy` and `sudo -n true` privately. Don't paste raw logs or environment files into tickets.

The default Git ref is explicitly **main**, not a reproducible pin. Set `ref` to a reviewed commit SHA when you need one; an old commit may lack personal invitations or the required server API. Bootstrap downloads that public source and its package dependencies on the host. A primary restart briefly interrupts service. Updates retain secrets and state. Changing the saved target order, names, providers or topology is rejected; plan a manual migration instead.

## Saved config and offline planning

```json
{
  "version": 1,
  "topology": "shared",
  "repository": "https://github.com/JGalego/havoc365.git",
  "ref": "main",
  "ssh_key": "~/.ssh/id_ed25519",
  "ssh_cidr": "203.0.113.10/32",
  "watch": {
    "urls": ["https://your-required-service.example.org/health"]
  },
  "targets": [
    {
      "name": "primary",
      "provider": "ssh",
      "hostname": "lifeboat.example.org",
      "host": "203.0.113.20",
      "ssh_user": "ubuntu"
    },
    {
      "name": "gateway",
      "provider": "aws",
      "hostname": "lifeboat-alt.example.org",
      "region": "eu-west-1",
      "image": "ami-0123456789abcdef0",
      "instance_type": "t3.micro",
      "vpc_id": "vpc-0123456789abcdef0",
      "subnet_id": "subnet-0123456789abcdef0",
      "ssh_user": "ubuntu"
    }
  ]
}
```

The addresses/IDs above are examples, not deployable credentials or selected resources. Replace them. A GCP target needs `project`, `region`, `zone`, optionally `image`/`instance_type`. An Alibaba target needs `region`, `zone`, `image`, optionally `instance_type`, normally `ssh_user: "root"`. Unknown fields (including secret fields) are rejected before any external command.

```sh
havoc365 lifeboat deploy --config deployment.json --plan --non-interactive
```

All target inputs and local key permissions are validated before commands. Offline plans do not establish that the AMI exists, provider quotas permit creation, the public repository/ref exists, DNS resolves or SSH is reachable. Apply runs provider init, validate, plan, rejects deletions/replacements, then applies that saved plan. Review the rendered resources in advance: accepting `APPLY` authorizes their creation/update. Each target has its own state directory. Keep that private output directory; losing state can cause duplicate resources and losing the retained disk can lose the room. No cloud-init or Terraform output contains Lifeboat credentials; only the VM address is exported.

## Onboarding and operations

Bootstrap generates **distinct** recovery admin and legacy member tokens on the primary host in root-only `/etc/havoc365/lifeboat.env`. They are never returned by the installer. Production sets `HAVOC365_LIFEBOAT_LEGACY_ACCESS=0`; the legacy member token does not grant normal access. A rerun preserves existing tokens.

The wizard prints an SSH command to retrieve recovery credentials privately. Run it only from a trusted terminal; don't record/share that output. Open the chosen HTTPS Lifeboat URL and use the recovery admin token to sign in. In the web admin, create **two named admins** and invite members. Distribute each personal invitation privately through a channel independent of Microsoft 365. The wizard does **not** send email/SMS or deliver invitations. Store recovery credentials offline and verify both admins can sign in. Repeat for every independent room; for shared gateways, issue each invitation from the URL that member will use.

Keep offline phone contacts and the chosen URL available to everyone before an incident. The wizard does not import an invented roster. Add contacts through the admin interface or prepare a reviewed contacts file for a manual installation.

Manual activation, from an operator machine with the recovery admin token set:

```sh
havoc365 lifeboat activate --url https://lifeboat.example.org
havoc365 lifeboat standby --url https://lifeboat.example.org
havoc365 lifeboat auto --url https://lifeboat.example.org
```

### Automatic activation

Supply `watch: {"urls": ["https://your-required-service.example.org/health"]}` in a saved config, or enter probe URLs at the wizard prompt. After HTTPS verification, bootstrap installs and enables `havoc365-watch.service` on the authority, restarts it automatically on failure, and starts it on boot. Shared deployments install exactly one watcher on the primary, never on gateways; independent deployments install one on each room's authority. It reuses the host's private recovery environment and keeps watcher state separately in `/var/lib/havoc365-watch`. No SMS, roster, paid delivery or third-party credentials are configured by deployment.

Probe targets must be explicit public HTTPS URLs on port 443, without embedded credentials, query strings, fragments, whitespace or malformed escapes; public health paths are allowed. The watcher only probes these URLs, not an implicit Microsoft or DNS service inventory, and makes no tenant/authentication calls. Its existing default interval and failure threshold apply. An empty or omitted `watch.urls` is **manual-only**, not automatic activation; reapplying that configuration disables any previously deployment-managed watcher. Check `systemctl status havoc365-watch` and `journalctl -u havoc365-watch` privately to confirm it is running.

Manual activate/standby holds override the watcher until `auto` releases them; deployment never clears a hold. A host-local watcher is convenient but shares the authority's failure domain: loss of that host, its network path or DNS can prevent activation, while a probe failure can cause unwanted activation. It is not failover or primary availability monitoring. For better failure-domain isolation, leave deployment's probe list empty and run the watcher **on a separate trusted monitoring machine**, with `HAVOC365_LIFEBOAT_ADMIN_TOKEN` loaded privately:

```sh
havoc365 watch --url https://your-required-service.example.org \
  --lifeboat-url https://lifeboat.example.org
```

Choose probes and monitoring ownership deliberately, and practice a manual hold. Run only the chosen monitor per room; the wizard does not install or coordinate a second independent monitor.

Automatic personal SMS invitations are a separate **paid opt-in** watcher feature, not part of deployment. Keep a private roster such as `{"people":[{"name":"Alex","phone":"+12025550123"}]}` (maximum 50 people), and set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` and the Lifeboat recovery admin token privately on the monitoring host:

```sh
havoc365 watch --url https://your-required-service.example.org/health \
  --sms --invite-roster /private/roster.json \
  --lifeboat-url https://lifeboat.example.org
```

Each outage creates one-use 24-hour personal invitations and attempts delivery by paid SMS; recovery messages contain the plain room URL. An uncertain send is not automatically retried, so check delivery evidence and use a private fallback channel rather than assuming every member received an invitation. Manual holds still override watcher activation. SMS is optional and depends on the monitor's network path and the SMS provider.

### Backup and restore

AWS and GCP boot disks persist across reboot and are configured not to auto-delete with the VM. Alibaba stores state on the retained data disk (not the disposable system disk). Deletion protection and Terraform `prevent_destroy` are additional guardrails, not backups. Existing SSH hosts need their own retained disks. SQLite, credentials, Caddy TLS state and disaster recovery all need an operator. Files are SQLite blobs, limited to 25 MiB per attachment and 250 MiB total per room. The consistent database backup includes those blobs; allow for them in private off-host backup storage and restore/disk capacity. Gateways add no file capacity.

On the primary, take a consistent SQLite backup (service can remain running):

```sh
sudo install -d -o havoc365 -g havoc365 -m 700 /var/lib/havoc365/backups
sudo -u havoc365 env XDG_STATE_HOME=/var/lib/havoc365 \
  /opt/havoc365/runtime/bin/havoc365 lifeboat backup \
  --output /var/lib/havoc365/backups/lifeboat.sqlite3
```

Copy it off-host privately, encrypt it at rest and test restoration. Backups contain people/invite/session data; treat them as secrets. Back up `/etc/havoc365/lifeboat.env` separately in an encrypted recovery store, not in Terraform state. Caddy keeps TLS material in its service user's data directory.

For restore, stop the only server before replacing its database. Put the backup where `havoc365` can read it, with mode 0600, then:

```sh
sudo systemctl stop havoc365
sudo -u havoc365 env XDG_STATE_HOME=/var/lib/havoc365 \
  /opt/havoc365/runtime/bin/havoc365 lifeboat restore \
  --backup /var/lib/havoc365/backups/lifeboat.sqlite3 --replace
sudo systemctl start havoc365
curl --fail https://lifeboat.example.org/api/state
```

If moving to another host, fence the old authority first. Never run two writers against copies of the same room and call them replicas. Restore the backup and recovery environment privately, update DNS/gateway upstreams explicitly and verify login, contacts and chat. Expect existing sessions to need sign-in. No automatic promotion, DNS failover, teardown or disk cleanup is implemented. Cloud API execution, regional image availability and fresh-host provisioning must be tested in your own dedicated account before relying on this runbook.

Template references: [AWS EC2](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/instance), [GCP Compute](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/compute_instance), [Alibaba ECS](https://registry.terraform.io/providers/aliyun/alicloud/latest/docs/resources/instance).
