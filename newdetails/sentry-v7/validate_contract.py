"""Validate the V7 contract package and representative boundary decisions."""

import copy
import json
import re
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parent
CHECK_FORMATS = FormatChecker()


def read_json(name):
    return json.loads((ROOT / name).read_text())


def validator(schema):
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=CHECK_FORMATS)


def rejected(checker, instance):
    assert not checker.is_valid(instance), f"Unexpectedly accepted: {instance}"


contract = yaml.safe_load((ROOT / "sentry-v7-contract.yaml").read_text())
assert contract["schemaVersion"] == "7.0"
assert contract["status"] == "ready-for-cv-implementation"
assert all((ROOT / name).is_file() for name in contract["schemas"].values())
schemas = {path.name: read_json(path.name) for path in ROOT.glob("*.schema.json")}
checks = {name: validator(schema) for name, schema in schemas.items()}

desired = read_json("desired-state.example.json")
def validate_desired(instance):
    checks["desired-state.schema.json"].validate(instance)
    if instance["edge_id"] != instance["deployment_id"]:
        raise ValueError("edge_id must equal deployment_id")

validate_desired(desired)
mismatched_identity = copy.deepcopy(desired)
mismatched_identity["edge_id"] = "different-edge"
try:
    validate_desired(mismatched_identity)
except ValueError:
    pass
else:
    raise AssertionError("edge_id/deployment_id mismatch was accepted")
evidence = read_json("evidence.example.json")
checks["evidence.schema.json"].validate(evidence)
delivery_ack = read_json("multi-box-delivery.example.json")
checks["multi-box-delivery.schema.json"].validate(delivery_ack)
for key in ("accepted_ack", "duplicate_ack", "skip_ack", "stream_state"):
    checks["acknowledgement.schema.json"].validate(delivery_ack[key])
events = read_json("observation-examples.json")
for event in events:
    checks["observation.schema.json"].validate(event)

event_types = {
    branch["properties"]["event_type"]["const"]
    for branch in schemas["observation.schema.json"]["oneOf"]
}
assert event_types == set(contract["outputSemantics"])
assert event_types == {event["event_type"] for event in events}
assert set(desired["cameras"][0]["bindings"][0]) >= {
    "use_case_id", "app", "scope", "geometry_ids"
}
expected_apps = {
    "vehicle_counting", "vehicle_entry_exit_counts", "vehicle_presence", "anpr",
    "people_counting", "people_entry_exit_counts", "person_presence", "line_crossing",
    "person_identity", "fire_smoke", "scene_search",
}
assert expected_apps == set(schemas["desired-state.schema.json"]["$defs"]["app"]["enum"])
protocol = contract["delivery"]["multiBoxProtocol"]
assert protocol["version"] == "7.0"
assert protocol["stream"]["sequence"]["scope"] == "permanent_deployment_stream"
assert protocol["stream"]["sequence"]["startsAt"] == 1
assert protocol["stream"]["identity"] == ["deployment_id"]
assert protocol["stream"]["writerFence"] == ["box_id", "placement_generation"]
assert protocol["stream"]["sequence"]["restartOnPlacementChange"] is False
assert protocol["sending"]["allocationOrder"] == "ascending_sequence"
assert protocol["sending"]["maximumInFlightRequests"] == 8
assert protocol["sending"]["noOvertakeWithinPackage"] is True
assert protocol["connectsToRedis"] is False
assert set(protocol["requestHeaders"]) == {
    "Authorization", "X-Sentry-Delivery-Mode", "X-Sentry-Box-Credential",
    "X-Sentry-Placement-Generation", "X-Sentry-Stream-Sequence"
}
skip_reasons = {
    "validation_failed", "evidence_expired", "object_unreadable",
    "outbox_overflow", "retention_expired",
}
assert set(protocol["skip"]["reasons"]) == skip_reasons
schema_skip_reasons = set(
    schemas["multi-box-delivery.schema.json"]["$defs"]["skip_request"]
    ["properties"]["body"]["properties"]["reason"]["enum"]
)
assert schema_skip_reasons == skip_reasons

local_states = {"pending", "acked", "dead_letter", "skip_pending", "skipped",
                "resolved_by_remote_receipt"}
assert set(protocol["outbox"]["states"]) == local_states
assert set(
    schemas["multi-box-delivery.schema.json"]["$defs"]["local_state"]["enum"]
) == local_states

hashing = protocol["outbox"]["hashes"]
assert hashing["observation"] == "sha256_of_exact_application_json_request_body_bytes"
assert hashing["embedding"] == "sha256_of_exact_application_json_request_body_bytes"
assert hashing["evidence"]["content_sha256"] == "sha256_of_raw_image_bytes"
assert hashing["evidence"]["delivery_payload_sha256"] == (
    "sentry_canonical_metadata_JSON_v1_then_one_NUL_byte_then_raw_image_bytes"
)
assert set(hashing["evidence"]["excludes"]) == {
    "multipart_boundaries", "multipart_headers", "transport_framing",
}

offline = delivery_ack["offline_eviction"]
assert offline == {
    "disconnected": True,
    "sequence": 8,
    "reason": "evidence_expired",
    "local_state": "skip_pending",
    "state_committed_and_fsynced_before_delete": True,
    "payload_bytes_retained": False,
    "after_reconnect_state": "skipped",
}
assert protocol["outbox"]["offlineEviction"] == {
    "transition": "transactionally_set_skip_pending_and_reason_commit_and_fsync_then_delete_payload_bytes",
    "crashRecovery": "a_committed_skip_pending_record_may_temporarily_retain_bytes_but_bytes_must_never_be_deleted_without_the_marker",
    "reconnect": "send_skip_markers_in_sequence_order_then_set_skipped",
}

recovery = delivery_ack["lost_state_recovery"]
assert recovery["next_sequence"] == recovery["highest_received_sequence"] + 1
assert [item["sequence"] for item in recovery["skips"]] == list(range(
    recovery["highest_contiguous_sequence"] + 1,
    recovery["highest_received_sequence"] + 1,
))
assert {item["reason"] for item in recovery["skips"]} == {"object_unreadable"}
assert recovery["receipt_conflict_http_status"] == 409
assert recovery["receipt_conflict_treated_as_success"] is True

cascade = delivery_ack["parent_child_skip"]
assert cascade["parent"]["object_type"] == "observation"
assert cascade["cascade_owner"] == "runtime"
assert cascade["one_skip_request_per_object"] is True
assert {item["object_type"] for item in cascade["children"]} == {
    "evidence", "embedding",
}
assert {item["reason"] for item in [cascade["parent"], *cascade["children"]]} == {
    cascade["parent"]["reason"],
}
assert cascade["defensive_child_upload"] == {
    "http_status": 200, "status": "duplicate",
}
assert protocol["skip"]["childRetryAfterCascade"] == {
    "http": 200,
    "status": "duplicate",
    "condition": "child_sequence_has_cascade_tombstone",
    "runtimeAction": "mark_skipped",
}
assert protocol["skip"]["missingParentWithoutChildTombstone"]["http"] == 425
assert protocol["skip"]["cascades"]["owner"] == "runtime"
assert protocol["skip"]["cascades"]["serverInference"] == (
    "none_because_skip_contains_no_object_or_parent_identity"
)
assert protocol["serverGuarantees"]["tombstoneBeforeParentCheck"] == (
    "a_child_sequence_already_filled_by_a_cascade_tombstone_returns_duplicate"
)
assert protocol["serverGuarantees"]["payloadHashing"] == (
    "recompute_using_the_object_type_rules_above_and_never_hash_raw_multipart_framing"
)

dead_letters = delivery_ack["dead_letter_policy"]
assert dead_letters["path"] == "/state/dead-letter"
assert dead_letters["maximum_records"] == 1000
assert dead_letters["maximum_age_days"] == 7
assert dead_letters["counts_toward_outbox_cap"] is True
assert dead_letters["payload_bytes_after_skip"] is False
assert protocol["skip"]["deadLetter"]["cleanup"] == {
    "maximumRecords": 1000,
    "maximumAgeDays": 7,
    "rule": "whichever_first_remove_oldest",
}

assert delivery_ack["payload_hashing"] == {
    "observation": "sha256_exact_application_json_body_bytes",
    "evidence_content_sha256": "sha256_raw_image_bytes",
    "evidence_delivery_payload_sha256": "sha256_sentry_canonical_metadata_json_v1_nul_raw_image_bytes",
    "embedding": "sha256_exact_application_json_body_bytes",
}
assert contract["retention"]["edgeOutbox"]["ageMeasuredFrom"] == (
    "durable_outbox_created_at_for_each_package"
)

gap = delivery_ack["gap_progression"]
assert gap["received_before_gap_fill"] == [1, 2, 3, 5, 6]
assert gap["before"]["highest_contiguous_sequence"] == 3
assert gap["before"]["highest_received_sequence"] == 6
assert gap["gap_fill"] == 4
assert gap["after"]["highest_contiguous_sequence"] == 6
assert gap["after"]["highest_received_sequence"] == 6
streams = delivery_ack["two_deployments_one_box"]["streams"]
assert len({item["deployment_id"] for item in streams}) == 2
assert [item["first_sequence"] for item in streams] == [1, 1]
assert streams[0]["highest_contiguous_sequence"] != streams[1]["highest_contiguous_sequence"]
assert delivery_ack["stream_state"]["status"] == "accepted"
assert (delivery_ack["stream_state"]["sequence"]
        == delivery_ack["stream_state"]["highest_received_sequence"])
for key in ("accepted_ack", "duplicate_ack", "skip_ack", "stream_state"):
    ack = delivery_ack[key]
    assert ack["deployment_id"] == desired["deployment_id"]
    assert ack["highest_contiguous_sequence"] <= ack["highest_received_sequence"]
    assert ack["sequence"] <= ack["highest_received_sequence"]
assert all(delivery_ack["ack_validation"].values())
assert delivery_ack["delivery_modes"] == {"remote": "remote_v7", "local": "local_v7"}
for key in ("local_object_request", "local_skip_request", "local_stream_request"):
    headers = delivery_ack[key]["headers"]
    assert headers["X-Sentry-Delivery-Mode"] == "local_v7"
    assert "X-Sentry-Box-Credential" not in headers
local_with_credential = copy.deepcopy(delivery_ack)
local_with_credential["local_stream_request"]["headers"]["X-Sentry-Box-Credential"] = "forbidden"
rejected(checks["multi-box-delivery.schema.json"], local_with_credential)
assert delivery_ack["stream_bootstrap"] == {
    "query_before_upload": True,
    "persist_stream_id": True,
    "stream_id_stable_across_placement": True,
    "unexpected_stream_id_action": "pause_uploads_and_deletion_require_recovery",
}
assert delivery_ack["receipt_conflict_resolution"]["local_state"] == "resolved_by_remote_receipt"
assert delivery_ack["receipt_conflict_resolution"]["send_skip"] is False
assert delivery_ack["receipt_conflict_resolution"]["renumber"] is False
assert delivery_ack["storage_profile"] == {
    "access_mode": "ReadWriteOnce",
    "storage_class": "local-path",
    "volume_mode": "Filesystem",
    "filesystem": "ext4",
    "network_filesystems": "prohibited_unless_separately_qualified",
    "qualification": "power_loss_and_node_reboot_with_wal_recovery",
}
assert contract["compatibility"]["wireCompatibleWithV6"] is False
assert contract["compatibility"]["analyticsPayloadShapeChanged"] is False
assert contract["compatibility"]["desiredStateLimitsChanged"] is True
assert contract["desiredState"]["identity"]["edge_id"] == (
    "deprecated_alias_that_must_equal_deployment_id"
)
assert contract["management"]["baseUrl"]["default"].startswith("https://")
assert contract["resources"]["hardCapCameraStreams"] == 50
assert contract["retention"]["physicalBox"]["configuredMaximumCameras"] == 50
assert schemas["desired-state.schema.json"]["properties"]["cameras"]["maxItems"] == 50
assert "temporary_file_hash_and_fsync" in protocol["serverGuarantees"]["evidenceWriteOrder"]
assert "without_a_committed_evidence_row" in protocol["serverGuarantees"]["evidenceOrphanRecovery"]

camera = desired["cameras"][0]
geometry = {
    **{item["id"]: "polygon" for item in camera["geometry"]["zones"]},
    **{item["id"]: "line" for item in camera["geometry"]["counting_lines"]},
}
binding_ids = [item["use_case_id"] for item in camera["bindings"]]
assert len(binding_ids) == len(set(binding_ids))
for binding in camera["bindings"]:
    assert all(geometry[item] == ("line" if binding["app"] in {
        "vehicle_entry_exit_counts", "people_entry_exit_counts", "line_crossing"
    } else "polygon") for item in binding["geometry_ids"])
    assert all(item in binding_ids and item != binding["use_case_id"]
               for item in binding.get("scene_trigger_use_case_ids", []))

owner_policy = contract["subjectEventOwnership"]
assert owner_policy["tieBreak"] == "lexicographically_smallest_use_case_id"
owner_candidates = [item for item in camera["bindings"]
                    if item["app"] in {"vehicle_presence", "anpr"}
                    and "parking-zone" in item["geometry_ids"]]
owner = min(owner_candidates,
            key=lambda item: (0 if item["app"] == "vehicle_presence" else 1,
                              item["use_case_id"]))
assert next(item for item in events if item["event_type"] == "object_present")["use_case_id"] == owner["use_case_id"]
assert min(["z-presence", "a-presence"]) == "a-presence"

plate = next(event for event in events if event["event_type"] == "plate_read")
assert evidence["evidence_type"] == "frame"
assert evidence["evidence_id"] in plate["evidence_ids"]
assert evidence["observation_id"] == plate["observation_id"]
assert evidence["source_frame_id"] == plate["source_frame_id"]
assert evidence["captured_at"] == plate["observed_at"]

full_frame = copy.deepcopy(desired)
full_frame["cameras"][0].pop("geometry")
full_frame["cameras"][0]["bindings"] = [
    {"use_case_id": "all-vehicle-count", "app": "vehicle_counting",
     "scope": "full_frame", "geometry_ids": [], "object_types": ["vehicle"]},
    {"use_case_id": "body-only", "app": "person_identity",
     "scope": "full_frame", "geometry_ids": [], "object_types": ["person"],
     "embedding_profile_ids": ["transreid-ssl-v18.1"]},
]
checks["desired-state.schema.json"].validate(full_frame)

polygon = next(item for item in camera["geometry"]["zones"]
               if item["id"] == "parking-zone")["poly"]
xs, ys = zip(*polygon)
rect = {"x1": min(xs), "y1": min(ys), "x2": max(xs), "y2": max(ys)}
assert contract["sceneRegionCrop"]["outsidePolygonMask"] == "none"
assert contract["sceneRegionCrop"]["padding"] == "none"
region_state = copy.deepcopy(desired)
region_binding = copy.deepcopy(next(item for item in camera["bindings"]
                                    if item["app"] == "scene_search"))
region_binding.update(use_case_id="parking-scene", scope="geometry",
                      geometry_ids=["parking-zone"])
region_state["cameras"][0]["bindings"].append(region_binding)
checks["desired-state.schema.json"].validate(region_state)
region_event = copy.deepcopy(next(item for item in events
                                  if item["event_type"] == "scene_sample"))
region_event.update(observation_id="35353535-3535-4535-8535-353535353535",
                    use_case_id="parking-scene", scope="geometry",
                    zone_id="parking-zone",
                    evidence_ids=["36363636-3636-4636-8636-363636363636"])
checks["observation.schema.json"].validate(region_event)
region_evidence = copy.deepcopy(evidence)
region_evidence.update(evidence_id=region_event["evidence_ids"][0],
                       observation_id=region_event["observation_id"],
                       evidence_type="scene_region_crop",
                       captured_at=region_event["observed_at"],
                       source_frame_id=region_event["source_frame_id"],
                       source_bbox_normalized=rect)
checks["evidence.schema.json"].validate(region_evidence)

bad = copy.deepcopy(desired)
bad["cameras"][0]["apps"] = ["anpr"]
rejected(checks["desired-state.schema.json"], bad)
bad = copy.deepcopy(desired)
bad["cameras"][0]["bindings"][4]["embedding_profile_ids"] = ["siglip2-base-v1"]
rejected(checks["desired-state.schema.json"], bad)
bad = copy.deepcopy(desired)
bad["cameras"][0]["bindings"][0]["sample_interval_seconds"] = 2
rejected(checks["desired-state.schema.json"], bad)

bad = copy.deepcopy(evidence)
bad["evidence_type"] = "plate_crop"
rejected(checks["evidence.schema.json"], bad)
bad["evidence_type"] = "aligned_face_chip"
rejected(checks["evidence.schema.json"], bad)

bad = copy.deepcopy(events[0])
bad["recheck_id"] = "12121212-1212-4212-8212-121212121212"
rejected(checks["observation.schema.json"], bad)
bad = copy.deepcopy(events[0])
bad["plate_text"] = "KA01AB1234"
rejected(checks["observation.schema.json"], bad)
bad = copy.deepcopy(next(x for x in events if x["event_type"] == "plate_read"))
bad["evidence_ids"] = []
rejected(checks["observation.schema.json"], bad)

embedding = {
    "schema_version": "7.0",
    "embedding_id": "14141414-1414-4414-8414-141414141414",
    "observation_id": "88888888-8888-4888-8888-888888888888",
    "use_case_id": "person-search",
    "evidence_id": "13131313-1313-4313-8313-131313131313",
    "profile_id": "adaface-ir101-v18.1",
    "kind": "face",
    "vector": [1.0] + [0.0] * 511,
    "quality": 0.82,
    "captured_at": "2026-10-01T10:03:00Z",
    "source_frame_id": "entrance-camera:3840",
    "presence_id": "12121212-1212-4212-8212-121212121212",
    "model_id": "adaface-ir101",
    "model_version": "qualified-revision",
    "preprocessing_id": "alignment-v1",
    "normalization": "l2",
}
checks["embedding.schema.json"].validate(embedding)
bad = copy.deepcopy(embedding)
del bad["quality"]
rejected(checks["embedding.schema.json"], bad)
scene_embedding = copy.deepcopy(embedding)
scene_embedding.update(profile_id="siglip2-base-v1", kind="semantic",
                       vector=[1.0] + [0.0] * 767, presence_id=None,
                       observation_id="55555555-5555-4555-8555-555555555555",
                       evidence_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
                       use_case_id="scene-history")
del scene_embedding["quality"]
checks["embedding.schema.json"].validate(scene_embedding)
scene_embedding["quality"] = 1.0
rejected(checks["embedding.schema.json"], scene_embedding)
subject_semantic = copy.deepcopy(scene_embedding)
del subject_semantic["quality"]
subject_semantic.update(presence_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                        observation_id="11111111-1111-4111-8111-111111111111",
                        evidence_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                        use_case_id="parking-presence")
checks["embedding.schema.json"].validate(subject_semantic)

release = yaml.safe_load((ROOT / "release.yaml").read_text())
assert release["status"] == "contract-ready-awaiting-qualified-image"
expected_capabilities = {
    "hardware_profile": "intel-285h",
    "wire_schema_version": "7.0",
    "multi_box_delivery": "7.0",
}
assert contract["imageRegistration"]["capabilities"] == expected_capabilities
assert release["capabilities"] == expected_capabilities
assert release["multi_box_delivery_contract"] == "7.0"

protocol_files = [
    ROOT / "sentry-v7-contract.yaml",
    ROOT / "acknowledgement.schema.json",
    ROOT / "multi-box-delivery.schema.json",
    ROOT / "multi-box-delivery.example.json",
    ROOT / "release.yaml",
    ROOT / "MULTI_BOX_IMAGE_HANDOFF.md",
    ROOT / "README.md",
]
protocol_text = "\n".join(path.read_text() for path in protocol_files)
for forbidden in (
    "batch_id", "X-Sentry-Batch-ID", "X-Sentry-Box-Sequence",
    "box_sequence", "batch commit", "manifest hash", "/api/v5",
    "SENTINEL_BOX_CREDENTIAL_is_present",
):
    assert forbidden.lower() not in protocol_text.lower(), f"stale protocol term: {forbidden}"
assert set(re.findall(r"/api/v([0-9]+)", protocol_text)) == {"7"}
assert "X-Sentry-Stream-Sequence" in protocol_text
assert '"1.0"' not in protocol_text and "'1.0'" not in protocol_text

handoff = (ROOT / "MULTI_BOX_IMAGE_HANDOFF.md").read_text()
for required in (
    "/readyz", "healthy", "degraded", "unavailable", "pod restart",
    "## Control-plane release steps", "Never run the same deployment on two boxes",
    "Evict while disconnected", "deployment's `/state`", "cascade-tombstoned sequence",
    "Exceed 1,000 dead letters", "different multipart boundaries",
    "outbox.sqlite", "SENTINEL_DELIVERY_MODE", "X-Sentry-Placement-Generation",
    "runtime responsibility", "fsync", "outbox_created_at", "hostname validation",
    "deterministic files", "no committed evidence",
    "resolved_by_remote_receipt", "ReadWriteOnce", "stable stream ID",
):
    assert required in handoff, f"missing handoff requirement: {required}"

print(f"PASS: {len(schemas)} schemas, {len(events)} event examples, V7 app catalog, "
      "cross-references, recovery/eviction gates, per-deployment delivery, "
      "and stale-protocol checks")
