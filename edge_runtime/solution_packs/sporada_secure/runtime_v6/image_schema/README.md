# Sentry V6 CV contract package

V6 is a complete edge/Management boundary. Desired state, observations,
evidence metadata, embeddings and acknowledgements all carry
`schema_version: "6.0"`, and delivery uses `/api/v6/ingest/...` routes.

V6 is not wire-compatible with 5.2. Any persisted 5.2 outbox must be drained
with the previous runtime before upgrading; partially acknowledged records
must never be silently relabelled as V6.

This directory is embedded in the runtime image at
`/opt/traffic-pilot/image_schema`. It includes:

- `sentry-v6-contract.yaml`: complete edge/management boundary and runtime behavior.
- `desired-state.schema.json` and `desired-state.example.json`.
- `observation.schema.json` and `observation-examples.json`.
- `evidence.schema.json` and `evidence.example.json`.
- `embedding.schema.json`, `acknowledgement.schema.json`, and
  `acknowledgement.example.json`.

The source repository's `newdetails/sentry-v6` directory contains the package
validator and immutable-image release metadata.

The runtime also defines camera fault isolation. A configured camera that
cannot decode becomes `unavailable`, retries in its own supervisor, and leaves
healthy cameras processing. `/readyz` reports the runtime ready when the
HTTP/runtime supervisor and per-camera recovery loops are operational; it
does not require every camera or every camera-local model process to be healthy.

One desired-state revision may configure 1 through 50 cameras. Measured FPS
and memory still depend on stream resolution and enabled analytics.

The recent management changes are compatible consumers of this behavior:
camera-health state uses the existing `healthy`, `degraded`, and `unavailable`
vocabulary; stale health heartbeats are shown as unavailable; and the dashboard
shows the reason and last reliable observation for the affected camera.

Only an image whose labels declare contract and wire schema `6.0`, and whose
digest is recorded in `release.yaml`, may claim V6 conformance.
