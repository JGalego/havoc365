# Deployment architectures as code

These Mermaid diagrams describe the deployed topology, not a future replication design. Render this file with GitHub's Markdown viewer or a Mermaid-compatible editor. In shared mode, pick exactly one of the VM authorities below; the other targets run gateways instead. In independent mode, every target is an authority for its own room.

Explicit probe URLs enable an optional installed `havoc365-watch.service` on each authority; blank URLs mean manual-only activation, with no installed watcher. The watcher probes only the specified HTTPS endpoints, never an implicit Microsoft/DNS inventory or tenant APIs. A recommended independent monitor is shown as an alternative for failure-domain isolation: choose it instead of the host-local watcher, not alongside it. Deployment enables no paid SMS or invitation delivery. Personal credentials establish HttpOnly cookie sessions; the root recovery token stays private. Manual holds override every watcher.

## AWS authority

```mermaid
flowchart LR
    participants["Participants + 2 named admins<br/>Personal invitations; host-local sessions"]
    operator["Operator<br/>OpenTofu/Terraform + verified SSH"]
    watcher["Recommended independent monitor alternative<br/>Private root-admin environment"]
    dependencies["Only explicitly configured HTTPS probes"]
    subgraph aws["AWS public subnet — Ubuntu 24.04 EC2"]
        firewall["Security group<br/>80/443 public; SSH admin CIDR only"]
        caddy["Caddy :443<br/>TLS + Origin checks"]
        server["systemd havoc365.service<br/>127.0.0.1:8080 only"]
        localwatch["Optional installed systemd watcher<br/>Enabled on boot when probe URLs supplied"]
        disk[("Encrypted retained EBS boot disk<br/>SQLite + attachment blobs<br/>/var/lib/havoc365/havoc365")]
        secrets["Root-only /etc/havoc365/lifeboat.env<br/>Distinct recovery admin + member tokens<br/>LEGACY_ACCESS=0"]
        firewall --> caddy --> server --> disk
        secrets --> server
        secrets --> localwatch
        localwatch -->|HTTPS activation; manual hold wins| caddy
        localwatch --> dependencies
    end
    participants -->|HTTPS| firewall
    operator -->|Provision + SSH install, never root secrets in TF| firewall
    watcher -->|HTTPS activation; manual hold wins| caddy
    watcher --> dependencies
```

## GCP authority

```mermaid
flowchart LR
    people["Participants + 2 named admins<br/>Personal invitations + HttpOnly cookies"]
    operator["Operator<br/>OpenTofu/Terraform + verified SSH"]
    watcher["Recommended independent monitor alternative<br/>Private recovery-admin token"]
    probes["Only explicitly configured HTTPS probes"]
    subgraph gcp["GCP dedicated VPC/subnet — Ubuntu 24.04 Compute Engine"]
        firewall["VPC firewall<br/>80/443 public; SSH admin CIDR only"]
        caddy["Caddy :443<br/>TLS + Origin checks"]
        server["systemd havoc365.service<br/>127.0.0.1:8080 only"]
        localwatch["Optional installed systemd watcher<br/>Enabled on boot when probe URLs supplied"]
        disk[("Retained persistent boot disk<br/>SQLite + attachment blobs<br/>/var/lib/havoc365/havoc365")]
        secrets["Host-generated root-only recovery environment<br/>Distinct admin/member secrets; LEGACY_ACCESS=0"]
        firewall --> caddy --> server --> disk
        secrets --> server
        secrets --> localwatch
        localwatch -->|HTTPS activation; manual hold wins| caddy
        localwatch --> probes
    end
    people -->|HTTPS| firewall
    operator -->|Provision + SSH install| firewall
    watcher -->|HTTPS activation; manual hold wins| caddy
    watcher --> probes
```

## Alibaba Cloud authority

```mermaid
flowchart LR
    people["Participants + 2 named admins<br/>Personal invitations + host-local cookies"]
    operator["Operator<br/>OpenTofu/Terraform + verified SSH"]
    watcher["Recommended independent monitor alternative<br/>Private recovery-admin token"]
    probes["Only explicitly configured HTTPS probes"]
    subgraph alibaba["Alibaba Cloud dedicated VPC/VSwitch — Ubuntu 24.04 ECS"]
        firewall["Security group<br/>80/443 public; SSH admin CIDR only"]
        caddy["Caddy :443<br/>TLS + Origin checks"]
        server["systemd havoc365.service<br/>127.0.0.1:8080 only"]
        localwatch["Optional installed systemd watcher<br/>Enabled on boot when probe URLs supplied"]
        disk[("Encrypted retained data disk /dev/vdb<br/>Mounted /var/lib/havoc365<br/>SQLite + attachment blobs")]
        secrets["Root-only /etc/havoc365/lifeboat.env<br/>On system disk; back up separately<br/>Distinct secrets; LEGACY_ACCESS=0"]
        firewall --> caddy --> server --> disk
        secrets --> server
        secrets --> localwatch
        localwatch -->|HTTPS activation; manual hold wins| caddy
        localwatch --> probes
    end
    people -->|HTTPS| firewall
    operator -->|Provision + SSH install| firewall
    watcher -->|HTTPS activation; manual hold wins| caddy
    watcher --> probes
```

## Existing cloud or on-prem host

```mermaid
flowchart LR
    people["Participants + 2 named admins<br/>Chosen public HTTPS URL"]
    operator["Operator + verified SSH key<br/>No VM provisioning"]
    watcher["Recommended independent monitor alternative<br/>Private recovery-admin token"]
    probes["Only explicitly configured HTTPS probes"]
    subgraph existing["Dedicated existing Ubuntu 24.04 host"]
        firewall["Operator-managed firewall/NAT<br/>Public DNS + TCP 80/443<br/>SSH restricted to admin network"]
        caddy["Caddy :443<br/>TLS + Origin checks"]
        server["systemd havoc365.service<br/>127.0.0.1:8080 only"]
        localwatch["Optional installed systemd watcher<br/>Enabled on boot when probe URLs supplied"]
        disk[("Operator-provided persistent disk<br/>SQLite + attachment blobs<br/>/var/lib/havoc365/havoc365")]
        secrets["Host-generated root-only recovery env<br/>Admin + separate member token<br/>LEGACY_ACCESS=0"]
        firewall --> caddy --> server --> disk
        secrets --> server
        secrets --> localwatch
        localwatch -->|HTTPS activation; manual hold wins| caddy
        localwatch --> probes
    end
    people -->|HTTPS + host-local cookies| firewall
    operator -->|SSH bootstrap with passwordless sudo| firewall
    watcher -->|HTTPS activation; manual hold wins| caddy
    watcher --> probes
```

## Mixed shared-authority deployment (default)

```mermaid
flowchart TB
    people["Participants + admins<br/>Use chosen URL; invite issued from that URL<br/>Sessions are host-local; no cross-domain SSO"]
    watcher["Recommended independent monitor alternative<br/>Root admin token loaded privately<br/>HTTPS activation; manual hold wins"]
    probes["Only explicitly configured HTTPS probes"]
    subgraph primary["First target: any provider or existing SSH host — AUTHORITY SPOF"]
        front["Caddy public HTTPS<br/>Origin checks"]
        server["systemd Lifeboat<br/>127.0.0.1:8080"]
        localwatch["Optional installed systemd watcher<br/>Authority only, never on gateways"]
        db[("ONE SQLite DB + attachment blobs<br/>Persistent disk")]
        root["Host-only recovery env<br/>LEGACY_ACCESS=0"]
        front --> server --> db
        root --> server
        root --> localwatch
        localwatch -->|HTTPS activation; manual hold wins| front
        localwatch --> probes
    end
    subgraph aws["AWS alternative URL"]
        gw1["Caddy HTTPS gateway only<br/>Reject foreign Origin BEFORE rewrite"]
    end
    subgraph gcp["GCP alternative URL"]
        gw2["Caddy HTTPS gateway only<br/>No Lifeboat DB"]
    end
    subgraph ali["Alibaba Cloud alternative URL"]
        gw3["Caddy HTTPS gateway only<br/>No Lifeboat DB"]
    end
    subgraph premise["On-prem alternative URL"]
        gw4["Caddy HTTPS gateway only<br/>No Lifeboat DB"]
    end
    people -->|HTTPS| front
    people -->|HTTPS| gw1
    people -->|HTTPS| gw2
    people -->|HTTPS| gw3
    people -->|HTTPS| gw4
    gw1 -->|Verified HTTPS; rewrite Host/Origin to primary| front
    gw2 -->|Same shared reads + writes| front
    gw3 -->|Same shared reads + writes| front
    gw4 -->|Same shared reads + writes| front
    watcher --> front
    watcher --> probes
    warning["NOT replicas or HA<br/>No automatic failover<br/>Primary or its network failure stops every URL"]
    db --- warning
```

## Independent Lifeboats

```mermaid
flowchart TB
    people["Participants + 2 admins per room<br/>Choose room explicitly; distribute separate invitations"]
    watchers["Recommended independent monitor alternatives<br/>Separate root token and explicit URL per room"]
    probes["Only explicitly configured HTTPS probes"]
    subgraph a["AWS — room A"]
        ca["Caddy HTTPS"] --> sa["systemd :8080 loopback"] --> da[("Persistent SQLite + blobs A")]
        ra["Host-only recovery env A"] --> sa
        wa["Optional installed systemd watcher A"] -->|HTTPS; manual hold wins| ca
        ra --> wa
    end
    subgraph b["GCP — room B"]
        cb["Caddy HTTPS"] --> sb["systemd :8080 loopback"] --> db[("Persistent SQLite + blobs B")]
        rb["Host-only recovery env B"] --> sb
        wb["Optional installed systemd watcher B"] -->|HTTPS; manual hold wins| cb
        rb --> wb
    end
    subgraph c["Alibaba Cloud — room C"]
        cc["Caddy HTTPS"] --> sc["systemd :8080 loopback"] --> dc[("Retained data disk: SQLite + blobs C")]
        rc["Host-only recovery env C"] --> sc
        wc["Optional installed systemd watcher C"] -->|HTTPS; manual hold wins| cc
        rc --> wc
    end
    subgraph d["Existing/on-prem — room D"]
        cd["Caddy HTTPS"] --> sd["systemd :8080 loopback"] --> dd[("Persistent SQLite + blobs D")]
        rd["Host-only recovery env D"] --> sd
        wd["Optional installed systemd watcher D"] -->|HTTPS; manual hold wins| cd
        rd --> wd
    end
    people --> ca
    people --> cb
    people --> cc
    people --> cd
    watchers -->|HTTPS; manual hold wins| ca
    watchers --> cb
    watchers --> cc
    watchers --> cd
    watchers --> probes
    wa --> probes
    wb --> probes
    wc --> probes
    wd --> probes
    warning["No synchronization or automatic failover<br/>Separate people, invites, sessions, contacts and files<br/>LEGACY_ACCESS=0 on every authority"]
    people --- warning
```

For every authority, copy consistent SQLite backups off-host and encrypt them. Attachments are stored inside SQLite, with a 25 MiB per-file and 250 MiB room quota; include that space in backup, restore and disk planning. Back up recovery environment files separately. Provider deletion protection is not a backup. See the [deployment runbook](README.md) for apply, onboarding and recovery.
