# QFLX-38 - A14 Plex native feasibility spike (Ultra, read-only)

Status: spike complete 2026-10-10. Jira: QFLX-38. Parent design:
`2026-10-09-ucc-divorce-design.md` section 7 (variants P1 / P2), decision D-1.

Method: read-only probes over the standard SSH wrapper as the slot user. The
only writes were a temp directory under `~/tmp` (the PMS `.deb` was
downloaded, unpacked, inspected with `ldd`, then deleted; nothing was
executed). No app was started, stopped or restarted. No tokens or hostnames
appear in this note; Preferences.xml keys are reported present/absent only.

## Recommendation

**P1: Plex stays UCC on Ultra; native Plex is installed fresh (new identity)
on box 2.** Do not convert Plex in place. P2 fails its own entry condition
("PMS can bind a private port on the shared host"): PMS cannot be moved off
port 32400 and the host network namespace is shared (finding a). The claim
risk (d) and the zero hardware gain (e) remove any upside that would justify
the risk. Glibc and thread budget are NOT blockers (b, c); they are not the
reason.

## Findings

### (a) Bind / listen set

- Current Plex runs as a rootless container under the slot user. Host-side it
  is reachable only through the recorded app port, held on three listeners
  (loopback, docker bridge address, slot public address). The listener rows
  show no owning process for the user (container proxy), unlike the native
  arrs which show their pid.
- Plex's own port 32400 is **not bound anywhere on the host**; it exists only
  inside the container network. So nobody owns 32400 host-side today, and
  `ip_unprivileged_port_start` is 1024, so a user may bind it.
- The host network namespace is **shared with other tenants**: SSDP/UPnP port
  1900 is already bound by many sockets on the slot's address, including a
  wildcard bind. A native PMS binds 32400 (fixed, no supported override;
  `ManualPortMappingPort` only changes the advertised port) plus DLNA 1900,
  GDM 32410-32414 and 32469 on wildcard addresses. Consequences:
  - 32400 would be a wildcard listener on a shared host, first-come
    first-served, with no per-slot guarantee. Not a private port.
  - DLNA (1900) must be disabled; GDM discovery too.
  - The recorded app port would need a forwarder (a second process) to keep
    the existing listen set, since PMS cannot listen there itself.
- Not verified (would need a bind test, outside a read-only spike): whether
  the platform firewall filters 32400 from outside. Treat it as exposed until
  proven otherwise.

### (b) glibc / ldd

- Current PMS: 1.43.4.10903-e5521bd8c (from `/identity`). The linux-x86_64
  release is shipped as a `.deb`/`.rpm` (there is no plain tarball); the
  `.deb` is ~83 MB.
- The package is self-contained and **musl-based** (bundled musl loader and
  libc, own libstdc++ stack). `ldd` on Media Server, Transcoder, Scanner,
  SQLite, Script Host, Tuner Service and DLNA Server (with the package's own
  lib dir on the path) reports every dependency resolved, none missing; no
  GLIBC or GLIBCXX symbol version is required.
- Host glibc is 2.41 (Debian 13), so there is no compatibility problem and
  no need for the CI bullseye artifact used for seerr. Box 2 is unaffected.

### (c) Task / thread budget

- Slot cap: `ulimit -u` = 2000 tasks (threads count).
- Sampled now: slot total ~823 tasks. Plex accounts for ~195: Media Server
  149, one active transcode 20, plug-in host 12, tuner service 11, others.
- A native PMS has the same thread profile (same binaries), so conversion
  neither adds nor removes headroom. Each extra concurrent transcode costs
  ~20 tasks; the stream cap of 4 per user bounds that. Not a blocker. Keep
  the `thread-ceiling` canary as the guard.

### (d) Claim exposure / identity

- Data location: the UCC Plex config lives under `~/.config/plex`
  (Library/Application Support/Plex Media Server, ~1.2 GB apparent size
  excluding linked media). **`~/.apps/plex` is an empty directory (dated
  2025)**; the earlier assumption that native would "reuse `~/.apps/plex`
  data" is wrong, the data root is `~/.config/plex`.
- Preferences.xml has the online token, the account name/mail fields,
  `ProcessedMachineIdentifier`, `MachineIdentifier`, `CertificateUUID`: the
  server is **claimed** and its identity is in that file. A native PMS
  pointed at the same config directory inherits the identity, so
  plex.tv sees the same server, which is what in-place conversion wants and
  also why a proof must never run beside the live one.
- A proof copy has two bad options:
  1. Copy Preferences.xml as is: two live servers with one machine identity
     and one token. plex.tv entitlements, relay and the existing members'
     shares would flap between them. Unacceptable.
  2. Strip the token (sanitize): the proof is **unclaimed**. An unclaimed PMS
     accepts a claim from anyone who can reach its web port with a valid
     plex.tv claim token, and (finding a) its port is on a shared host
     namespace. Claim needs the claimer's own claim token, but there is no
     way to bound who can reach the port. Mitigation would be Plex's
     allowed-networks setting, which is itself an in-app preference we cannot
     set before first start without more verification.
- Net: there is no private-port, unclaimed-safe proof available on this slot.
  The "unclaimed instance cannot be claimed by a stranger" entry condition
  of P2 is not demonstrable here.

### (e) Transcoder / hardware

- Container and native use the same bundled `Plex Transcoder`; software
  transcoding. `HardwareAcceleratedCodecs` is 0 today.
- The host exposes a DRM `card0` node owned by root, group `video`, mode 660;
  the slot user is not in `video` and there is no render node. Hardware
  transcoding is **unavailable either way**; native gains nothing.
- Transcoder temp is `/config` inside the container (a path in the data
  tree); native would set an explicit temp dir under the slot, and the
  `plex-transcoder` / `plex-playback` canaries must be re-proven.
- CPU is a shared 128-thread EPYC; no per-slot CPU controls differ between
  container and native.

## Decision record

| Question | Answer |
|---|---|
| Can a user bind the recorded listen set natively? | Not directly: the recorded port is held by the container proxy, and PMS listens on fixed 32400 on a shared namespace |
| glibc? | Not an issue (musl bundle, all libs resolve) |
| Task budget? | Not an issue (~195 of 2000, unchanged) |
| Claim safe? | No safe proof on this slot (clone or unclaimed-exposed) |
| HW / transcoder gain? | None |
| Variant | **P1** (D-1 stands): UCC on Ultra, native fresh on box 2 |

## Follow-ups for box 2 (carry into QFLX-39/42)

- Install via `.deb` extract (no root), set DLNA/GDM off, pin the explicit
  transcode temp dir, auto-update off (already in the design).
- Box 2 must fix its own port story; there the host is dedicated, so 32400
  is fine.
- Keep the gate probe pinned to UCC Plex on Ultra until the box is retired.
