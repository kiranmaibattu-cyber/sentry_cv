# Sentinel V6 dev22 management handoff

## Immutable image

```text
ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.10.28-v6-dev22
sha256:b042b3714d9548d42d67dea21948808237b1dfabe78459adbe4db6756437436b
```

Image ID: `ef8488352e2a4497c2f9249bd98e1a0f9dc53d45ee7152efd354ff90b13aec55`

The image remains Sentry V6 with schema `6.0`. It accepts no V7 or multi-box
credentials and delivers only through:

- `POST /api/v6/ingest/events`
- `POST /api/v6/ingest/evidence`
- `POST /api/v6/ingest/embeddings`

## Embedded contract material

The image embeds the contract at `/opt/traffic-pilot/image_schema`, including:

- V6 desired-state, observation, evidence, embedding and acknowledgement schemas.
- A desired-state example.
- Sixteen representative observation payloads.
- Evidence and acknowledgement payload examples.

The source package is under `newdetails/sentry-v6`. Its validator checks all
five schemas, examples, cross-references, stale fields, and camera counts 1, 8,
16 and 50. A 51-camera desired state is rejected.

## Runtime changes

- The desired-state schema and runtime validator allow 1-50 cameras.
- Each camera remains isolated in its own supervised process. A failed camera
  cannot make a healthy peer stop or make shared readiness false.
- Logs are JSON and carry UTC timestamp, process ID, camera ID and stream-session
  ID. Decoder start, exit/reconnect, retry reason/backoff and sanitized FFmpeg
  stderr are recorded. URL credentials, tokens and bearer values are redacted.
- The legacy console FPS line is replaced by current per-camera telemetry:
  target, decoded, inference, quiet and output-event rates.
- `/healthz` remains available during a stream outage and reports last-frame
  time/age, reconnects, decoder errors, FFmpeg exit code, retry reason/backoff,
  and current stream session.
- `/healthz` and `/metrics` expose pending count/bytes/oldest age, attempts,
  acknowledgements, retries and HTTP rejection reasons separately for
  observations, evidence and embeddings.
- Pending outbox gauges are scanned at most once per second during production,
  with an immediate refresh after successful completion or dead-lettering, so
  backlog telemetry does not turn event persistence into quadratic work.

## Intel validation

The final image was tested with a 1920x1080 RTSPS stream at a desired 5 FPS and
the complete configured app mix. VAAPI hardware decode was active; vehicle,
plate, fire/smoke and scene work loaded on GPU; face/body/gait and OCR included
the configured NPU paths.

A stable telemetry window reported 5.255 decoded FPS. The final-image test
persisted 32 observations, 17 evidence images and 14 embeddings before shutdown.
The test had no Management endpoint configured, so these remained in the
outbox by design.

A controlled FFmpeg termination produced an unavailable/degraded camera state,
exit code 255, one reconnect/error, a one-second retry, and automatic recovery
under a new stream-session ID. `/healthz` and shared readiness stayed available;
the pod and camera worker were not restarted.

The one-camera full-app run reported approximately 1.9-2.0 GiB container
working set. Cgroup `memory.current`, which includes page cache, was about
3.2-3.4 GiB. Therefore 50 is a validated configuration/supervision limit, not
a claim that 50 full-app 1080p streams fit a 14 GiB pod. Higher-count Intel
qualification must declare camera resolution, desired FPS, enabled bindings,
scene interval and memory limit.

## Representative local test command

The rootless Podman test required host UID mapping and retained Intel device
groups. Kubernetes must provide equivalent `/state` ownership and device access.

```bash
podman run --rm --network=host \
  --userns=keep-id:uid=10001,gid=10001 \
  --group-add keep-groups \
  --security-opt label=disable \
  --device /dev/dri:/dev/dri \
  --device /dev/accel:/dev/accel \
  -v "$CONFIG_DIR:/configs:ro" \
  -v "$CAMERA_SECRET_DIR:/run/secrets/sentinel/cameras:ro" \
  -v "$STATE_DIR:/state:rw" \
  ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime@sha256:b042b3714d9548d42d67dea21948808237b1dfabe78459adbe4db6756437436b
```

For the tested RTSPS source, the effective FFmpeg decode shape was:

```text
ffmpeg -nostdin -loglevel error
  -hwaccel vaapi -hwaccel_device /dev/dri/renderD128
  -hwaccel_output_format vaapi
  -rtsp_transport tcp -timeout 30000000
  -i <mounted-secret-RTSPS-URL>
  -vf fps=5,hwdownload,format=nv12
  -pix_fmt nv12 -f rawvideo -
```

Kubernetes should run as UID/GID 10001, make `/state` writable using an
appropriate `fsGroup` or volume ownership policy, and expose `/dev/dri` and
`/dev/accel`. Do not weaken `/readyz`; an unavailable camera is represented in
per-camera health while the operational supervisor remains ready.
