# Sentry V7 embedded contract package

This directory is copied into the V7 multi-box runtime image at
`/opt/traffic-pilot/image_schema`. It contains the desired-state, observation,
evidence, embedding, acknowledgement and delivery schemas used by the runtime.

V7 preserves V6 analytics meanings and production-policy gates while changing
the wire marker to `7.0` and using sequenced `/api/v7/ingest/*` delivery. Each
logical deployment owns one permanent stream, one transactional SQLite outbox
and one stable server-generated stream ID. Camera processes share that outbox;
they do not own independent delivery streams.

The complete source package and qualification handoff are in
`newdetails/sentry-v7`. No image should claim production V7 conformance until
its immutable digest is recorded after contract, recovery, Intel and
live-camera qualification.
