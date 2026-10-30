# Sentinel V7 multi-box runtime implementation

Status: development implementation on branch `feature/sentry-v7-multibox`.
Analytics baseline: immutable V6 dev39 commit `9619cf6` and image digest
`sha256:aae3a9ed26a83b9e93958b74dcd02dde7a232ff85d1301f99014243dda9b0a29`.

## Scope

V7 preserves dev39 decoding, models, motion gating, tracking, adaptive
production policy and use-case producers. It changes the desired-state marker,
wire schema and delivery subsystem. Cross-camera identity, rules, incidents and
semantic retrieval remain Management responsibilities.

One Kubernetes Deployment runs exactly one logical `deployment_id`. Multiple
deployments on one physical box run as separate pods with separate tokens,
ConfigMaps, Secrets and `/state` PVCs.

## Runtime topology

```text
one logical deployment / pod
  shared models and fleet supervisor
    camera process A --+
    camera process B --+--> /state/sentinel_v7/outbox.sqlite
    camera process N --+          |
                                 +--> one permanent deployment sequence
                                 +--> local_v7 or remote_v7 HTTPS delivery
                                 +--> /api/v7/ingest/*
```

Camera processes remain failure-isolated. They coordinate delivery through
SQLite WAL transactions. Sequence allocation, upload claims, dependency state,
watermarks, skips and stream identity are deployment-wide, not camera-wide.

## Required runtime configuration

| Variable | Meaning |
|---|---|
| `SENTINEL_EDGE_TOKEN` | Deployment bearer token. |
| `SENTINEL_BOX_ID` | Enrolled physical-box UUID. |
| `SENTINEL_PLACEMENT_GENERATION` | Positive current assignment generation. |
| `SENTINEL_DELIVERY_MODE` | Exactly `local_v7` or `remote_v7`. |
| `SENTINEL_BOX_CREDENTIAL` | Required only for `remote_v7`; forbidden for `local_v7`. |
| `SENTINEL_INGEST_BASE_URL` | Credential-free HTTPS Management base URL. |
| `SENTINEL_INGEST_CA_FILE` | Site CA bundle; defaults to `/run/secrets/sentinel/ingest/site-ca.crt`. |
| `SENTINEL_OUTBOX_MAX_BYTES` | Per-deployment cap, bounded to 1–2 GiB. |
| `SENTINEL_OUTBOX_RETENTION_DAYS` | Defaults to seven days. |

The image sets `SENTINEL_V7_REQUIRE_DELIVERY=true`. Missing or incomplete V7
delivery configuration fails the camera worker closed; it is never silently
downgraded to V6 or an unsequenced path.

## Persistence and ordering

The database is `/state/sentinel_v7/outbox.sqlite` with WAL, synchronous FULL,
foreign keys and a 30-second busy timeout. One transaction validates and stores
an entire package and allocates its permanent sequences in this order:

1. Observation.
2. Evidence objects.
3. Embedding objects.

Evidence bytes are written to a temporary file, flushed and fsynced, atomically
renamed, and the evidence directory is fsynced before the database package is
committed. Orphan files can therefore be identified after a crash.

An evidence upload cannot be claimed before its observation is acknowledged.
An embedding cannot be claimed before its evidence is acknowledged. Up to
eight unrelated objects can be leased across all camera processes.

## Stream lifecycle

Every uploader bootstraps with `GET /api/v7/ingest/stream`. The first valid
server-generated `stream_id` and both watermarks are persisted. A changed stream
ID, mismatched deployment, box, generation, sequence invariant or backward
watermark pauses that uploader instead of deleting data.

Placement changes retain `deployment_id`, `stream_id`, sequence and watermarks.
The new box uses an incremented generation. If local state is unavailable, the
runtime starts at `highest_received_sequence + 1` and creates ordered
`object_unreadable` skip markers for unresolved positions above the contiguous
watermark.

## Skip and retention behavior

The runtime owns skip cascading. Skipping an observation marks its evidence and
embeddings; skipping evidence marks its embeddings. Each affected sequence is
submitted separately. Offline eviction commits and fsyncs `skip_pending` before
deleting bytes.

Retention uses durable `outbox_created_at`, never the event `observed_at` clock.
Under pressure it evicts evidence first and whole packages second, retaining
the durable skip metadata required to advance Management's watermark.

## V7 payload behavior

Observation, evidence and embedding meanings are unchanged from V6, but their
wire `schema_version` is `7.0`. Routes are exclusively:

```text
POST /api/v7/ingest/events
POST /api/v7/ingest/evidence
POST /api/v7/ingest/embeddings
POST /api/v7/ingest/skip
GET  /api/v7/ingest/stream
```

The updated desired-state validator accepts 1–50 cameras, requires
`edge_id == deployment_id`, and rejects more than 64 aggregate configured FPS
for one deployment. Physical-box aggregate admission across separate pods is a
Management scheduler responsibility.

## Production policy

The implementation pins the current `SENTINEL_V6_PRODUCTION_POLICY.md` content
without changing its gates. Its SHA-256 is declared in the image label
`io.apexfabric.production-policy.sha256`. V7 delivery does not relax tracking,
ANPR, face/body/gait, presence, count, dwell-support, scene or hazard policy.

## Build

```bash
./scripts/build_sentinel_v7_image.sh
```

Default development tag:

```text
ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.10.30-v7-dev1
```

Local build result on 2026-10-30:

```text
image ID: 2ff3799c04ff350737d6d1019ce83c53d2447295687032e2c24f263d0ffac49d
contract/wire version: 7.0
qualification status: development
```

The build validates the embedded V7 schemas and examples, imports the V7
analytics modules, verifies the TCP/MPEG-TS receiver plugins, verifies that the
VAAPI plugin library is installed, and compiles the runtime. The VAAPI decoder
element itself must be verified at runtime with the Intel render device mounted.

This is a development tag until mocked ingest, restart, power-loss/PVC,
local/remote delivery, Intel hardware, live-camera and scale qualification are
complete. The existing V6 dev39 image and main branch remain unchanged.
