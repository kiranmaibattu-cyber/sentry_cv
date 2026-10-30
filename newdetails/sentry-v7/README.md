# Sentry V7 CV contract package

Status: ready for CV-team implementation; no image is qualified or released yet.

V7 retains the desired-state, observation, evidence, and embedding wire schemas
while adding authenticated, ordered delivery for local and remote CV boxes. Each deployment
owns one permanent sequence stream, fenced current writer, and transactional SQLite
outbox on its own `/state` PVC.

This directory is self-contained for CV-team implementation and validation:

- `sentry-v7-contract.yaml`: complete edge/management boundary and delivery behavior.
- `desired-state.schema.json` and `desired-state.example.json`.
- `observation.schema.json` and `observation-examples.json`.
- `evidence.schema.json` and `evidence.example.json`.
- `embedding.schema.json`.
- `acknowledgement.schema.json`: common per-object, skip, and stream response.
- `multi-box-delivery.schema.json` and example: request headers, acknowledgements,
  skip, stream recovery, and two independent deployments on one box.
- `MULTI_BOX_IMAGE_HANDOFF.md`: runtime implementation and qualification gates.
- `validate_contract.py`: schema, cross-reference, and protocol consistency checks.
- `release.yaml`: runtime-image publication status.

Run the static checks from the repository root:

```bash
python3 solution-packs/sentry-v7/validate_contract.py
```

## Runtime compatibility

`SENTINEL_DELIVERY_MODE` explicitly selects `local_v7` or `remote_v7`; missing
or unknown values fail closed. Both modes send `X-Sentry-Stream-Sequence` and
`X-Sentry-Placement-Generation`. Remote mode additionally requires the box
credential. The deployment token identifies the permanent stream while the current assignment
and placement generation fence its writer;
no caller-supplied identity is authoritative. Objects are persisted
transactionally before upload and deleted only through the acknowledged
contiguous watermark.

Runtime startup bootstraps with `GET /api/v7/ingest/stream` and persists the
stable stream ID. Placement changes preserve that ID, sequence, receipts, and
watermarks. A different-payload `409` is resolved locally as
`resolved_by_remote_receipt` after querying stream state; it is never skipped
or renumbered.

Local V7 remains sequenced and uses the same acknowledgement. Unsequenced
compatibility belongs only to older V5/V6 images on their legacy endpoints.

No existing runtime image claims V7 conformance. A new immutable image and
digest must pass the handoff qualification gates before registration with
`"multi_box_delivery":"7.0"`.
