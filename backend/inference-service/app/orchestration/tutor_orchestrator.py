from __future__ import annotations

import time
import logging
from typing import Any, AsyncIterator, Awaitable, Callable

logger = logging.getLogger(__name__)

from pydantic import BaseModel, Field

from app.context.experiment_context_provider import ExperimentContextProvider
from app.context.pack_context_provider import PackContextProvider
from app.language.language_adapter import LanguageAdapter
from app.monitoring.tutor_monitor import TutorLatencyBreakdown
from app.sessions.session_manager import SessionManager
from app.tutor.prompting import append_experiment_context, append_language_instruction, append_no_context_note


BuildSystemPromptFn = Callable[[Any, str], str]
BuildUserPromptFn = Callable[[Any, list[Any]], str]
ChatCompletionFn = Callable[[str, str, Any, bool], Awaitable[Any]]


class OrchestratedTutorResult(BaseModel):
    answer: str = ""
    language: str
    model: str
    context: list[Any] = Field(default_factory=list)
    stream: AsyncIterator[str] | None = None
    retrieval_diagnostics: dict[str, Any] = Field(default_factory=dict)
    metrics: TutorLatencyBreakdown
    session_id: str
    session_context: dict[str, Any] = Field(default_factory=dict)
    experiment_context: dict[str, Any] = Field(default_factory=dict)
    final_prompt: str = ""
    system_prompt: str = ""
    user_prompt: str = ""
    chunks_retrieved: int = 0
    chunks_used: int = 0

    model_config = {"arbitrary_types_allowed": True}


class TutorOrchestrator:
    def __init__(
        self,
        session_manager: SessionManager,
        pack_context_provider: PackContextProvider,
        experiment_context_provider: ExperimentContextProvider,
        language_adapter: LanguageAdapter,
        build_system_prompt: BuildSystemPromptFn,
        build_user_prompt: BuildUserPromptFn,
        chat_completion: ChatCompletionFn,
        active_model: Callable[[], str],
    ) -> None:
        self.session_manager = session_manager
        self.pack_context_provider = pack_context_provider
        self.experiment_context_provider = experiment_context_provider
        self.language_adapter = language_adapter
        self.build_system_prompt = build_system_prompt
        self.build_user_prompt = build_user_prompt
        self.chat_completion = chat_completion
        self.active_model = active_model

    async def run(self, request: Any) -> OrchestratedTutorResult:
        total_start = time.perf_counter()
        sim_ctx = getattr(request, "simulation_context", {})
        logger.info(f"[ORCHESTRATOR] Received simulation_context: {sim_ctx}")
        session = self.session_manager.get_or_create(request)
        target_language = self.language_adapter.detect_language(request)

        tutor_context, context_latency_ms = await self.pack_context_provider.load(request)
        experiment_context, experiment_context_ms = await self.experiment_context_provider.load(request, session)
        context_latency_ms += experiment_context_ms

        retrieval_diagnostics = tutor_context.retrieval_diagnostics
        context_results = tutor_context.context_results

        system_prompt = self.build_system_prompt(request, getattr(request, "hint_style", "guided"))
        if not context_results:
            system_prompt = append_no_context_note(system_prompt)
        system_prompt = append_language_instruction(system_prompt, self.language_adapter.prompt_instruction(target_language))

        user_prompt = self.build_user_prompt(request, context_results)
        user_prompt = append_experiment_context(user_prompt, experiment_context)
        final_prompt = f"SYSTEM:\n{system_prompt}\n\nUSER:\n{user_prompt}"

        if getattr(request, "stream", False):
            stream = await self.chat_completion(system_prompt, user_prompt, request, True)
            metrics = TutorLatencyBreakdown(
                context_latency_ms=round(context_latency_ms, 2),
                total_response_latency_ms=round((time.perf_counter() - total_start) * 1000, 2),
            )
            return OrchestratedTutorResult(
                language=target_language,
                model=self.active_model(),
                context=context_results,
                stream=stream,
                retrieval_diagnostics=retrieval_diagnostics,
                metrics=metrics,
                session_id=session.session_id,
                session_context=session.model_dump(),
                experiment_context=experiment_context.model_dump(),
                final_prompt=final_prompt,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                chunks_retrieved=int(retrieval_diagnostics.get("chunks_retrieved", len(context_results))),
                chunks_used=int(retrieval_diagnostics.get("chunks_used", len(context_results))),
            )

        tutor_start = time.perf_counter()
        raw_answer = await self.chat_completion(system_prompt, user_prompt, request, False)
        tutor_latency_ms = (time.perf_counter() - tutor_start) * 1000

        adapted_answer, adapter_latency_ms = await self.language_adapter.adapt(
            str(raw_answer),
            target_language,
            request,
            lambda system, user, req: self.chat_completion(system, user, req, False),
        )
        self.session_manager.append_turn(session.session_id, getattr(request, "question", ""), adapted_answer)
        total_ms = (time.perf_counter() - total_start) * 1000

        return OrchestratedTutorResult(
            answer=adapted_answer,
            language=target_language,
            model=self.active_model(),
            context=context_results,
            retrieval_diagnostics=retrieval_diagnostics,
            metrics=TutorLatencyBreakdown(
                tutor_latency_ms=round(tutor_latency_ms, 2),
                context_latency_ms=round(context_latency_ms, 2),
                language_adapter_latency_ms=round(adapter_latency_ms, 2),
                total_response_latency_ms=round(total_ms, 2),
            ),
            session_id=session.session_id,
            session_context=session.model_dump(),
            experiment_context=experiment_context.model_dump(),
            final_prompt=final_prompt,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            chunks_retrieved=int(retrieval_diagnostics.get("chunks_retrieved", len(context_results))),
            chunks_used=int(retrieval_diagnostics.get("chunks_used", len(context_results))),
        )
