from __future__ import annotations

import re
from collections import Counter
from typing import Any

from shared.text_normalization import normalize_language_code

OCR_MARKERS = (
    "/square6",
    "\u25a1",
    "reprint 2025-26",
    "reprint 205-6",
    "chapter notes",
)

KANNADA_FALLBACKS = {
    "question": "ಪ್ರಶ್ನೆ",
    "related": "ಸಂಬಂಧಿತ",
    "unrelated": "ಸಂಬಂಧಿಸದ",
    "generic_phrase": "ಸಾಮಾನ್ಯ ಪಠ್ಯಪುಸ್ತಕ ಪದಬಳಕೆ",
    "incomplete_idea": "ಅಧ್ಯಾಯದ ಸಂಬಂಧಿತ ಆದರೆ ಅಪೂರ್ಣ ಕಲ್ಪನೆ",
    "different_concept": "ಸಂಪೂರ್ಣವಾಗಿ ಬೇರೆ ಕಲ್ಪನೆ",
    "non_matching": "ಹೊಂದಿಕೆಯಾಗದ ಹೇಳಿಕೆ",
}

GENERIC_PHRASES = (
    "only related to",
    "important idea studied",
    "important idea from the chapter notes",
    "review the chapter notes",
    "the following question",
    "what is ",
    "how do you use ",
    "concept:",
    "problem:",
    "step 1:",
    "step 2:",
)

GENERIC_TERMS = {
    "a", "an", "and", "are", "both", "correct", "example", "idea", "object", "paper",
    "reason", "size", "table", "figure", "chapter", "section", "page",
}


def normalize_text(text: str) -> str:
    value = str(text or "").replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def clean_text(text: str) -> str:
    value = normalize_text(text)
    value = value.replace("•", ". ")
    value = re.sub(r"\s*[/\\]square6\s*", " ", value, flags=re.I)
    value = re.sub(r"\bReprint\s+\d{3,4}-\d{1,2}\b", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -:;")


def sentence_split(text: str) -> list[str]:
    parts = [segment.strip() for segment in re.split(r"(?<=[.!?।])\s+", normalize_text(text)) if segment.strip()]
    return parts


def tokenize(text: str) -> list[str]:
    return re.findall(r"[\w\u0900-\u097F\u0C80-\u0CFF][\w\u0900-\u097F\u0C80-\u0CFF'/-]{1,}", normalize_text(text))


def is_noisy_text(text: str) -> bool:
    value = normalize_text(text).lower()
    if not value:
        return True
    if any(marker in value for marker in OCR_MARKERS):
        return True
    if any(phrase in value for phrase in GENERIC_PHRASES):
        return True
    if len(tokenize(value)) <= 1 and len(value) < 3:
        return True
    return False


def is_meaningful_term(term: str) -> bool:
    value = clean_text(term)
    if is_noisy_text(value):
        return False
    lowered = value.lower()
    if lowered in GENERIC_TERMS:
        return False
    if lowered.startswith(("what ", "how ", "why ", "the following", "chapter ", "concept ", "problem ", "step ", "both ")):
        return False
    if " assertion and reason" in lowered or " chapter notes" in lowered:
        return False
    if "/" in lowered and not re.search(r"[a-z]+/[a-z]+", lowered):
        return False
    return True


def dedupe_by_key(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        value = normalize_text(str(item.get(key) or "")).lower()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(item)
    return result


def pick_anchor_sentence(text: str, term: str | None = None) -> str:
    sentences = [sentence for sentence in sentence_split(clean_text(text)) if sentence and not is_noisy_text(sentence)]
    if not sentences:
        return clean_text(text)[:220]
    if term:
        term_l = term.lower()
        for sentence in sentences:
            if term_l in sentence.lower():
                return sentence
    scored: list[tuple[int, int, str]] = []
    for sentence in sentences:
        tokens = tokenize(sentence)
        score = len(tokens)
        if any(token.lower() in {"is", "are", "means", "refers", "shows", "forms", "helps", "requires"} for token in tokens):
            score += 4
        scored.append((score, len(sentence), sentence))
    scored.sort(reverse=True)
    return scored[0][2]


def extract_candidate_terms(text: str) -> list[str]:
    clean = clean_text(text)
    tokens = tokenize(clean)
    counts: Counter[str] = Counter()
    for token in tokens:
        cand = token.strip("-/").title()
        if len(cand) > 3 and is_meaningful_term(cand):
            counts[cand] += 1
    # Extract 2-gram and 3-gram key phrases (e.g. Electric Current, Focal Length)
    words = clean.split()
    for n in (2, 3):
        for i in range(len(words) - n + 1):
            ngram = " ".join(words[i : i + n]).strip(".,;:-_()[]")
            if (
                ngram
                and 4 < len(ngram) < 45
                and not is_noisy_text(ngram)
                and all(w.lower() not in GENERIC_TERMS for w in ngram.split())
                and is_meaningful_term(ngram)
            ):
                counts[ngram.title()] += 2
    return [term for term, _ in counts.most_common(25)]


def build_mcq_options(answer: str, distractor_pool: list[str], limit: int = 4, language: str | None = None) -> list[str]:
    options = [clean_text(answer)]
    language_code = normalize_language_code(language)
    for candidate in distractor_pool:
        candidate_clean = clean_text(candidate)
        if not candidate_clean or candidate_clean == options[0] or candidate_clean in options:
            continue
        options.append(candidate_clean)
        if len(options) >= limit:
            break

    # Contextual distractors when pool has insufficient candidates
    kn_plausible_distractors = [
        "ಕೇವಲ ನಿರ್ದಿಷ್ಟ ಬಾಹ್ಯ ಪರಿಸ್ಥಿತಿಗಳಲ್ಲಿ ಮಾತ್ರ ಅನ್ವಯಿಸುತ್ತದೆ",
        "ಭೌತಿಕ ಪ್ರಮಾಣದ ಮೇಲೆ ವಿಲೋಮವಾಗಿ ಅವಲಂಬಿತವಾಗಿದೆ",
        "ಬಾಹ್ಯ ಶಕ್ತಿಯ ಹರಿವಿಲ್ಲದೆ ಪ್ರಕ್ರಿಯೆ ಸಂಭವಿಸುವುದಿಲ್ಲ",
        "ಪ್ರಮಾಣಿತ ಉಷ್ಣಾಂಶ ಮತ್ತು ಒತ್ತಡದಲ್ಲಿ ಸ್ಥಿರವಾಗಿರುತ್ತದೆ",
    ]
    en_plausible_distractors = [
        "Applies exclusively under specific external conditions",
        "Depends inversely on the magnitude of the related variable",
        "Occurs only when sufficient external energy is transferred",
        "Remains unchanged regardless of system transformations",
    ]

    fallback_pool = kn_plausible_distractors if language_code == "kn" or re.search(r"[\u0C80-\u0CFF]", answer) else en_plausible_distractors
    for fallback in fallback_pool:
        if len(options) >= limit:
            break
        if fallback not in options:
            options.append(fallback)

    return options[:limit]
