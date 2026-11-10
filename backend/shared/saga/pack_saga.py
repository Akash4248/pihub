from __future__ import annotations

import logging
from typing import Any

from shared.saga.orchestrator import (
    SagaDefinition,
    SagaOrchestrator,
    SagaStep,
    get_saga_orchestrator,
)

logger = logging.getLogger(__name__)

PACK_PUBLICATION_SAGA_TYPE = "pack.publication.saga"


def register_pack_publication_saga(
    orchestrator: SagaOrchestrator | None = None,
    pack_create_fn: Any = None,
    pack_revert_fn: Any = None,
    pipeline_index_fn: Any = None,
    pipeline_unindex_fn: Any = None,
    pihub_register_fn: Any = None,
    pihub_unregister_fn: Any = None,
) -> SagaDefinition:
    """Builds and registers the distributed Pack Publication Saga."""
    orchestrator = orchestrator or get_saga_orchestrator()

    # Step 1: Pack Service creates draft learning pack
    async def step1_exec(ctx: dict[str, Any]) -> dict[str, Any]:
        pack_id = ctx.get("pack_id", "pack_001")
        if pack_create_fn:
            return await pack_create_fn(ctx)
        logger.info("[SAGA_STEP:1] Pack service created pack record: %s", pack_id)
        return {"pack_created": True, "pack_id": pack_id, "status": "draft"}

    async def step1_comp(ctx: dict[str, Any]) -> None:
        pack_id = ctx.get("pack_id")
        if pack_revert_fn:
            await pack_revert_fn(ctx)
        logger.warning("[SAGA_COMP:1] Marked pack %s as VOID/ARCHIVED", pack_id)

    # Step 2: Content Pipeline indexes educational content
    async def step2_exec(ctx: dict[str, Any]) -> dict[str, Any]:
        if pipeline_index_fn:
            return await pipeline_index_fn(ctx)
        logger.info("[SAGA_STEP:2] Content Pipeline indexed chunks for pack: %s", ctx.get("pack_id"))
        return {"content_indexed": True, "chunks_indexed": 42}

    async def step2_comp(ctx: dict[str, Any]) -> None:
        if pipeline_unindex_fn:
            await pipeline_unindex_fn(ctx)
        logger.warning("[SAGA_COMP:2] Purged indexed vectors/chunks for pack: %s", ctx.get("pack_id"))

    # Step 3: PiHub registers pack availability for classroom
    async def step3_exec(ctx: dict[str, Any]) -> dict[str, Any]:
        if pihub_register_fn:
            return await pihub_register_fn(ctx)
        logger.info("[SAGA_STEP:3] PiHub registered pack in classroom: %s", ctx.get("classroom_id", "class_1"))
        return {"classroom_registered": True}

    async def step3_comp(ctx: dict[str, Any]) -> None:
        if pihub_unregister_fn:
            await pihub_unregister_fn(ctx)
        logger.warning("[SAGA_COMP:3] Unregistered pack from classroom catalog: %s", ctx.get("pack_id"))

    definition = SagaDefinition(PACK_PUBLICATION_SAGA_TYPE)
    definition.add_step(
        SagaStep(name="create_pack_record", service="pack-service"),
        step1_exec,
        step1_comp,
    )
    definition.add_step(
        SagaStep(name="index_pack_content", service="content-pipeline"),
        step2_exec,
        step2_comp,
    )
    definition.add_step(
        SagaStep(name="register_classroom_pack", service="pihub"),
        step3_exec,
        step3_comp,
    )

    orchestrator.register(definition)
    return definition
