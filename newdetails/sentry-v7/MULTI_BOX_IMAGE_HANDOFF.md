# CV image handoff: multi-box delivery 7.0

This handoff defines the runtime behavior required for a CV image to claim
Sentry V7 multi-box delivery. The desired-state and observation, evidence, and
embedding payload schemas remain at `7.0`; this revision changes delivery only.

## Stream and credentials

Each `deployment_id` owns one permanent sequence stream and one stable,
server-generated `stream_id`. The control plane derives `deployment_id` from
the deployment bearer token. `box_id` and `placement_generation` identify and
fence the current writer, but never create a new stream or reset its receipts,
watermarks, or next sequence. The control plane derives the remote box from
`X-Sentry-Box-Credential` and verifies that the active CV-capable box currently
owns the deployment.

`deployment_id` is the immutable Management key used verbatim in desired
state, observations, tokens, receipts, and acknowledgements. It is the existing
1–200 character `[A-Za-z0-9._-]` identifier, not a second UUID. `edge_id` is a
deprecated compatibility alias and must equal `deployment_id`.

Sequence allocation starts at 1 for each deployment. Two deployments on one
box therefore both use sequence 1 without sharing counters or watermarks.
Each logical deployment is exactly one runtime process/Kubernetes Deployment
with its own token, ConfigMap, Secret, and PVC. A process never hosts multiple
deployments; a physical box hosts several deployments as separate pods.

## Required environment and trust

Remote runtimes require:

| Variable | Requirement |
|---|---|
| `SENTINEL_EDGE_TOKEN` | Deployment bearer token. |
| `SENTINEL_BOX_ID` | Enrolled box UUID. |
| `SENTINEL_BOX_CREDENTIAL` | Required only in `remote_v7`; never selects the protocol. |
| `SENTINEL_DELIVERY_MODE` | Required explicit value: `local_v7` or `remote_v7`. Missing/unknown fails closed. |
| `SENTINEL_PLACEMENT_GENERATION` | Positive assignment generation bound to the token and every request. |
| `SENTINEL_INGEST_BASE_URL` | Site-CA HTTPS control-plane URL. |
| `SENTINEL_INGEST_CA_FILE` | `/run/secrets/sentinel/ingest/site-ca.crt`. |
| `SENTINEL_OUTBOX_MAX_BYTES` | Per-deployment cap; default 2 GiB, allowed 1–2 GiB. |
| `SENTINEL_OUTBOX_RETENTION_DAYS` | Per-deployment age limit; default 7. |

The runtime uses HTTPS only, trusts the supplied site CA specifically for this
connection, performs hostname validation, and rejects insecure HTTP and
certificates outside that trust. Server certificates may rotate under the same
CA without a restart. A CA rotation requires an overlapping CA bundle followed
by a pod restart. The runtime must not connect to Redis.

`SENTINEL_BOX_CREDENTIAL` is read from the pod environment at startup. When
management re-issues that credential, restart the deployment pod to load it;
the running process does not hot-reload environment variables.

## Durable per-deployment outbox

Each deployment has one persistent `/state` volume and a transactional SQLite
store at `/state/sentinel_v7/outbox.sqlite`. It uses WAL mode,
`synchronous=FULL`, and foreign keys. Before its first upload attempt, one
transaction increments the next sequence and stores the object and its
parent/child relationship.
There must never be a used number without a record or a record without a number.

The qualified PVC is `ReadWriteOnce`, `Filesystem` volume mode, using the
K3s `local-path` storage class on ext4 or XFS with working POSIX locks and
durable fsync. NFS and other network filesystems are prohibited unless they
are separately qualified for SQLite WAL locking and durability. Every release
must pass power-loss and node-reboot WAL recovery on its exact storage class.

Each record contains:

```text
sequence, object_type, object_id, delivery_payload_sha256,
payload or evidence path, parent_object_id, state, attempts, next_attempt_at,
last_error, outbox_created_at
```

`object_type` is `observation`, `evidence`, or `embedding`; `state` is
`pending`, `acked`, `dead_letter`, `skip_pending`, `skipped`, or
`resolved_by_remote_receipt`.

For one detection, allocation order is observation, evidence, then embedding.
Sequences are never reused or renumbered.

### Exact payload hashing

- Observation: SHA-256 over the exact `application/json` request body bytes.
- Embedding: SHA-256 over the exact `application/json` request body bytes.
- Evidence `content_sha256`: SHA-256 over raw image bytes.
- Evidence `delivery_payload_sha256`: SHA-256 over canonical metadata bytes,
  one `0x00` separator byte, then the raw image bytes. Multipart boundaries,
  part headers, and transport framing are excluded.

Evidence metadata uses Sentry canonical JSON v1: UTF-8 bytes equivalent to
`json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
allow_nan=False)`. Array order is preserved. This makes retries with different
multipart boundaries produce the same digest.

The control plane recomputes this digest from the received object before its
receipt lookup. It never hashes the raw multipart body and never trusts a
client-supplied digest as the duplicate-detection authority.

For evidence, the control plane writes and fsyncs a temporary file, verifies
the content hash, and atomically renames it to the deterministic
organization/site/box path before inserting the database row. An identical
retry safely overwrites the same bytes. A bounded reconciliation pass removes
stale temporary files and deterministic files that have no committed evidence
row after the configured grace period.

## Upload protocol

Allocate in ascending sequence order with no more than eight requests in
flight. Unrelated observation packages may upload concurrently, but within a
package the observation must be acknowledged before evidence and evidence
must be acknowledged before embeddings. An object never overtakes its parent.
Object requests use:

```http
Authorization: Bearer <deployment token>
X-Sentry-Delivery-Mode: remote_v7
X-Sentry-Box-Credential: <box credential>
X-Sentry-Placement-Generation: <integer>
X-Sentry-Stream-Sequence: <integer>
```

The only remote endpoints are:

```text
POST /api/v7/ingest/events
POST /api/v7/ingest/evidence
POST /api/v7/ingest/embeddings
POST /api/v7/ingest/skip
GET  /api/v7/ingest/stream
```

Before sending any object, the runtime calls the stream endpoint, validates
the returned deployment, box, and placement generation, and stores the first
valid `stream_id` and watermarks in SQLite. On restart it repeats this bootstrap.
The `stream_id` remains unchanged across placement generations. If an
initialized runtime receives a different stream ID, it pauses uploads and
local deletion and requires explicit recovery rather than silently adopting it.

Every successful response uses the common acknowledgement shape:

```json
{
  "status": "accepted",
  "sequence": 4,
  "deployment_id": "north-yard",
  "box_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  "placement_generation": 3,
  "stream_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  "highest_contiguous_sequence": 3,
  "highest_received_sequence": 6
}
```

`status` is `accepted` for the first write and `duplicate` for an identical
retry. The contiguous watermark is the largest N for which every sequence from
1 through N has either a receipt or skip tombstone.
For `GET /api/v7/ingest/stream`, `status` is `accepted` and `sequence` equals
`highest_received_sequence` (or 0 for a new stream).

Before accepting an acknowledgement, the runtime verifies that deployment,
box, generation, and stream match the running process; contiguous is not above
received; the acknowledged sequence is not above received; and neither
watermark moved backwards. A failure is a protocol error and pauses deletion.

## Idempotency, errors, and retry

| Result | Runtime action |
|---|---|
| New sequence: `200 accepted` | Mark the record acknowledged. |
| Same sequence and bytes: `200 duplicate` | Mark the record acknowledged. |
| Same sequence, different bytes: `409` | Store bounded diagnostic metadata, query stream state, mark `resolved_by_remote_receipt`; never skip or renumber the occupied sequence. |
| Parent missing: `425` | Retry defensively. |
| Validation failure: `400` or `422` | Dead-letter, then send a skip marker. |
| `401` or `403` | Pause upload; continue capture and retain `/state`. |
| Network error, `408`, `429`, or `5xx` | Retry with bounded exponential backoff and jitter. |

Backoff starts at 250 ms and caps at 60 seconds. Honor `Retry-After` only up to
60 seconds.

A different-payload `409` means Management already has a durable receipt for
that sequence. Once the stream query confirms that position is covered, the
runtime sets the local record to `resolved_by_remote_receipt`, deletes payload
bytes only under the normal contiguous-watermark rule, and continues. Sending
a skip for the occupied sequence would conflict and is forbidden.

## Skip markers

`POST /api/v7/ingest/skip` accepts `sequence` and exactly one of:

- `validation_failed`
- `evidence_expired`
- `object_unreadable`
- `outbox_overflow`
- `retention_expired`

A tombstone fills the sequence for watermark advancement. The same reason is
idempotent; a sequence that already has a receipt returns `409`.

Skip cascading is exclusively a runtime responsibility because the skip body
contains no object or parent identity. The runtime sends one skip request for
every affected sequence, using the same reason:

- Skipping an observation also skips its evidence and embeddings.
- Skipping evidence also skips its embeddings; the observation remains.
- This applies to validation failures and whole-record eviction.

The server never infers child sequences. Because the runtime explicitly
tombstones every child sequence, a later upload retry
for that child returns `200 duplicate`. Receipt/tombstone lookup happens before
parent validation. An untombstoned child whose parent domain row is absent
returns `425`; the sequence-and-reason-only skip format intentionally carries
no parent object identity from which the server could infer anything else.

## Restart, recovery, and deletion

Normal restart resumes from `/state`. If one deployment loses `/state`, call
the stream endpoint, set its next sequence to
`highest_received_sequence + 1`, then send `object_unreadable` skips for every
sequence from `highest_contiguous_sequence + 1` through
`highest_received_sequence`. Treat `409` for one of these recovery skips as
success because it means the sequence already has a receipt. Log a local
recovery event, add no wire field, and do not affect another deployment.

On reassignment, Management increments `placement_generation`, binds a new
token to the new box, and rejects the prior writer. The new runtime bootstraps
the existing permanent stream, preserves `stream_id`, continues after the
server's received watermark, and repairs holes through the lost-state
procedure. Placement never restarts sequence numbering at 1.

After every acknowledgement, delete `acked` or `skipped` records and evidence
only when their sequence is at or below `highest_contiguous_sequence`. Retain
everything above the watermark except limit eviction. While disconnected, one
SQLite transaction changes the record to `skip_pending`, stores the reason,
commits and fsyncs it, and only then deletes payload bytes. A crash may leave
extra bytes behind, but must never leave deleted bytes without a durable skip
marker. After reconnect,
send those markers in sequence order and change each accepted marker to
`skipped`.

### Dead-letter storage and cleanup

Dead-letter metadata is stored per deployment under `/state/dead-letter`.
Before its skip is acknowledged, the payload still counts toward the 2 GiB
cap. After skip acknowledgement, delete payload/evidence bytes and retain only
sequence, object type and ID, payload hash, reason, last error, attempts,
creation time, and skip time. This metadata continues to count toward the cap.

Keep at most 1,000 dead-letter metadata records and no record older than seven
days, removing oldest metadata when either bound is reached. Delivery records
may therefore reach `skipped` and be deleted through the watermark while their
bounded diagnostic metadata remains; dead letters are never retained forever.

## Edge and central retention

The runtime enforces a per-deployment 2 GiB default cap, configurable only from
1 to 2 GiB, and a seven-day age limit measured from durable
`outbox_created_at`. `observed_at` remains the event timeline and is never the
only storage-expiration clock. The PVC request is not the limit.

When usage exceeds the cap, evict oldest first until usage is at most 90%:

1. Remove oldest evidence bytes, mark their delivery records and embeddings
   `skip_pending` with `evidence_expired`, and retain observations.
2. If still above target, remove oldest whole-record payloads and mark the
   observation, evidence, and embeddings `skip_pending` with `outbox_overflow`.

Eviction does not require connectivity. The runtime sends these skips in
sequence order after reconnect and then changes them to `skipped`.

Records older than seven days use `retention_expired`. Upload evidence
immediately whenever connected.

Placement reserves the sum of deployment caps, 20% headroom, and 20 GiB free
space. A deployment and physical box admit at most 50 configured cameras and
64 aggregate configured FPS by default. Kubelet DiskPressure and 15% node/image filesystem eviction
thresholds provide the continuous physical-box guard.

The control plane retains at most 15 GiB or seven days per site control-plane
evidence store, whichever limit is
reached first, deleting oldest events by `observed_at`. It removes evidence
files before database rows and never deletes stream watermarks.

## Control-plane-local V7

`local_v7` is accepted only when the token is assigned to the control-plane
box. It omits the box credential but still sends the sequence and placement
generation and uses the same receipts, watermarks, and acknowledgement shape.
`remote_v7` always requires the box credential. Credential absence never
changes modes. Legacy unsequenced behavior remains only on the V5 ingest paths
for old V5/V6 images.

## Unchanged V6 runtime behavior

V6 readiness and camera fault isolation remain required. `/readyz` reports the
runtime ready when desired state, shared models, the inference scheduler, and
per-camera supervisors/recovery loops are operational; it does not require
every camera to be healthy. One failed camera must not stop healthy cameras.
Camera health remains `healthy`, `degraded`, or `unavailable`.

## CV-team image requirements

The CV image must implement and qualify every runtime behavior above. Its image
metadata must declare the exact capability set below, but the CV team does not
edit control-plane migrations or release bundle pins.

```json
{
  "hardware_profile": "intel-285h",
  "wire_schema_version": "7.0",
  "multi_box_delivery": "7.0"
}
```

Never claim this capability on dev15 or another unqualified image.

## Control-plane release steps

Add and apply the V7 stream-state and receipt/tombstone database migration.
Deploy the `/api/v7/ingest/*` routes and verify they operate with Redis absent.
Apply the placement-generation migration, expose V7 ingest through the
site-CA TLS service, mount `sentinel-site-ca`, and inject the explicit delivery
mode and current placement generation.

Register only a newly qualified immutable image with contract version `7.0`
and capabilities containing:

```json
{
  "hardware_profile": "intel-285h",
  "wire_schema_version": "7.0",
  "multi_box_delivery": "7.0"
}
```

In the same release change, update `CV_IMAGE` and its expected digest together
in `scripts/build-images.sh`.

## Qualification gates

- Disconnect the CV node for ten minutes while capture continues.
- Confirm backlog survives both a pod restart and node reboot.
- Kill the runtime between SQLite sequence allocation and object commit; verify no sequence is lost.
- Kill it before and after the `skip_pending` transaction commits; verify bytes are never lost without a durable marker.
- Restore connectivity and drain in order at twice production rate with no duplicates.
- Repeat an acknowledged object; the same bytes return `duplicate` with the same watermark.
- Reuse a sequence with different bytes; dead-letter without renumbering.
- For a different-payload `409`, query stream state, set `resolved_by_remote_receipt`, send no skip, and continue past the occupied sequence.
- Bootstrap with `GET /stream`, persist its stream ID, and pause uploads/deletion when a later response returns an unexpected stream ID.
- Increment placement generation and move the writer to another box; verify the stable stream ID and sequence/watermarks continue while the old token is rejected.
- Exercise event, skip, and stream requests in `local_v7`; verify the box credential is absent and the same sequence and acknowledgement rules apply.
- Power-cycle and reboot the node while SQLite WAL contains committed work on the qualified `local-path` ext4/XFS storage; verify full recovery.
- Run two deployments on one box from sequence 1 with independent watermarks and restart behavior.
- Force validation failure and confirm a skip advances the watermark while retaining the dead letter.
- Evict while disconnected; confirm payload bytes are removed, records become `skip_pending`, and ordered skips are sent only after reconnect.
- Delete one deployment's `/state` with holes below received; resume at received plus one and send `object_unreadable` skips for every hole.
- Skip an observation and verify its evidence and embeddings are skipped; skip evidence and verify its embedding is skipped; a child retry at its cascade-tombstoned sequence returns `200 duplicate`.
- Verify the runtime sends one skip request for each parent and child sequence; the server infers none.
- Fill a 2 GiB outbox while disconnected; verify evidence-first eviction, then whole-record eviction, cascade behavior, and skips.
- Age records beyond seven days and verify `retention_expired` skips.
- Exceed 1,000 dead letters and age one beyond seven days; verify oldest metadata cleanup and no retained payload bytes after skip.
- Retry identical evidence using different multipart boundaries; verify the same digest and duplicate acknowledgement.
- Revoke the box credential; uploads pause while capture and local retention continue.
- Attempt remote upload with a missing credential, wrong mode, stale placement generation, wrong box, and a replayed old generation; each must fail closed.
- Validate acknowledgement identity and numeric invariants and reject a backward or cross-stream acknowledgement.
- Verify HTTPS hostname validation, rejection of HTTP and system-only trust, server-certificate rotation, and overlapping-CA rotation with pod restart.
- Fill aggregate camera/FPS admission and the physical-box free-space reserve; verify placement rejects additional work.
- Confirm evidence is written only to the control-plane evidence volume.

## Not covered

- Automatic movement and `/state` transfer between boxes. Every future move
  must increment placement generation and issue a new token before startup.
- Never run the same deployment on two boxes concurrently, including during
  operator recovery. The generation fence rejects stale uploads but does not
  replace workload termination.
- Mutual TLS and client certificates.

## Changes from the batch version

| Previous draft | V7 delivery 7.0 |
|---|---|
| One sequence counter covered a physical box. | Every deployment owns its sequence and watermark. |
| Delivery grouped objects and required a separate finalization request. | Each object acknowledgement advances stream state immediately. |
| Permanent local failure stalled progress. | A validated skip tombstone fills the sequence. |
| A transient cache participated in acknowledgement delivery. | PostgreSQL receipts and `/state` are authoritative. |
| Multiple endpoint and capability generations were described. | Only `/api/v7` and capability `7.0` are valid. |
