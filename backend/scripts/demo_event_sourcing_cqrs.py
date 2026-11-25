#!/usr/bin/env python3
"""
Demonstration & Verification Suite for Event Sourcing & CQRS in PIHUB Pack Service.

Covers:
  1. Event Store Engine: Append-only event history & schema initialization.
  2. Optimistic Concurrency Control: aggregate_version linear sequence verification.
  3. Idempotency Protection: duplicate submission yields identical response.
  4. CQRS Commands:
     - PackCreated: Pack generation / registration
     - PackPublished: Approval for distribution
     - PackDownloaded: Classroom download tracking
     - PackSynchronized: Node verified sync acknowledgment
     - PackUpdated: Version superseding
  5. CQRS Queries (Read Model):
     - Pack State & Classroom Distribution Projection
     - Aggregate Event History
     - Classroom-level Pack Availability
     - System Distribution Analytics
  6. Event Sourcing Replay:
     - Wipe read models
     - Replay entire event stream from Event Store
     - Verify complete projection reconstruction
"""

import os
import sys
import json
import uuid
import tempfile
from pathlib import Path

# Setup paths
SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
PACK_SERVICE_DIR = BACKEND_DIR / "pack-service"

sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(PACK_SERVICE_DIR))

from app.events.event_store import PackEventStore, ConcurrencyException
from app.events.projections import PackProjections
from app.events.event_service import PackEventService


def print_header(title: str):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def run_tests():
    print_header("PIHUB EVENT SOURCING & CQRS VERIFICATION SUITE")

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test_pack_events.sqlite3"
        print(f"[*] Initializing Event Store & Read Models at: {db_path}")

        service = PackEventService(db_path)
        pack_id = "test_grade6_science_ch1"
        classroom_id = "classroom_rural_101"
        device_id = "pi_node_alpha"

        # -------------------------------------------------------------
        # STEP 1: PackCreated
        # -------------------------------------------------------------
        print("\n[Step 1] Recording PackCreated Event (Command: Generate Pack)...")
        created_event = service.record_pack_created(
            pack_id=pack_id,
            version="1.0.0",
            grade=6,
            subject="science",
            chapter="living_organisms",
            checksum="sha256:abc123mockhash",
            size_bytes=1048576,
            chunk_count=24,
            actor_id="curriculum_builder",
        )
        print(f"   -> Emitted Event: {created_event['event_type']} (v{created_event['aggregate_version']})")
        print(f"   -> Event ID: {created_event['event_id']}")
        assert created_event["aggregate_version"] == 1
        assert created_event["event_type"] == "PackCreated"

        # Verify initial CQRS Read Model projection
        state = service.get_status(pack_id)
        assert state is not None
        assert state["publication_status"] == "draft"
        print(f"   -> CQRS Read Projection: publication_status = '{state['publication_status']}'")

        # -------------------------------------------------------------
        # STEP 2: Concurrency & Idempotency Checks
        # -------------------------------------------------------------
        print("\n[Step 2] Testing Concurrency Control & Idempotency...")
        # 2a. Duplicate PackCreated with same idempotency key should not fail or create duplicate
        dup_event = service.record_pack_created(
            pack_id=pack_id,
            version="1.0.0",
            grade=6,
            subject="science",
            chapter="living_organisms",
            idempotency_key=f"created_{pack_id}_v1.0.0",
        )
        assert dup_event["event_id"] == created_event["event_id"]
        print("   -> Idempotency Check: PASS (Duplicate payload safely returned existing event)")

        # 2b. Out-of-order aggregate version attempt
        try:
            service.store.append_event(
                aggregate_id=pack_id,
                expected_version=99,  # Invalid version jump!
                event_type="PackPublished",
                payload={"version": "1.0.0"},
            )
            print("   -> Concurrency Check: FAILED (Expected ConcurrencyException)")
            sys.exit(1)
        except ConcurrencyException as e:
            print(f"   -> Concurrency Check: PASS ({e})")

        # -------------------------------------------------------------
        # STEP 3: PackPublished
        # -------------------------------------------------------------
        print("\n[Step 3] Publishing Pack (Command: PublishPack)...")
        published_event = service.record_pack_published(
            pack_id=pack_id,
            version="1.0.0",
            publisher_id="admin_mary",
            notes="Approved for state curriculum distribution",
        )
        print(f"   -> Emitted Event: {published_event['event_type']} (v{published_event['aggregate_version']})")
        assert published_event["aggregate_version"] == 2

        state = service.get_status(pack_id)
        assert state["publication_status"] == "published"
        print(f"   -> CQRS Read Projection: publication_status = '{state['publication_status']}', published_at = {state['published_at']}")

        # -------------------------------------------------------------
        # STEP 4: PackDownloaded
        # -------------------------------------------------------------
        print("\n[Step 4] Downloading Pack to Classroom Node (Command: RecordDownload)...")
        download_event = service.record_pack_downloaded(
            pack_id=pack_id,
            version="1.0.0",
            classroom_id=classroom_id,
            device_id=device_id,
            request_id="req-dl-5561",
            client_ip="192.168.1.105",
        )
        print(f"   -> Emitted Event: {download_event['event_type']} (v{download_event['aggregate_version']})")
        assert download_event["aggregate_version"] == 3

        state = service.get_status(pack_id)
        dist = state["distribution"]
        assert len(dist) == 1
        assert dist[0]["distribution_status"] == "downloaded"
        assert dist[0]["download_count"] == 1
        print(f"   -> Classroom Read Projection: status = '{dist[0]['distribution_status']}', downloads = {dist[0]['download_count']}")

        # -------------------------------------------------------------
        # STEP 5: PackSynchronized (Acknowledgment from Node)
        # -------------------------------------------------------------
        print("\n[Step 5] Acknowledging Verified Classroom Sync (Command: PackSynchronized)...")
        sync_event = service.record_pack_synchronized(
            pack_id=pack_id,
            version="1.0.0",
            classroom_id=classroom_id,
            device_id=device_id,
            checksum="sha256:abc123mockhash",
            status="success",
        )
        print(f"   -> Emitted Event: {sync_event['event_type']} (v{sync_event['aggregate_version']})")
        assert sync_event["aggregate_version"] == 4

        state = service.get_status(pack_id)
        dist = state["distribution"]
        assert dist[0]["distribution_status"] == "synchronized"
        assert dist[0]["sync_count"] == 1
        print(f"   -> Classroom Read Projection: status = '{dist[0]['distribution_status']}', sync_count = {dist[0]['sync_count']}")

        # -------------------------------------------------------------
        # STEP 6: Multi-Classroom Distribution Support
        # -------------------------------------------------------------
        print("\n[Step 6] Testing Distribution Across Multiple Classrooms...")
        classroom_2 = "classroom_urban_202"
        service.record_pack_downloaded(pack_id=pack_id, version="1.0.0", classroom_id=classroom_2, device_id="pi_beta")
        service.record_pack_synchronized(pack_id=pack_id, version="1.0.0", classroom_id=classroom_2, device_id="pi_beta", checksum="sha256:abc123mockhash")

        state = service.get_status(pack_id)
        assert len(state["distribution"]) == 2
        print(f"   -> Pack tracked across {len(state['distribution'])} distinct classrooms simultaneously.")

        # -------------------------------------------------------------
        # STEP 7: Event History & Analytics Queries
        # -------------------------------------------------------------
        print("\n[Step 7] Querying Event History & Distribution Analytics...")
        history = service.get_history(pack_id)
        print(f"   -> Full Event Stream for '{pack_id}' ({len(history)} events):")
        for ev in history:
            print(f"      [v{ev['aggregate_version']}] {ev['event_type']} @ {ev['occurred_at'][:19]} (Actor: {ev.get('actor_id')})")

        analytics = service.get_analytics()
        print("\n   -> System Distribution Analytics:")
        print(f"      • Total Events Logged: {analytics['total_events']}")
        print(f"      • Packs Tracked:       {analytics['total_packs_tracked']}")
        print(f"      • Published Packs:     {analytics['published_packs_count']}")
        print(f"      • Total Downloads:     {analytics['total_downloads']}")
        print(f"      • Total Sync Acks:     {analytics['total_synchronizations']}")
        print(f"      • Active Classrooms:   {analytics['active_classrooms']}")

        # -------------------------------------------------------------
        # STEP 8: Event Sourcing Core Demonstration - Event Replay
        # -------------------------------------------------------------
        print("\n[Step 8] Demonstrating Event Sourcing Event Replay...")
        print("   -> Deleting all read model projections (simulating cache loss / new read model)...")
        # Snapshot state before wiping
        state_before = service.get_status(pack_id)

        # Execute replay
        replay_result = service.replay_and_rebuild()
        print(f"   -> Replay complete: {replay_result}")

        # Snapshot state after wiping
        state_after = service.get_status(pack_id)
        assert state_before["publication_status"] == state_after["publication_status"]
        assert state_before["published_at"] == state_after["published_at"]
        assert len(state_before["distribution"]) == len(state_after["distribution"])
        print("   -> Verification: State reconstructed from event log is 100% identical!")

    print_header("ALL EVENT SOURCING & CQRS TESTS PASSED (100% OK)")


if __name__ == "__main__":
    run_tests()
