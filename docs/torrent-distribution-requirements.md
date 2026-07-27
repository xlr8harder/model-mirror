# Torrent Distribution And Archive Upgrade Requirements

Status: Experimental normative baseline; initial implementation completed
2026-07-27.

The torrent CLI, managed backend, and torrent-specific metadata formats are
experimental. This document defines the current contract, but compatibility is
not promised until the feature is stabilized.

## Purpose

This document defines the product boundary for publishing verified
model-mirror archives through BitTorrent, seeding them, joining an existing
swarm to create a local mirror, repairing a published mirror, and adding
complete torrent hash coverage to existing archives.

The normative core contains 29 requirements. Goals, design notes, test
scenarios, delivery slices, and open questions are intentionally kept outside
that count.

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY**
are normative.

## Product Goals

- Publishing and seeding a verified mirror should be one command once a seeder
  backend is configured.
- Joining a model-mirror torrent should produce a normal local model-mirror
  archive without manual conversion.
- Users should be able to hand standard torrent artifacts to their preferred
  client for download or seeding and return to model-mirror for validation and
  archive finalization.
- A trusted torrent should remain sufficient to recover a usable archive when
  the upstream repository is unavailable.
- Publishers of the same repository commit should converge on the same swarm.
- Torrent support must preserve commit pinning and distinguish upstream
  verification from publication trust.
- Existing archives should reuse trustworthy hashes and scan payload only when
  required torrent hashes are missing.
- Seeding and joining should not require a second full payload copy.
- Desired seeds should resume after host or backend restart without an explicit
  reseed command or an unnecessary payload-wide check.

## Non-Goals

- Model-mirror is not a general-purpose torrent creator for unmanaged files.
- A torrent is not a mutable `latest` pointer.
- Initial support does not require simultaneous local storage of multiple
  commits for one repository.
- Initial support does not require cross-snapshot deduplication, HTTP web seeds,
  mutable DHT pointers, or a public torrent catalog.
- An arbitrary unsigned torrent is not automatically trusted as authentic
  upstream content.
- Model-mirror does not need to supervise arbitrary external torrent clients or
  own their restart, bandwidth, peer, and resume behavior.

## Terms

- **Semantic snapshot ID:** Upstream provider, repository type, canonical
  repository ID, and full resolved commit.
- **Publication:** Immutable torrent metadata and model-mirror publication
  state for one semantic snapshot.
- **Publication profile:** A versioned deterministic algorithm for constructing
  identity-bearing torrent metadata.
- **Publication descriptor:** Portable deterministic model-mirror metadata
  encoded in a custom field of the torrent's hashed `info` dictionary.
- **Publication fence:** Persistent state preventing the canonical archive path
  from moving to another commit while a publication refers to it.
- **Desired seed state:** Persistent model-mirror intent that a publication
  should be registered and active in the configured torrent backend.
- **Maintenance:** Temporary detachment of a publication from its torrent
  session so the same pinned commit can be repaired.
- **Retirement:** Explicit removal of local desired seed state and the
  publication fence. It cannot revoke a torrent from other peers.
- **Hash coverage:** Files with complete reusable torrent hashes for a specific
  publication profile.
- **Torrent-verified:** The payload matches the torrent's piece hashes and
  infohash, without making an upstream-provenance claim.
- **Trusted publication:** The exact infohash or a detached attestation was
  obtained through a trust source explicitly recorded by model-mirror.

## Normative Requirements

### Snapshot And Torrent Identity

- **REQ-01 — Semantic identity:** Model-mirror MUST identify a publishable
  snapshot as `<provider>:<repo-type>:<repo-id>@<full-resolved-commit>`.
  Requested branches, tags, and symbolic revisions MUST NOT be part of this
  identity.

- **REQ-02 — Commit-scoped content:** A publication's downloadable payload MUST
  contain exactly the authoritative expected file list for one resolved
  commit. Different commits MUST produce different publications even when
  their upstream payload happens to be byte-identical. Different requested
  revisions resolving to the same commit MUST identify the same publication.

- **REQ-03 — Deterministic construction:** A versioned publication profile MUST
  define every input affecting the torrent `info` dictionary, including file
  ordering, root naming, piece-length selection, alignment, padding, and
  descriptor bytes. The same snapshot and profile MUST produce the same
  infohashes. Timestamps, publisher identity, local paths, trackers, web seeds,
  and runtime state MUST NOT affect those infohashes. An identity-affecting
  algorithm change MUST use a new profile version.

- **REQ-04 — Protocol identity:** Initial publications SHOULD be hybrid
  BitTorrent v1/v2 torrents and MUST record both infohashes. Model-mirror SHOULD
  use the full v2 infohash as its canonical protocol distribution ID while
  retaining the v1 hash for compatibility. Labels and individual file Merkle
  roots MUST NOT be treated as complete-snapshot identity.

### Archive And Portable Metadata

- **REQ-05 — Authoritative snapshot description:** Every clean mirror MUST have
  an authoritative snapshot description matching its resolved commit,
  expected file list, and verification state. Mirror creation, same-commit
  repair, and commit update MUST preserve or atomically replace that
  description as appropriate.

- **REQ-06 — Portable descriptor:** Every publication MUST encode a canonical,
  versioned descriptor as a custom field within the hashed torrent `info`
  dictionary. It MUST contain the semantic snapshot ID and sorted expected
  payload entries. Each entry MUST include path, size, full-file SHA-256, and
  available Git blob or LFS identity. The descriptor MUST provide enough
  information to derive the canonical local archive target without adding a
  file to the downloadable model payload.

- **REQ-07 — Metadata separation:** The portable descriptor MUST NOT contain
  source-machine mtimes, absolute paths, requested revisions, verification
  timestamps, repair state, locks, progress, peer state, resume data,
  credentials, access tokens, or self-referential final infohashes. Local
  `.manifest` and `.verification` files MUST NOT be used verbatim as the wire
  descriptor. Detached attestations, torrent files, publication records, and
  seeder resume data MUST remain outside the hashed `info` dictionary and
  torrent payload.

- **REQ-08 — Versioned archive reading:** Archive metadata readers MUST dispatch
  by schema version, continue to read supported older formats as newer formats
  are introduced, and permit supported schema-1 archives to remain in place
  indefinitely.
  Readers MUST reject malformed or unknown future formats without rewriting
  them. Failures MUST identify the path, detected format, and safe recovery
  action when one exists.

- **REQ-09 — Separate coverage state:** Profile-specific torrent hash coverage
  MUST use versioned metadata separate from the existing archive manifest
  schema. Coverage MAY be absent, partial, or complete without making an
  otherwise supported archive unreadable. Coverage replacement MUST be atomic,
  and per-file enrichment MUST be checkpointed and resumable.

### Torrent Hash Production

- **REQ-10 — Single-pass accumulation:** Xet and HTTP downloads SHOULD feed
  full-file integrity hashers and torrent hash accumulators in the same payload
  pass. Same-commit repair and full verification SHOULD collect missing torrent
  hashes while already reading the affected bytes.

- **REQ-11 — Correct resumable hashing:** The publication profile and piece
  length MUST be known before its hashes are accumulated. Accumulation MUST
  support concurrent files, retries, truncation, and resumed prefixes.
  Torrent hashes MUST be checkpointed only after the file passes upstream size
  and hash validation.

- **REQ-12 — Reuse and fallback:** Model-mirror MUST reuse trustworthy
  profile-specific hashes and invalidate them when their file fingerprint no
  longer matches. Existing archives MUST have a fallback that reads only files
  missing required coverage. Stream-accumulated results MUST be cross-checkable
  against an independent builder that rereads the same payload.

### Publication And Seeding

- **REQ-13 — Safe publication:** Publication MUST be explicit, acquire the
  existing model-operation lock for its transition, and refuse dirty,
  incomplete, busy, ambiguous, or commit-inconsistent archives. It MUST ensure
  complete profile coverage, create or reuse deterministic torrent metainfo,
  atomically write the publication record, and establish the publication fence
  before seeding begins.

- **REQ-14 — One-command publication:** With a configured supported backend,
  `model-mirror torrent publish REPO` MUST create or reuse the publication,
  register it for seeding, and print its magnet and torrent path in one
  idempotent command. The emitted magnet and `.torrent` MUST be ordinary,
  client-independent artifacts usable without model-mirror. Model-mirror MUST
  also emit a backend-independent recovery record containing the semantic
  snapshot ID, descriptor digest, publication profile, both infohashes, and
  optional detached attestations. Clean archives, especially private or gated
  ones, MUST NOT be published implicitly.

- **REQ-15 — Control-plane boundary:** Model-mirror MUST own publication records,
  desired seed state, and lifecycle transitions. A replaceable long-running
  backend MUST own peer networking, DHT, trackers, ports, bandwidth limits,
  transfers, and backend resume state. Initial delivery MUST include at least
  one supported backend able to add, detach, resume, inspect, and retire a seed
  without deleting or copying its payload. The backend MUST accept
  model-mirror's authoritative verified-piece state so that adding or
  reattaching a valid publication does not require a payload-wide recheck.

- **REQ-16 — Durable safe seeding:** Desired seed state and the publication
  fence MUST survive CLI, daemon, backend, and host restart. A configured
  backend MUST automatically reconcile and resume desired seeds without an
  explicit reseed command. It MUST use valid fast-resume data or reconstruct
  seed state from matching model-mirror file fingerprints and verified hash
  coverage without rereading payload solely to reconstruct backend state.
  Status MUST distinguish desired state from observed backend state. A seeder
  MUST NOT hold the normal model-operation lock for its lifetime. A missing or
  changed fingerprint MUST stop seeding and mark the publication unhealthy
  rather than trusting stale resume data.

### Updates, Retirement, And Repair

- **REQ-17 — Update fence:** Read-only verification and upstream-change
  detection MUST remain available for a published archive, but any operation
  that would move its canonical path to another commit MUST fail before
  mutation while the publication fence exists.

- **REQ-18 — Explicit retirement:** Stopping network activity MUST NOT release
  the publication fence. Retirement MUST be explicit, remove local desired seed
  state and the fence, and explain that already distributed torrent metadata
  cannot be revoked. A canonical mutable path MUST NOT back active publications
  for multiple commits; historical publications require distinct immutable
  payload paths.

- **REQ-19 — Same-commit maintenance repair:** Repair of a published snapshot
  MUST detach or flush its torrent session before mutation, remain pinned to the
  published commit, repair only recorded damaged paths, and validate repaired
  full-file and torrent hashes against the existing publication. Successful
  repair MUST update only affected metadata and resume the same torrent without
  scanning unchanged files. Neither model-mirror nor its supported backend may
  reread unchanged payload solely to rebuild publication or seed state when
  authoritative verified-piece state is available. A mismatch MUST leave
  seeding stopped. `repair --update` MUST remain blocked until retirement.

### Joining And Trust

- **REQ-20 — Native join, external handoff, and hostile input handling:**
  Model-mirror MUST accept magnet URIs and torrent files through a native join
  command. It MUST also accept a corresponding payload downloaded by an
  external client and pass it through the same validation and finalization
  path. Client-neutral handoff output MUST identify a safe staging destination
  and the exact follow-up import command without requiring client-specific
  integration. Native join SHOULD stage on the archive filesystem and
  prioritize the publication descriptor once metadata is available. All paths,
  sizes, schema, and identity MUST be validated before canonical import.
  Absolute paths, traversal, collisions, unsafe symlinks, reserved-path
  conflicts, and unreasonable resource claims MUST be rejected or quarantined.

- **REQ-21 — Local import:** Join MUST support safe resume, refuse to overwrite a
  different snapshot at the canonical path, and finalize into normal local
  archive metadata rather than copying another host's operational state.
  Finalization SHOULD use same-filesystem rename and backend path rebinding
  instead of a second full payload copy. Failed joins MUST remain visibly
  incomplete and resumable. Upstream unavailability MUST NOT prevent
  finalization as a usable torrent-verified archive. Continued seeding MUST be
  controlled by an explicit option or documented configuration policy.

- **REQ-22 — Authenticity boundary:** Torrent piece verification MUST be
  presented as content consistency, not independent upstream authenticity.
  Join MUST record content state, publication trust, upstream provenance, and
  upstream availability separately. An exact user-supplied magnet or torrent
  MAY be recorded as an explicit publication trust anchor, and a detached
  attestation MAY provide an additional trust root. When upstream is available,
  normal pinned-upstream verification SHOULD be supported; when it is
  unavailable, a trusted publication MUST remain usable without a second full
  read. An untrusted publication MAY be recovered but MUST NOT be described as
  upstream-verified.

### Existing Archive Coverage Upgrade

- **REQ-23 — Explicit complete upgrade:** Model-mirror MUST provide
  `upgrade REPO`, `upgrade --all`, and `upgrade --all --dry-run`. A successful
  upgrade MUST calculate and persist complete hash coverage for the default
  publication profile, operating in place without changing payload bytes or
  resolved commit. `--dry-run` MUST report detected formats, reusable metadata,
  files requiring reads, and bytes requiring reads.

- **REQ-24 — Backward-compatible in-place upgrade:** Upgrade MUST reuse
  trustworthy existing full-file and profile-specific hashes and read every
  file still missing required coverage. A schema-1 archive MUST remain
  supported before and after coverage is added; torrent support alone MUST NOT
  require a structural manifest rewrite. Known headerless `.manifest` and
  `.checksums` formats SHOULD be parsed when unambiguous and MUST NOT be deleted
  merely because the current reader cannot consume them. Malformed, unknown, or
  identity-inconsistent metadata MUST fail before overwrite.

- **REQ-25 — Incremental enrichment policy:** Upgrade MUST fill missing coverage
  incrementally and resumably until complete. Repair, download, and full
  verification SHOULD retain torrent hashes for files they already read, but
  ordinary cached verification and repair MUST NOT trigger an implicit
  payload-wide upgrade. Publication MUST invoke the same upgrade mechanism to
  complete any remaining required coverage. Upgrade MUST reconstruct a missing
  or stale authoritative snapshot description from trustworthy evidence or
  fail, and MUST NOT alter identity-bearing files in an active publication.

### Interface, Compatibility, And Quality

- **REQ-26 — CLI and configuration:** Torrent and upgrade features MUST use
  discoverable model-mirror commands, documented exit statuses, the existing
  YAML configuration surface where practical, and automation-safe behavior.
  Expensive operations MUST report files, bytes, and live progress. Errors MUST
  identify the snapshot and path and print an exact safe next command when one
  exists.

- **REQ-27 — Operational status:** Model-mirror status MUST distinguish at least
  unpublished, published, seeding, stopped, maintenance, unhealthy, retired,
  update-available, upgrade-available, and partial-coverage states. It MUST
  expose resolved commit, publication profile, both infohashes, schema and
  coverage state, content verification, publication trust, upstream provenance
  and availability, managed-versus-external client mode, and
  desired-versus-observed backend status without making unrelated archives
  unreadable. Runtime state owned by an external client MUST be reported as
  external or unknown rather than inferred as model-mirror-managed.

- **REQ-28 — Client and upgrade compatibility:** Standard torrent clients MUST
  be able to seed an emitted torrent from the verified snapshot or download its
  payload to model-mirror's declared staging destination while ignoring
  model-mirror metadata. Model-mirror MUST NOT require a particular external
  client for these handoff workflows. It MUST use a versioned publication
  profile rather than unversioned library defaults, support tracker or web-seed
  changes and detached attestations without changing swarm identity, and keep
  existing non-torrent commands and supported older archives usable without
  mandatory migration.

- **REQ-29 — Verification quality:** Implementation work MUST retain the
  repository's 100% statement and branch coverage gate. Deterministic torrent
  fixtures, legacy-upgrade fixtures, lifecycle failure tests, selective-I/O
  tests, hostile join tests, and at least one independent client or library
  check MUST provide traceability to these requirements through the scenarios
  below.

## Required State Model

These lifecycles are explanatory; their guarantees are carried by REQ-13
through REQ-22.

```text
clean, unfenced
    │ publish
    ▼
published@commit ───────────── upstream moves
    │                               │
    │                               ▼
    │                     published@commit
    │                     update-available
    │
    │ damage
    ▼
maintenance@commit
    │ repair and verify same commit
    ▼
published@commit

published@commit
    │ explicit retire
    ▼
clean, unfenced
    │ explicit update
    ▼
clean@new-commit
```

The publication fence is persistent model-mirror state, not a process holding
`.verification.lock` indefinitely.

```text
published@commit, desired=seeding
    │ host or backend restart
    ▼
reconciling@commit, fenced
    ├── fingerprints match ──► seeding@commit
    └── fingerprint changed ─► unhealthy@commit, fenced
```

Join state is multi-dimensional rather than one overloaded clean/dirty label:

| Facet | Example values |
| --- | --- |
| Content | incomplete, torrent-verified |
| Publication trust | trusted-infohash, trusted-attestation, untrusted |
| Upstream provenance | upstream-verified, not-upstream-verified |
| Upstream availability | available, unavailable, unknown |

## Acceptance-Test Traceability

These are test scenarios, not additional requirements. Each scenario will
normally contain several unit or integration cases.

| Scenario | Covers | Demonstrates |
| --- | --- | --- |
| AT-01 Deterministic identity | REQ-01–REQ-04 | Same commit through different requested revisions converges; another commit diverges; local and tracker fields do not affect identity. |
| AT-02 Descriptor and archive metadata | REQ-05–REQ-09 | The canonical descriptor is hashed metadata rather than payload; local state is excluded; schema-1 remains readable; separate coverage writes are atomic. |
| AT-03 Streamed torrent hashes | REQ-10–REQ-12 | Fresh, resumed, retried, truncated, and concurrent streams match an independent reread. |
| AT-04 Publication | REQ-13–REQ-16 | Clean publication is idempotent, records both hashes, emits a recovery record, establishes the fence, and registers without copying or rereading payload. |
| AT-05 Seeder restart and failure | REQ-15–REQ-18, REQ-27 | Graceful and abrupt host/backend restart reconcile desired seeds without an explicit command or payload-wide read; changed fingerprints stop safely; the fence persists. |
| AT-06 Update and retirement | REQ-17–REQ-18, REQ-26–REQ-27 | Detection remains available, update is blocked, stop differs from retire, and retirement enables a new commit. |
| AT-07 Selective repair | REQ-10–REQ-12, REQ-16–REQ-19 | One damaged file is repaired and the same torrent resumes without model-mirror or its backend reading unchanged payload solely to reconstruct state. |
| AT-08 Offline join and import | REQ-20–REQ-22 | Native joins and external-client handoffs validate and finalize without a second copy when upstream is unavailable; content, trust, and provenance states remain distinct. |
| AT-09 Hostile join | REQ-20–REQ-22 | Traversal, collisions, unsafe links, malformed descriptors, excessive claims, and untrusted identity are rejected safely. |
| AT-10 Archive coverage upgrade | REQ-05, REQ-08–REQ-12, REQ-23–REQ-25 | Schema-1 and known legacy archives remain supported; explicit upgrade produces complete coverage, reuses trustworthy data, resumes partial work, and preserves unknown metadata on failure. |
| AT-11 Crash recovery | REQ-09, REQ-11–REQ-16, REQ-19, REQ-21, REQ-23–REQ-25 | Interrupted hash, publish, repair, join, upgrade, and hard restart operations leave old valid or resumable state. |
| AT-12 CLI and status | REQ-14, REQ-23, REQ-26–REQ-27 | Help, exit statuses, progress, state labels, and recovery commands remain automation-safe and useful. |
| AT-13 Compatibility | REQ-03–REQ-04, REQ-06–REQ-07, REQ-14–REQ-15, REQ-28–REQ-29 | Golden torrents remain stable; independent clients seed and download using the standard artifacts while ignoring the custom descriptor; magnet metadata preserves it; detached attestations do not alter identity. |

## Delivery Slices

1. **Protocol and backend gate:** Build a small libtorrent-backed prototype and
   compare its externally managed fallback against the same fixtures. Prove
   hybrid determinism, custom `info` metadata round-tripping, magnet metadata
   recovery, graceful and abrupt restart, and same-commit repair without
   payload-wide rereads. Measure actual file-read I/O rather than relying only
   on backend status.
2. **Metadata and hashing foundation:** Add versioned per-profile coverage,
   complete upgrade commands, and stream accumulation while keeping schema-1
   manifests supported in place.
3. **Deterministic publication:** Add the portable descriptor, hybrid torrent
   creation, detached recovery record, publication record, and fence.
4. **Seeding lifecycle:** Integrate the selected backend, automatic restart
   reconciliation, one-command publication, status, maintenance repair,
   retirement, and update blocking.
5. **Offline join and import:** Add magnet/torrent acquisition, hostile-input
   validation, trust recording, upstream-unavailable finalization, and optional
   continued seeding.

## Open Design Questions

The initial implementation resolved the former backend, piece-length, coverage,
and join-default questions: direct libtorrent passed the gate; profile
`hybrid-v1-v2-1` uses the documented adaptive power-of-two rule; coverage is
separate versioned JSON; and join seeds only with `--seed`.

1. Which detached signing and trusted-catalog mechanisms belong in the first
   release versus a later extension?
2. When should HTTP web-seed support be added?
3. Should later commit-addressed storage retain and seed multiple commits?
