from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from shared.auth import issue_service_token, verify_service_token
from shared.db.ownership import (
    DATA_OWNERSHIP_CATALOG,
    ServiceDomain,
    get_service_db_connection,
    init_all_service_databases,
)
from shared.discovery import ServiceInstance, get_service_registry
from shared.events import (
    Event,
    EventBus,
    EventType,
    IdempotencyStore,
    ResilientConsumer,
    TransactionalOutbox,
)
from shared.resilience import (
    CircuitBreaker,
    CircuitBreakerOpenException,
    CircuitState,
    RateLimiter,
    ResponseCache,
    get_circuit_breaker_registry,
)
from shared.saga import (
    PACK_PUBLICATION_SAGA_TYPE,
    SagaStatus,
    get_saga_orchestrator,
    register_pack_publication_saga,
)
from shared.tracing import Span, generate_span_id, generate_trace_id, get_tracer

# Ensure gateway path is configured for importing gateway app
import sys
from pathlib import Path
_gateway_dir = str(Path(__file__).resolve().parents[1] / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

from app.main import app as gateway_app


# =====================================================================
# 1. RabbitMQ Event Bus & Event-Driven Architecture Tests
# =====================================================================

@pytest.mark.asyncio
async def test_event_bus_publish_and_subscribe():
    """Verify asynchronous event publication and consumption across services."""
    bus = EventBus()
    received_events: list[Event] = []

    async def on_pack_created(evt: Event) -> None:
        received_events.append(evt)

    await bus.subscribe(EventType.PACK_CREATED.value, on_pack_created)

    test_event = Event(
        event_type=EventType.PACK_CREATED.value,
        producer="pack-service",
        correlation_id="corr-1234",
        payload={"pack_id": "math_grade_8", "title": "Grade 8 Mathematics"},
    )
    success = await bus.publish(test_event)

    assert success is True
    assert len(received_events) == 1
    assert received_events[0].event_id == test_event.event_id
    assert received_events[0].payload["pack_id"] == "math_grade_8"
    assert received_events[0].producer == "pack-service"


@pytest.mark.asyncio
async def test_event_bus_pattern_wildcard_matching():
    """Verify topic exchange pattern matching (e.g. 'pack.*' matches 'pack.created')."""
    bus = EventBus()
    events: list[Event] = []

    await bus.subscribe("pack.*", lambda e: events.append(e))

    await bus.publish(Event(event_type=EventType.PACK_CREATED.value, producer="pack", payload={"id": 1}))
    await bus.publish(Event(event_type=EventType.PACK_UPDATED.value, producer="pack", payload={"id": 2}))
    await bus.publish(Event(event_type=EventType.DEVICE_REGISTERED.value, producer="pihub", payload={"id": 3}))

    assert len(events) == 2
    assert {e.payload["id"] for e in events} == {1, 2}


# =====================================================================
# 2. Transactional Outbox Pattern Tests
# =====================================================================

@pytest.mark.asyncio
async def test_transactional_outbox_staging_and_draining(tmp_path):
    """Verify atomic local SQLite staging of events and reliable asynchronous publishing."""
    db_file = tmp_path / "test_outbox.sqlite3"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE dummy_business (id TEXT PRIMARY KEY, val TEXT)")

    test_evt = Event(
        event_type=EventType.DEVICE_REGISTERED.value,
        producer="pihub",
        payload={"device_id": "tablet-04", "classroom": "Room 101"},
    )

    # Simulate atomic local transaction
    with conn:
        conn.execute("INSERT INTO dummy_business VALUES ('1', 'registered')")
        TransactionalOutbox.stage_event(conn, test_evt)

    # Verify event is in PENDING state in the DB
    cur = conn.execute("SELECT status, event_id FROM outbox_events WHERE event_id = ?", (test_evt.event_id,))
    row = cur.fetchone()
    assert row is not None
    assert row[0] == "PENDING"
    conn.close()

    # Drain pending outbox events to bus
    bus = EventBus()
    consumed: list[Event] = []
    await bus.subscribe(EventType.DEVICE_REGISTERED.value, lambda e: consumed.append(e))

    published_count = await TransactionalOutbox.publish_pending(db_file, bus)
    assert published_count == 1
    assert len(consumed) == 1
    assert consumed[0].payload["device_id"] == "tablet-04"

    # Verify status changed to PUBLISHED
    conn_verify = sqlite3.connect(str(db_file))
    status_row = conn_verify.execute("SELECT status FROM outbox_events WHERE event_id = ?", (test_evt.event_id,)).fetchone()
    assert status_row[0] == "PUBLISHED"
    conn_verify.close()


# =====================================================================
# 3. Idempotent Consumer & Deduplication Tests
# =====================================================================

@pytest.mark.asyncio
async def test_idempotent_consumer_skips_duplicates(tmp_path):
    """Verify that sending a duplicate event does not duplicate business execution."""
    store = IdempotencyStore(tmp_path / "idempotency.sqlite3")
    bus = EventBus()
    consumer = ResilientConsumer(consumer_id="analytics-service", bus=bus, idempotency_store=store)

    execution_count = 0

    async def process_event(evt: Event) -> None:
        nonlocal execution_count
        execution_count += 1

    wrapped = consumer.wrap_handler(process_event)

    duplicate_event = Event(
        event_id="evt-fixed-uuid-101",
        event_type=EventType.QUIZ_COMPLETED.value,
        producer="pihub",
        payload={"score": 95},
    )

    # First delivery
    await wrapped(duplicate_event)
    assert execution_count == 1
    assert store.is_processed("evt-fixed-uuid-101", "analytics-service") is True

    # Duplicate delivery
    await wrapped(duplicate_event)
    assert execution_count == 1  # Should not increase!


# =====================================================================
# 4. Circuit Breaker Pattern & Fault Tolerance Tests
# =====================================================================

@pytest.mark.asyncio
async def test_circuit_breaker_lifecycle():
    """Verify CLOSED -> OPEN (on failures) -> Fallback / Fast-Fail -> HALF-OPEN -> CLOSED (on recovery)."""
    cb = CircuitBreaker("inference-service", failure_threshold=3, recovery_timeout=0.1, half_open_max_calls=1)

    async def faulty_downstream(fail: bool):
        if fail:
            raise RuntimeError("Model timeout")
        return {"response": "photosynthesis explained"}

    # Initially CLOSED
    assert cb.state == CircuitState.CLOSED
    res = await cb.call(faulty_downstream, False)
    assert res["response"] == "photosynthesis explained"

    # Trigger failures to breach threshold
    for _ in range(3):
        try:
            await cb.call(faulty_downstream, True)
        except RuntimeError:
            pass

    # Circuit should now be OPEN
    assert cb.state == CircuitState.OPEN

    # Requests while OPEN fail immediately without swamping downstream
    with pytest.raises(CircuitBreakerOpenException):
        await cb.call(faulty_downstream, False)

    # Wait for recovery timeout
    await asyncio.sleep(0.12)

    # Next call probe transitions to HALF_OPEN and succeeds -> closes circuit
    probe_res = await cb.call(faulty_downstream, False)
    assert probe_res["response"] == "photosynthesis explained"
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_breaker_with_graceful_fallback():
    """Verify fallback factory returns graceful degraded response when open."""
    fallback_data = {"status": "degraded", "cached_answer": "Fallback curriculum definition"}
    cb = CircuitBreaker(
        "content-pipeline",
        failure_threshold=1,
        recovery_timeout=5.0,
        fallback_factory=lambda *a, **kw: fallback_data,
    )

    async def fail_call():
        raise ConnectionRefusedError("Content pipeline unreachable")

    # Trip breaker
    res = await cb.call(fail_call)
    assert res == fallback_data
    assert cb.state == CircuitState.OPEN

    # Second call uses fallback without attempting network
    res2 = await cb.call(fail_call)
    assert res2 == fallback_data


# =====================================================================
# 5. Service Discovery & Dynamic Load Balancing Tests
# =====================================================================

def test_service_discovery_registration_and_load_balancing():
    """Verify dynamic instance registration, round-robin load distribution, and unhealthy removal."""
    registry = get_service_registry()
    registry.register("inference-service", "inf-node-1", "http://10.0.0.1:8010")
    registry.register("inference-service", "inf-node-2", "http://10.0.0.2:8010")

    # Round Robin balancing between healthy nodes
    url1 = registry.resolve("inference-service")
    url2 = registry.resolve("inference-service")
    assert {url1, url2} == {"http://10.0.0.1:8010", "http://10.0.0.2:8010"}

    # Mark node 1 as unhealthy
    registry.mark_unhealthy("inference-service", "inf-node-1")
    url3 = registry.resolve("inference-service")
    assert url3 == "http://10.0.0.2:8010"

    # Mark node 1 healthy again
    registry.mark_healthy("inference-service", "inf-node-1")
    instances = registry.get_instances("inference-service", healthy_only=True)
    assert len(instances) == 2


# =====================================================================
# 6. Distributed Tracing & W3C Traceparent Propagation Tests
# =====================================================================

def test_distributed_tracing_span_generation_and_w3c_header():
    """Verify W3C traceparent generation and span measurement."""
    tracer = get_tracer("gateway")
    span = tracer.start_span("process_tutor_request", attributes={"subject": "biology"})
    time.sleep(0.01)
    tracer.end_span(span, status="OK")

    assert span.duration_ms > 0
    assert span.trace_id is not None
    assert len(span.trace_id) == 32
    assert span.span_id is not None
    assert len(span.span_id) == 16

    # Test W3C header injection
    headers: dict[str, str] = {}
    injected = tracer.inject_headers(headers, span)
    assert "traceparent" in injected
    assert injected["traceparent"].startswith("00-")
    assert span.trace_id in injected["traceparent"]


# =====================================================================
# 7. Inter-Service Authentication & Zero-Trust Security Tests
# =====================================================================

def test_service_token_issue_and_verify_success():
    """Verify signed service token authentication with valid permissions."""
    token = issue_service_token(issuer="gateway", audience="inference-service", permissions=["inference:generate"])
    claims = verify_service_token(token, required_permission="inference:generate")
    assert claims["iss"] == "gateway"
    assert "inference:generate" in claims["permissions"]


def test_service_token_rejects_unauthorized_permission():
    """Verify zero-trust enforcement: service lacking required permission is rejected with 403."""
    from fastapi import HTTPException
    token = issue_service_token(issuer="pihub", audience="inference-service", permissions=["pihub:sync"])
    with pytest.raises(HTTPException) as exc_info:
        verify_service_token(token, required_permission="inference:generate")
    assert exc_info.value.status_code == 403
    assert "lacks required permission" in exc_info.value.detail


# =====================================================================
# 8. Saga Pattern for Distributed Transactions Tests
# =====================================================================

@pytest.mark.asyncio
async def test_saga_orchestrator_success_path():
    """Verify orchestrated saga executes all steps and transitions to COMPLETED."""
    orchestrator = get_saga_orchestrator()
    orchestrator.registry.clear()
    register_pack_publication_saga(orchestrator)

    payload = {"pack_id": "test_pack_grade8", "classroom_id": "class_a"}
    record = await orchestrator.execute(PACK_PUBLICATION_SAGA_TYPE, payload)

    assert record.status == SagaStatus.COMPLETED
    assert len(record.completed_steps) == 3
    assert "create_pack_record" in record.completed_steps
    assert "index_pack_content" in record.completed_steps
    assert "register_classroom_pack" in record.completed_steps


@pytest.mark.asyncio
async def test_saga_orchestrator_failure_and_backward_compensation():
    """Verify that failure in Step 3 automatically triggers reverse compensating transactions."""
    orchestrator = get_saga_orchestrator()
    orchestrator.registry.clear()

    reverted_steps: list[str] = []

    async def mock_fail_pihub(ctx):
        raise RuntimeError("Classroom quota exceeded")

    async def mock_comp_pipeline(ctx):
        reverted_steps.append("content-pipeline")

    async def mock_comp_pack(ctx):
        reverted_steps.append("pack-service")

    register_pack_publication_saga(
        orchestrator,
        pack_revert_fn=mock_comp_pack,
        pipeline_unindex_fn=mock_comp_pipeline,
        pihub_register_fn=mock_fail_pihub,
    )

    record = await orchestrator.execute(PACK_PUBLICATION_SAGA_TYPE, {"pack_id": "fail_pack_01"})

    assert record.status == SagaStatus.COMPENSATED
    assert record.failed_step == "register_classroom_pack"
    # Crucial distributed transaction guarantee: reverse order compensation
    assert reverted_steps == ["content-pipeline", "pack-service"]


# =====================================================================
# 9. Database-per-Service Isolation Tests
# =====================================================================

def test_database_per_service_catalog_and_isolation():
    """Verify strict data ownership catalog and isolated schemas per service."""
    init_all_service_databases()

    for domain in (ServiceDomain.PIHUB, ServiceDomain.PACK_SERVICE, ServiceDomain.CONTENT_PIPELINE):
        conn = get_service_db_connection(domain)
        # Verify connection operates in WAL mode for microservice concurrency
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert mode.lower() == "wal"
        conn.close()

    assert ServiceDomain.PIHUB in DATA_OWNERSHIP_CATALOG
    assert "students" in DATA_OWNERSHIP_CATALOG[ServiceDomain.PIHUB]["owned_entities"]
    assert "packs" in DATA_OWNERSHIP_CATALOG[ServiceDomain.PACK_SERVICE]["owned_entities"]


# =====================================================================
# 10. API Gateway Architecture Endpoints Tests
# =====================================================================

def test_gateway_architecture_endpoints():
    """Verify gateway exposes microservices architecture inspection endpoints."""
    client = TestClient(gateway_app)

    # 1. Circuit breakers monitoring
    cb_resp = client.get("/api/v1/system/circuit-breakers")
    assert cb_resp.status_code == 200
    assert "circuit_breakers" in cb_resp.json()

    # 2. Distributed Traces
    traces_resp = client.get("/api/v1/system/traces")
    assert traces_resp.status_code == 200
    assert "traces" in traces_resp.json()

    # 3. Dynamic Service Discovery
    disc_resp = client.get("/api/v1/discovery/services")
    assert disc_resp.status_code == 200
    assert "services" in disc_resp.json()

    # 4. Data Ownership Catalog
    ownership_resp = client.get("/api/v1/data-ownership")
    assert ownership_resp.status_code == 200
    assert ownership_resp.json()["pattern"] == "Database-per-Service"
