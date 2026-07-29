# Experimental Torrent Distribution And Recovery

Torrent support publishes or recovers immutable, verified model snapshots. The
interfaces and torrent-specific metadata formats remain experimental; ordinary
mirror, verification, and repair workflows do not require the torrent extra.

For the design contract, see
[Torrent Distribution And Archive Upgrade Requirements](torrent-distribution-requirements.md).
For backend evaluation, see [Torrent Backend Spike](torrent-backend-spike.md).

## Installation

Install the PyPI tool with torrent support:

```bash
uv tool install --force 'model-mirror-cli[torrent]'
```

For a development checkout:

```bash
uv sync --extra torrent
```

## Publication Identity

Every torrent is an immutable publication of one resolved `repo@commit`.
Publishing requires a clean, pinned mirror. It completes missing torrent hash
coverage, writes an ordinary hybrid v1/v2 `.torrent`, prints a standard magnet
URI and external-client data location, and creates a persistent fence that
prevents the canonical archive from moving to another commit.

The current publication profile is `hybrid-v1-v2-1`. The profile deterministically
fixes file order, piece sizing, padding, and descriptor encoding. A future
identity-affecting algorithm change will use a new profile rather than silently
changing an existing swarm.

The torrent payload contains the expected upstream files, not model-mirror's
private verification directory. A canonical descriptor inside the torrent's
hashed `info` dictionary carries repository identity, resolved commit, paths,
sizes, and content hashes.

## Create Or Publish

```bash
model-mirror upgrade org/model
model-mirror torrent create org/model
model-mirror torrent publish org/model
model-mirror torrent show org/model
```

- `upgrade` optionally precomputes missing torrent hash coverage.
- `create` writes torrent and recovery artifacts and establishes the update
  fence without requesting managed seeding.
- `publish` creates or reuses those artifacts and records durable seed intent.
- `show` reports publication identity, trust, fence, desired state, and
  observed backend state.

Downloads and same-commit repairs collect reusable torrent hashes during their
existing payload pass. Older archives can be upgraded incrementally:

```bash
model-mirror upgrade --all --dry-run
model-mirror upgrade --all
```

Upgrade reads only files missing trustworthy coverage for the current profile.
It does not change the model payload or resolved commit.

## Managed Seeding

Run the replaceable libtorrent backend:

```bash
model-mirror torrent serve
```

Use a normal service manager with restart-on-failure and restart-on-boot:

```ini
[Service]
ExecStart=/path/to/model-mirror --config /path/to/config.yaml torrent serve
Restart=always
```

Publication intent, verified file fingerprints, and the update fence are
durable. A restarted backend reconciles desired seeds without an explicit
reseed command or a normal payload-wide recheck. `model-mirror status` reports
desired and observed torrent state.

The backend owns peers, trackers, DHT, ports, bandwidth, and libtorrent resume
state. Model-mirror owns archive identity, verified hash coverage, publication
records, desired seed state, and lifecycle fences.

## Preferred External Clients

Emitted magnets and `.torrent` files are ordinary client-independent artifacts.
For external seeding, add the printed torrent to the client with the printed
data location, or record external ownership explicitly:

```bash
model-mirror torrent publish --external org/model
```

Model-mirror does not supervise or infer the runtime health of an external
client. Stop that client before modifying payload.

For downloading with a preferred client:

```bash
model-mirror torrent handoff /path/model@commit.torrent
# Download to the printed destination, then:
model-mirror torrent import /path/model@commit.torrent /printed/path/model
```

`handoff` prints a safe destination and exact follow-up command. `import`
validates hostile paths, the commit-scoped descriptor, sizes, and content
before atomically finalizing a normal local archive.

External import independently hashes downloaded files because model-mirror
cannot trust another client's private runtime state.

## Native Join

Model-mirror can download from a torrent file or magnet:

```bash
model-mirror torrent join /path/model@commit.torrent
model-mirror torrent join 'magnet:?xt=...' --seed
```

Native join stages on the archive filesystem, validates the publication
descriptor, and atomically finalizes the normal archive layout. It reuses
libtorrent's verified piece state to reconstruct model-mirror coverage without
rereading the completed payload. Upstream unavailability does not prevent
finalization as a usable torrent-verified archive.

## Trust Boundary

A trusted torrent proves consistency with the supplied infohash. It does not,
by itself, prove that the publisher's bytes were authentic Hugging Face
content.

Imported state therefore records these independently:

- content verification, such as `torrent-verified`
- publication trust, such as `trusted-infohash`
- upstream provenance
- upstream availability

The recovered archive remains useful when upstream disappears and can later be
checked against upstream if it returns.

## Updates, Repair, And Retirement

An active publication fence prevents `repair --update` from moving the
canonical archive to another commit. Read-only verification and upstream-change
detection remain available.

Stopping network activity retains the fence:

```bash
model-mirror torrent stop org/model
```

Release seed intent and the update fence explicitly:

```bash
model-mirror torrent retire org/model
model-mirror repair --update org/model
```

Retirement cannot revoke torrent metadata already shared with peers.

Same-commit repair enters maintenance, waits for the managed backend to detach,
repairs only recorded damaged paths, refreshes their torrent coverage, and
resumes the same publication after verification succeeds. A mismatch leaves
seeding stopped. Updating to another commit remains blocked until retirement.

Mirror removal is also blocked by an active publication. Stop any external
client, retire the publication, then run `model-mirror remove REPO`.
