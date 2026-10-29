# Sentinel V6 Dev27 Shared Inference Architecture

Dev27 changes the internal vehicle/person inference transport and scheduler. It
does not change the Sentry V6 desired state, wire schemas, ingest routes,
production-event policy, event types, evidence semantics, or embedding profiles.

## Runtime boundary

Each camera still owns:

- VAAPI decoding and motion-gate state;
- tracking, presence recovery, zones, counts, dwell, and line crossing;
- ANPR candidate history and person-identity sample selection;
- original decoded frames and all evidence crops;
- production-policy confirmation, deduplication, cooldowns, and timestamps;
- V6 observation/evidence/embedding persistence and delivery.

The fleet parent owns only the compiled vehicle/person model and its inference
scheduling:

```text
camera process                         fleet parent
--------------                         ------------
original BGR frame
  -> letterbox 640x640
  -> private mmap slot --------------> fair round-robin scheduler
  -> bounded job token                  -> OpenVINO AsyncInferQueue
  <- bounded result token <------------ 4 requests, batch 1
  -> source-coordinate detections
  -> existing tracker and V6 policy
```

No source frame, evidence JPEG, tracker state, presence state, or policy state is
placed in the shared inference service. A camera waits for its own detector
result before it advances its state, preserving the ordering used by dev25.

## Safety and backpressure

- Every camera has a private 640x640x3 uint8 slot (1,228,800 bytes).
- Every camera may have at most one queued detector job.
- The scheduler holds the slot lock until OpenVINO completes, including across a
  camera-process crash/restart.
- Job IDs prevent a restarted camera from consuming a stale completion.
- The scheduler checks cameras round-robin so one busy camera cannot monopolize
  submissions.
- The scheduler checks `AsyncInferQueue.is_ready()` before every submission, so
  a full four-request queue never blocks the thread responsible for allowing
  Python completion callbacks to run.
- Batch remains fixed at one; concurrency comes from four asynchronous requests.
- If `/dev/shm` lacks the slot bytes plus an 8 MiB reserve, slots use
  `/tmp/sentinel/shared_vehicle`. `SENTINEL_SHARED_MEMORY_DIR` can select an
  explicit mount.
- A scheduler failure fails the worker and lets Kubernetes restart the image;
  it cannot silently leave a ready HTTP process with dead inference.

## Telemetry

The existing per-camera `/healthz` telemetry now separates:

- `detection.vehicle_person`: camera-observed end-to-end detector call;
- `detection.vehicle_person.scheduler_queue_wait`: wait before submission;
- `detection.vehicle_person.device_inference`: asynchronous request wall time.

These values are timings, not hardware-utilization percentages. Existing GPU/NPU
node counters and per-camera attributed model-stage telemetry remain unchanged.

## Validation

- V6 contract package: five schemas, 16 representative outputs, and
  1/8/16/50-camera desired states.
- Runtime tests: 67 passed.
- One-camera full-app recorded-video test: VAAPI decoder healthy; GPU vehicle,
  plate, fire/smoke and scene models initialized; NPU face/body/gait paths
  initialized; V6 scene evidence and embeddings persisted.
- Thirteen-camera test: three RTSPS and ten MPEG-TS/TCP camera workers became
  healthy and decoded around their 5 FPS targets through one four-request
  scheduler. After running beyond the 30-second timeout boundary, there were no
  shared-inference timeouts, `CL_OUT_OF_RESOURCES` errors, camera-process
  failures, or scheduler failures. All 464 captured outbox packages validated
  against the V6 observation schema.

The HTTP recording server delivered frames faster than camera time, so its
decoded-FPS values are not capacity measurements. The test establishes routing,
fairness, accelerator execution, policy continuity, and V6 output integrity.

## Deferred optimization phases

Dev27 deliberately does not yet:

- pass native NV12 tensors directly from decode to OpenVINO;
- decouple camera decode and policy processing with a source-frame ring;
- centralize plate, fire/smoke, scene, face, body, or gait model execution;
- pass VA surface handles between processes;
- use cross-camera static batching.

Those changes require separate parity and evidence-retention validation. Dev27 is
the first safe phase: remove detector tensor serialization and locked single-
request execution without changing Management-visible behavior.
