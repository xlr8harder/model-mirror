# Torrent Backend Spike

Status: Experimental; direct libtorrent selected and integrated as the managed
backend.

Date: 2026-07-27.

## Decision

Use libtorrent 2.0.x directly for model-mirror's managed torrent backend.
Continue to emit ordinary `.torrent` and magnet artifacts so users can seed or
download with another client. Treat those external clients as unmanaged rather
than attempting client-specific lifecycle control. The managed interface and
torrent-specific metadata formats remain experimental.

The Python binding is an optional runtime extra:

```bash
pip install 'model-mirror-cli[torrent]'
```

It is also a development dependency so the deterministic metainfo and
independent-library tests run in the normal repository validation gate.

## Evidence

The local spike exercised Ubuntu's libtorrent 2.0.10 binding and the PyPI
libtorrent 2.0.13 wheel.

| Capability | Result |
| --- | --- |
| Hybrid v1/v2 creation | Passed; both `btih` and `btmh` were produced. |
| Hashed model-mirror descriptor | Passed; a custom `info` field changed swarm identity and remained parseable. |
| Magnet metadata exchange | Passed between two independent local sessions; the custom descriptor survived intact. |
| Verified seed registration | Passed with explicit `have_pieces` and `verified_pieces` state. |
| Registration payload I/O | Approximately 100 bytes of `/proc/self/io` `rchar` change and zero `read_bytes` change for a 6 MiB fixture. |
| Resume-data restart | Passed; the recreated seed did not reread the fixture. |
| Payload copy | None; `save_path` points at the parent of the existing verified archive. |

The spike used loopback sessions with DHT, LSD, UPnP, and NAT-PMP disabled so
network discovery did not obscure the metadata and I/O checks.

## Why Not Make qBittorrent The Managed Default?

qBittorrent remains a good user-selected external client and a possible later
adapter. Its Web API can add a torrent with `skip_checking`, but that is a
coarse trust switch. Direct libtorrent accepts explicit piece state, exposes
resume data, constructs hybrid torrents, and gives model-mirror enough control
to validate changed fingerprints before restoring a seed.

Model-mirror must never infer that an external client is healthy or running.
It can still protect a published archive with its own publication fence and can
validate a client-downloaded payload before local import.

## Implementation Boundary

The first foundation code provides:

- a versioned deterministic publication descriptor;
- an explicit adaptive piece-length rule;
- hybrid metainfo construction from the pinned snapshot file list;
- verified seed parameters that point at the existing payload;
- streaming v1 piece hashes and v2 Merkle roots validated against libtorrent;
- a generic accumulator hook on the existing download hashing writer.

The implementation now persists resumable per-file coverage, feeds the
accumulator from Xet and HTTP downloads, creates metainfo directly from saved
hashes, exposes publication/fence and native/external receive commands, and
reconciles durable seed intent. Tests cover graceful restart, abrupt-state
reconstruction, out-of-band mutation fencing, and selective same-commit repair.
