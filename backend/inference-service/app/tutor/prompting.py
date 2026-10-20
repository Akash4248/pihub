from __future__ import annotations

import json
from typing import Any


def append_language_instruction(system_prompt: str, instruction: str) -> str:
    return f"{system_prompt}\n\n[Language Adaptation]\n{instruction}"


def append_experiment_context(user_prompt: str, experiment_context: Any) -> str:
    if not getattr(experiment_context, "has_context", False):
        return user_prompt

    lines = [
        "\nEXPERIMENT CONTEXT:",
        "Use this only when it helps answer the student's current experiment question.",
    ]
    if getattr(experiment_context, "experiment_id", None):
        lines.append(f"Experiment ID: {experiment_context.experiment_id}")
    if getattr(experiment_context, "current_variables", None):
        lines.append("Current variables: " + json.dumps(experiment_context.current_variables, ensure_ascii=False))
    if getattr(experiment_context, "current_observations", None):
        lines.append("Current observations: " + json.dumps(experiment_context.current_observations, ensure_ascii=False))
    if getattr(experiment_context, "active_investigation_step", None):
        lines.append("Active investigation step: " + json.dumps(experiment_context.active_investigation_step, ensure_ascii=False))
    return user_prompt + "\n" + "\n".join(lines)


#: Single relevance contract for retrieved context. Emitted at most once per
#: prompt (builders must not add their own copy).
RELEVANCE_INSTRUCTION = "IMPORTANT: Use provided context only if it is relevant and accurate."

#: Appended when retrieval returned nothing, instead of the relevance line.
NO_CONTEXT_NOTE = "[Note: No relevant educational context was found. Answer based on your knowledge.]"


def append_no_context_note(system_prompt: str) -> str:
    """Append the no-context signal exactly once."""
    if NO_CONTEXT_NOTE in system_prompt:
        return system_prompt
    return f"{system_prompt}\n{NO_CONTEXT_NOTE}"
