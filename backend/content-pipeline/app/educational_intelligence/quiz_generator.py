from __future__ import annotations

import re
from typing import Any

from app.educational_intelligence.artifact_cleaning import build_mcq_options, clean_text, is_meaningful_term
from app.educational_intelligence.glossary_extractor import GlossaryExtractor
from app.educational_intelligence.inference_client import InferenceClient
from shared.text_normalization import normalize_language_code


class QuizGenerator:
    """Generate lightweight educational quizzes from chunks and glossary entries.

    Uses inference-service AI endpoint as the primary method for
    higher-quality questions with proper distractors.
    Falls back to template-based MCQ generation from glossary terms.
    """

    def __init__(self, inference_client: InferenceClient | None = None) -> None:
        self.inference = inference_client or InferenceClient()
        self.glossary_extractor = GlossaryExtractor(inference_client=inference_client)

    async def generate(self, chunks: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
        metadata = chunks[0].get("metadata", {}) if chunks else {}
        text_parts: list[str] = []
        concepts: list[str] = []
        for chunk in chunks[:10]:
            t = clean_text(str(chunk.get("text", "")))
            if t:
                text_parts.append(t)
            for c in (chunk.get("metadata", {}).get("concepts") or chunk.get("metadata", {}).get("topics") or []):
                if c not in concepts:
                    concepts.append(c)

        content = "\n\n".join(text_parts)[:8000]
        title = metadata.get("chapter") or "Untitled"

        ai_result = await self.inference.generate_quiz(
            title=title,
            content=content,
            concepts=concepts[:20],
            grade=metadata.get("grade"),
            subject=metadata.get("subject"),
            chapter=metadata.get("chapter"),
            language=metadata.get("language"),
        )
        ai_quizzes: list[dict[str, Any]] = []
        items_list = (ai_result.get("items") if ai_result else None) or (ai_result.get("quizzes") if ai_result else None)
        if items_list:
            ai_quizzes = [
                {
                    "question_type": "mcq",
                    "question": clean_text(str(item.get("question", ""))),
                    "options": [clean_text(str(o)) for o in item.get("options", [])],
                    "answer": clean_text(str(item.get("correct_answer") or item.get("answer", ""))),
                    "explanation": clean_text(str(item.get("explanation", ""))),
                    "chapter": metadata.get("chapter"),
                    "subject": metadata.get("subject"),
                    "language": metadata.get("language"),
                    "generated_by": "inference-service",
                }
                for item in items_list
                if clean_text(str(item.get("question", "")))
            ]
        if len(ai_quizzes) >= limit:
            return ai_quizzes[:limit]

        heuristic_quizzes = await self._heuristic_generate(chunks, limit=limit - len(ai_quizzes))
        existing_questions = {q["question"].lower() for q in ai_quizzes}
        for hq in heuristic_quizzes:
            if hq["question"].lower() not in existing_questions:
                ai_quizzes.append(hq)
                existing_questions.add(hq["question"].lower())

        return ai_quizzes[:limit]

    async def _heuristic_generate(self, chunks: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
        glossary = await self.glossary_extractor.extract(chunks)
        quizzes: list[dict[str, Any]] = []
        language = self._infer_language(chunks, glossary)
        definitions = [
            clean_text(str(entry.get("definition") or ""))
            for entry in glossary
            if clean_text(str(entry.get("definition") or ""))
        ]

        tf_toggle = True
        for entry in glossary:
            term = clean_text(str(entry.get("term") or ""))
            definition = clean_text(str(entry.get("definition") or ""))
            if not term or not definition or not is_meaningful_term(term):
                continue
            distractors = self._build_distractors(definition, definitions, language)
            options = build_mcq_options(definition, distractors, limit=4, language=language)
            quizzes.append({
                "question_type": "mcq",
                "question": self._question_template(term, language),
                "options": options,
                "answer": definition,
                "chapter": entry.get("chapter"),
                "subject": entry.get("subject"),
                "language": entry.get("language") or language,
                "generated_by": "heuristic",
            })

            # Balanced True/False questions (both true formulation and plausible falsification)
            quizzes.append({
                "question_type": "true_false",
                "question": f"True or False: {definition}",
                "answer": True,
                "chapter": entry.get("chapter"),
                "subject": entry.get("subject"),
                "language": entry.get("language") or language,
                "generated_by": "heuristic",
            })
            false_statement = self._negate_statement(definition)
            quizzes.append({
                "question_type": "true_false",
                "question": f"True or False: {false_statement}",
                "answer": False,
                "chapter": entry.get("chapter"),
                "subject": entry.get("subject"),
                "language": entry.get("language") or language,
                "generated_by": "heuristic",
            })

            # Fill-in-the-blank (only when term exists as standalone word)
            if re.search(rf"\b{re.escape(term)}\b", definition, flags=re.IGNORECASE):
                blanked = re.sub(rf"\b{re.escape(term)}\b", "______", definition, count=1, flags=re.IGNORECASE)
                quizzes.append({
                    "question_type": "fill_blank",
                    "question": f"Fill in the blank: {blanked}",
                    "answer": term,
                    "chapter": entry.get("chapter"),
                    "subject": entry.get("subject"),
                    "language": entry.get("language") or language,
                    "generated_by": "heuristic",
                })
            if len(quizzes) >= max(limit * 3, 3):
                break

        return quizzes[: max(limit * 3, 3)]

    @staticmethod
    def _negate_statement(text: str) -> str:
        s = text.strip().rstrip(".")
        replacements = [
            (r"\bis directly proportional to\b", "is inversely proportional to"),
            (r"\bis always\b", "is never"),
            (r"\bcan always\b", "cannot"),
            (r"\bcan\b", "cannot"),
            (r"\bincreases\b", "decreases"),
            (r"\bdecreases\b", "increases"),
            (r"\bhas no effect on\b", "directly determines"),
            (r"\bis defined as\b", "is completely unrelated to"),
            (r"\bis\b", "is not"),
            (r"\bare\b", "are not"),
        ]
        for pattern, repl in replacements:
            if re.search(pattern, s, re.IGNORECASE):
                return re.sub(pattern, repl, s, count=1, flags=re.IGNORECASE) + "."
        return f"It is never the case that {s.lower()}."

    def _build_distractors(self, answer: str, glossary_definitions: list[str], language: str) -> list[str]:
        distractors = []
        answer_l = answer.lower()
        for candidate in glossary_definitions:
            if candidate.lower() != answer_l and candidate not in distractors:
                distractors.append(candidate)
        if not distractors:
            distractors = self._localized_distractors(answer, language)
        return distractors

    @staticmethod
    def _infer_language(chunks: list[dict[str, Any]], glossary: list[dict[str, Any]]) -> str:
        for item in glossary:
            language = normalize_language_code(item.get("language"))
            if language:
                return language
        for chunk in chunks:
            language = normalize_language_code(chunk.get("metadata", {}).get("language"))
            if language:
                return language
        return "en"

    @staticmethod
    def _localized_distractors(answer: str, language: str) -> list[str]:
        if language == "kn" or re.search(r"[\u0C80-\u0CFF]", answer):
            return [
                "ಮತ್ತೊಂದು ಅಧ್ಯಾಯದಿಂದ ಸಂಬಂಧಿಸದ ಕಲ್ಪನೆ",
                "ಸಾಮಾನ್ಯ ಪಠ್ಯಪುಸ್ತಕ ಪದಬಳಕೆ",
                "ಸಂಪೂರ್ಣವಾಗಿ ಬೇರೆ ಕಲ್ಪನೆ",
                "ಹೊಂದಿಕೆಯಾಗದ ಹೇಳಿಕೆ",
            ]
        if language == "hi" or re.search(r"[\u0900-\u097F]", answer):
            return [
                "दूसरे अध्याय का असंबंधित विचार",
                "सामान्य पाठ्यपुस्तक वाक्यांश",
                "पूरी तरह अलग अवधारणा",
                "मिलान न करने वाला कथन",
            ]
        return [
            "An unrelated idea from another chapter",
            "A generic textbook phrase",
            "A different concept entirely",
            "A non-matching statement",
        ]

    @staticmethod
    def _question_template(term: str, language: str) -> str:
        if language == "kn":
            return f"{term} ಅನ್ನು ಅತ್ಯುತ್ತಮವಾಗಿ ವಿವರಿಸುವುದು ಯಾವುದು?"
        if language == "hi":
            return f"{term} का सबसे अच्छा वर्णन कौन सा है?"
        return f"What best describes {term}?"
