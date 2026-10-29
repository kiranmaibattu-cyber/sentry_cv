# Sentinel V6 dev24 TCP multi-camera validation

## Scope

Dev24 preserves the Sentry V6 schema 6.0 contract and `/api/v6/ingest/*`
delivery introduced before dev23. It fixes the multi-camera GPU failure found
while five camera IDs consumed the same MPEG-TS TCP source.

Candidate image:

```text
ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.10.29-v6-dev24
local image ID: c2f24c1bdf1820dab423ec1a460eec4ca8327320628e50b7721b9c047cc3f613
local digest: sha256:34fa9686bcefcfdd628ec4b334c5cf6a9b8c559579d03cee125205478ce3ecbc
```

The TCP, RTSP, RTSPS, HTTP and HTTPS inputs all continue into the same VAAPI
decode, motion gate, inference, tracking, production-policy and V6 delivery
pipeline. No wire-contract field or Management API changed.

## Root cause

Dev23 compiled an independent OpenVINO vehicle/person GPU model in every
camera process. Five synchronized consumers of the same TCP source submitted
frames in lockstep. The fifth GPU model instance could fail during execution
with:

```text
[GPU] clFinish, error code: -5 CL_OUT_OF_RESOURCES
```

An Intel GPU-plugin teardown failure then hid that exception behind a
`longjmp`/SIGSEGV message. Five software FFmpeg consumers and five direct VAAPI
decoder consumers were stable, confirming that TCP transport and VAAPI decode
were not the original failure.

## Runtime correction

For multi-camera deployments, dev24 compiles the mandatory vehicle/person
detector once in a dedicated GPU inference service. Each isolated camera
process performs its own decode and 640x640 letterbox, submits that bounded
tensor to the service, and receives the NMS result. Camera-local tracking,
motion gating, event policy, evidence and delivery remain isolated.

The detector uses `PERFORMANCE_HINT=LATENCY`, one GPU stream and one shared
InferRequest. A transient `CL_OUT_OF_RESOURCES` response skips the affected
frame without producing a false empty count, resets the request, records
`gpu_resource_rejections`, and applies bounded backoff. An unhandled camera
exception is logged before the camera-local supervisor restart, so OpenVINO
teardown cannot replace the original traceback.

## Five-camera live result

Source:

```text
tcp://192.168.1.92:9001
```

Five unique camera IDs used separate Secret files containing that same URL.
The source was H.264 MPEG-TS at 1920x1080 and each camera requested 5 FPS.

Stable measured window:

```text
camera  decoded FPS  inference FPS  health   reconnects  decoder errors
tcp01      5.016         5.016       healthy      0             0
tcp02      5.016         5.016       healthy      0             0
tcp03      5.020         5.020       healthy      0             0
tcp04      5.020         5.020       healthy      0             0
tcp05      4.978         4.978       healthy      0             0
```

There were no `CL_OUT_OF_RESOURCES` responses, native crashes, camera-process
restarts or decoder reconnects during the validation window. Container memory
was approximately 2.08 GB, down from approximately 3.2 GB with five separately
compiled detector instances. Motion-gate windows can intentionally report a
lower inference FPS while decoded FPS remains near 5.

## Static verification

```text
runtime tests:       65 passed
Python compilation: passed
```

The 1/8/16/50-camera desired-state acceptance limit remains a configuration
contract. It does not claim that every combination of resolution, FPS and
enabled models can meet real-time throughput on one Intel 285H. Higher camera
counts still require capacity qualification with representative streams and
use-case bindings.

The image also includes and verifies `tcpclientsrc`, `tsdemux`, `h264parse`
and `vaapih264dec`. The active V6 TCP implementation continues to use the same
FFmpeg VAAPI decoder as RTSP/HTTP; these GStreamer elements preserve the native
surface-path capability without changing the selected runtime path.

## Health and performance telemetry

`/healthz`, `/readyz`, and `/metrics` were tested against the live TCP source.
`/healthy` is intentionally absent. CPU and RAM values come from process and
cgroup-v2 counters. NPU utilization comes from the NPU busy-time counter. GPU
utilization uses Intel GT non-RC6 active residency and is explicitly labeled as
node-wide activity rather than per-process engine busy. Unavailable counters
remain `null`/`counter_unavailable`; they are not replaced with synthetic zeroes.

A peer-isolation test configured one healthy TCP camera and one unreachable TCP
camera. The healthy camera continued at approximately 4.98 decoded FPS, the
failed camera reported `unavailable/probe_failed`, `/healthz` remained fresh,
and `/readyz` remained HTTP 200.
