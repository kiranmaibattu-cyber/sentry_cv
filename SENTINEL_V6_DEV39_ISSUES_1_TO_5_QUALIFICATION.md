# Sentinel V6 dev39: Issues 1–5 Qualification

Date: 2026-10-30

Image: `ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.10.30-v6-dev39`

Local image ID: `d43d03ad200391a80f7f5038aca5fe0fed4481d2805bef8f610896b2e64a0cf6`

Local digest: `sha256:aae3a9ed26a83b9e93958b74dcd02dde7a232ff85d1301f99014243dda9b0a29`

Registry digest: `sha256:aae3a9ed26a83b9e93958b74dcd02dde7a232ff85d1301f99014243dda9b0a29`

## 1. Timestamp provenance

The active decoder now records decoded-frame receipt time as
`frame_received_at`. It does not manufacture `frame_observed_at` from that
value. Events therefore report `timestamp_source=edge_received_at` unless a
real upstream source timestamp is explicitly supplied.

Tests cover both cases:

- No source time: `observed_at` uses receive time and reports
  `edge_received_at`.
- Explicit source time: source time takes precedence and reports `source_pts`.

The live RTSPS qualification reported `edge_received_at`.

## 2. Parent-image metadata

The build now records both the exact parent reference and its image ID:

- Parent: `localhost/apexfabric-intel-traffic-runtime-base:intel-285h-2026.09.18-v2`
- Parent image ID: `1a7a4208cefe3567d03e9d2222a7711530b359861a579d936fb079af50f76c6d`

OCI labels:

- `io.apexfabric.parent-base`
- `io.apexfabric.runtime-base.digest`

## 3. Reproducible publication material

The repository contains the Dockerfile, exact build script, baked model files,
embedded schemas, canonical desired-state example, output/acknowledgement
examples, production policy and this qualification record. The release
manifest identifies the tested tag, image ID, local digest, validation counts
and remaining limitations.

Build command:

```bash
./scripts/build_sentinel_v6_image.sh
```

## 4. Geometry-only desired-state updates

Geometry is no longer part of the worker-restart signature. A geometry-only
revision is applied through `V6EventPipeline.reconfigure()`.

For changed or removed geometry, the runtime:

1. Marks affected open zone episodes `ended_unknown`.
2. Emits a material-change presence update where an owned presence exists.
3. Clears only affected zone/line/count/scene/hazard state.
4. Emits `camera_health` with reason `view_changed`.
5. Re-evaluates ownership against the new geometry.
6. Does not invent `zone_entry`, `zone_exit` or `line_cross` facts.

A live revision test preserved the camera worker PID and stream-session ID while
the loaded revision changed from 2 to 3. Unit tests additionally verify
presence continuity and the absence of synthetic transitions.

## 5. Trigger qualification and presence lifecycle

Presence lifecycle corrections:

- Motion-gate quiet frames do not age a presence.
- The missing timer begins only on an evaluated negative observation.
- At least two evaluated negatives are required.
- Tracker-retained predictions preserve association but do not refresh
  `last_seen` or count as real detections.
- Small/distant subjects receive a bounded two-times unobserved window, capped
  at ten seconds; ended-unknown remains capped at 22 seconds.

Live use-case qualification:

- Face: AdaFace samples with crop evidence and 512D embeddings.
- Body: TransReID samples with crop evidence and 384D embeddings.
- Gait: qualified GaitBase sequences and 4096D embeddings.
- Line crossing: actual segment-intersection events.
- Fire/smoke: `fire_smoke_suspected` for smoke, score approximately `0.814`,
  with full-frame evidence and NPU execution.

Across the qualification states, 478 observations, 403 evidence records and 79
embeddings validated against the V6 schemas with zero broken ID/file links.

The final dev39 RTSPS presence run held `7.954` decoded FPS against an 8-FPS
target. It produced no sub-second loss/recovery pair; the shortest recovery
followed approximately five seconds of evaluated detection absence. Its 21
observations and six evidence assets validated with zero broken references.
`/healthz`, `/readyz`, and `/metrics` returned HTTP 200; the removed `/healthy`
alias returned 404.

## Validation commands

```bash
pytest -q edge_runtime/solution_packs/sporada_secure/runtime_v6/tests/test_v6_runtime.py
podman image inspect \
  ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.10.30-v6-dev39
```

Result: `78 passed`.

The live container used host networking plus `/dev/dri` and `/dev/accel`; the
camera URL remained in the mounted Secret file and was not printed.

## Explicit remaining limitation

The supplied motorcycle image was used as ANPR ground truth. The visible text
appears to be `KA05KP0667`; the current OCR returned variants
`KA05KP8667`/`KA05KP8677`. Original-frame plate cropping and several
preprocessing variants did not correct that character. Therefore this sample
is not exact-match certified. The runtime does not add an Indian-plate regex or
silently rewrite uncertain OCR text. This requires OCR model/data improvement
and a labelled ANPR evaluation set, not merely looser gates.
