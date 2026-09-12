from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from shared.config import get_settings

logger = logging.getLogger(__name__)


class InferenceClient:
    """Async HTTP client for the inference-service AI content generation endpoints.

    Calls endpoints like /ai/content/summary, /ai/content/quiz, etc.
    Falls back gracefully when the inference service is unavailable.
    """

    def __init__(self, base_url: str | None = None, timeout: float = 60.0) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.inference_service_url).rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _post(
        self, endpoint: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        try:
            client = await self._get_client()
            response = await client.post(
                f"{self.base_url}{endpoint}", json=payload
            )
            if response.is_success:
                return response.json()
            logger.warning(
                "Inference service %s returned %s: %s",
                endpoint, response.status_code, response.text[:200],
            )
        except httpx.ConnectError:
            logger.warning("Inference service unreachable at %s", self.base_url)
        except httpx.TimeoutException:
            logger.warning("Inference service timeout on %s", endpoint)
        except Exception as exc:
            logger.warning("Inference service error on %s: %s", endpoint, str(exc)[:200])
        return None

    async def generate_summary(
        self,
        title: str,
        content: str,
        concepts: list[str] | None = None,
        grade: int | None = None,
        subject: str | None = None,
        chapter: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any] | None:
        return await self._post("/ai/content/summary", {
            "title": title,
            "content": content,
            "concepts": concepts or [],
            "grade": grade,
            "subject": subject,
            "chapter": chapter,
            "language": language,
        })

    async def generate_chapter_notes(
        self,
        title: str,
        content: str,
        concepts: list[str] | None = None,
        grade: int | None = None,
        subject: str | None = None,
        chapter: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any] | None:
        return await self._post("/ai/content/chapter-notes", {
            "title": title,
            "content": content,
            "concepts": concepts or [],
            "grade": grade,
            "subject": subject,
            "chapter": chapter,
            "language": language,
        })

    async def generate_flashcards(
        self,
        title: str,
        content: str,
        concepts: list[str] | None = None,
        grade: int | None = None,
        subject: str | None = None,
        chapter: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any] | None:
        return await self._post("/ai/content/flashcards", {
            "title": title,
            "content": content,
            "concepts": concepts or [],
            "grade": grade,
            "subject": subject,
            "chapter": chapter,
            "language": language,
        })

    async def generate_quiz(
        self,
        title: str,
        content: str,
        concepts: list[str] | None = None,
        grade: int | None = None,
        subject: str | None = None,
        chapter: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any] | None:
        return await self._post("/ai/content/quiz", {
            "title": title,
            "content": content,
            "concepts": concepts or [],
            "grade": grade,
            "subject": subject,
            "chapter": chapter,
            "language": language,
        })

    async def generate_glossary(
        self,
        title: str,
        content: str,
        concepts: list[str] | None = None,
        grade: int | None = None,
        subject: str | None = None,
        chapter: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any] | None:
        return await self._post("/ai/content/glossary", {
            "title": title,
            "content": content,
            "concepts": concepts or [],
            "grade": grade,
            "subject": subject,
            "chapter": chapter,
            "language": language,
        })

    async def generate_learning_objectives(
        self,
        title: str,
        content: str,
        concepts: list[str] | None = None,
        grade: int | None = None,
        subject: str | None = None,
        chapter: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any] | None:
        return await self._post("/ai/content/learning-objectives", {
            "title": title,
            "content": content,
            "concepts": concepts or [],
            "grade": grade,
            "subject": subject,
            "chapter": chapter,
            "language": language,
        })

    async def generate_misconceptions(
        self,
        title: str,
        content: str,
        concepts: list[str] | None = None,
        grade: int | None = None,
        subject: str | None = None,
        chapter: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any] | None:
        return await self._post("/ai/content/misconceptions", {
            "title": title,
            "content": content,
            "concepts": concepts or [],
            "grade": grade,
            "subject": subject,
            "chapter": chapter,
            "language": language,
        })

    async def _generate_text(self, prompt: str) -> str | None:
        """Send a raw text prompt to the inference service and return the response text.

        Used by the agentic orchestrator for self-critique and iterative improvement.
        The message is formatted as a user message; the inference service's /ai/chat
        endpoint handles system-prompt separation internally.
        """
        try:
            client = await self._get_client()
            response = await client.post(
                f"{self.base_url}/ai/chat",
                json={"question": prompt, "language": "en", "limit": 3},
                timeout=180.0,
            )
            if response.is_success:
                body = response.json()
                return body.get("answer")
            logger.warning(
                "_generate_text returned %s: %s",
                response.status_code, response.text[:200],
            )
        except httpx.ConnectError:
            logger.warning("Inference service unreachable for _generate_text")
        except httpx.TimeoutException:
            logger.warning("Inference service timeout on _generate_text")
        except Exception as exc:
            logger.warning("_generate_text error: %s", str(exc)[:200])
        return None

    async def generate_structured(
        self,
        prompt: str,
        response_model: type[Any],
        max_retries: int = 2,
    ) -> Any | None:
        """Generate structured response validated against a Pydantic response_model."""
        import re
        for attempt in range(max_retries + 1):
            raw_text = await self._generate_text(prompt)
            if not raw_text:
                continue
            try:
                clean_text = raw_text.strip()
                if clean_text.startswith("```"):
                    clean_text = re.sub(r"^```(?:json)?\s*\n?", "", clean_text)
                    clean_text = re.sub(r"\n?```\s*$", "", clean_text)
                # Try finding JSON block boundaries
                if not clean_text.startswith("{") and not clean_text.startswith("["):
                    start = min((pos for pos in (clean_text.find("{"), clean_text.find("[")) if pos >= 0), default=-1)
                    end = max(clean_text.rfind("}"), clean_text.rfind("]"))
                    if start >= 0 and end > start:
                        clean_text = clean_text[start : end + 1]
                data = json.loads(clean_text)
                return response_model.model_validate(data)
            except Exception as err:
                logger.warning(
                    "Structured validation attempt %d failed for %s: %s",
                    attempt + 1, response_model.__name__, err,
                )
        return None


    async def generate_applications(
        self,
        title: str,
        content: str,
        concepts: list[str] | None = None,
        grade: int | None = None,
        subject: str | None = None,
        chapter: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any] | None:
        return await self._post("/ai/content/applications", {
            "title": title,
            "content": content,
            "concepts": concepts or [],
            "grade": grade,
            "subject": subject,
            "chapter": chapter,
            "language": language,
        })
