# Sentinel V6 dev23 TCP validation

## Scope

Dev23 is dev22 plus one additional live input scheme: MPEG-TS over TCP.

The following remain unchanged from dev22:

- Sentry V6 wire schema `6.0` and `/api/v6/ingest/*` delivery.
- FFmpeg VAAPI hardware decoding and frame conversion.
- Motion gate, inference, tracking, production policy and all use cases.
- Model files and GPU/NPU placement.
- Worker lifecycle, retry policy, telemetry behavior and camera startup order.
- RTSP, RTSPS, HTTP and HTTPS behavior.

## TCP processing path

The desired-state camera source remains a Secret-file reference. The referenced
file may now contain:

```text
tcp://host:port
```

The runtime classifies it as a live TCP source and passes it to the same FFmpeg
decoder used by the existing inputs:

```text
TCP MPEG-TS
  -> FFmpeg input with -rw_timeout 30000000
  -> existing VAAPI hardware decode
  -> existing NV12 download/BGR conversion
  -> existing motion gate
  -> existing inference, tracking and use cases
  -> existing V6 output and delivery
```

TCP URLs are redacted from structured logs in the same way as RTSP and HTTP
URLs.

## Candidate image

```text
ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.10.28-v6-dev23
```

A registry digest remains pending until explicit publication.

Local image ID:

```text
712c909551ce1eb418a52f3bef79a0560ca7a5514db529ad0a003f842a8b3192
```

## Live TCP validation

Test source:

```text
tcp://192.168.1.92:9001
```

The source is H.264 MPEG-TS at 1920x1080. A new connection initially arrived
before an SPS/PPS repeat, so the resolution probe briefly reported degraded and
then recovered when valid headers arrived. The decoder subsequently remained
healthy with no reconnects or decoder errors.

Stable measurements from the exact candidate image:

```text
desired FPS:             5.0
decoded FPS:             5.020
selective inference FPS: 0.456
motion-gate quiet FPS:   4.563
decode placement:        GPU/VAAPI
detection placement:     GPU
decoder starts:          1
decoder reconnects:      0
decoder errors:          0
```

The configured V6 vehicle-counting path persisted `camera_health`, `zone_count`
and track-lifecycle observations. This verifies that TCP feeds the existing
decode, motion, detection and V6 event pipeline rather than a separate
analytics path.

The previously supplied local RTSP test paths returned HTTP-style RTSP `404`
responses during this validation, and the supplied RTSPS endpoint timed out.
No RTSP/HTTP decode logic was modified by dev23; its unchanged behavior remains
covered by the existing runtime tests.

## Five-camera qualification

Five unique camera IDs were configured with five Secret files containing the
same `tcp://192.168.1.92:9001` URL. This configuration is not yet qualified as
stable.

All five camera pipelines loaded and every camera reached VAAPI decoding and
GPU detection at least once. However, health cycled during the test: camera
processes exited with native status `-11` or `-6`, some TCP decoders reconnected,
and the five cameras did not remain healthy concurrently for a sustained
window. Healthy cameras processed near the configured 5 FPS while affected
cameras returned to probing or decoder startup.

To isolate the source, five plain FFmpeg consumers were run concurrently for
30 seconds. All five completed successfully, proving that the server accepts
five simultaneous clients. Every client did report missing H.264 PPS at its
initial join point, so the server should repeat SPS/PPS with IDR frames to make
new-client startup deterministic. That source behavior explains slow probes,
but it does not by itself explain the native camera-process exits.

Conclusion: one TCP camera is validated; five duplicate TCP camera instances
are currently not production-stable in the CV runtime and require a separate
multi-camera native-runtime investigation. No workaround or unrelated startup
policy has been added to this TCP-only image.

## Static verification

```text
runtime tests:            60 passed
V6 contract validation:  5 schemas, 16 examples, app catalogue, and
                         1/8/16/50-camera configurations passed
Python compilation:      passed
```
