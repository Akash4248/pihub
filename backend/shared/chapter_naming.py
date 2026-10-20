from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

CHAPTER = "chapter"
FRONT_MATTER = "front_matter"
REFERENCE = "reference"
FULL_TEXTBOOK = "full_textbook"

_OVERRIDES_PATH = Path(__file__).with_name("chapter_title_overrides.json")

_CHAPTER_PREFIX_PATTERN = re.compile(
    r"^\s*(?:chapter|unit|lesson|topic)\s*[-:#.]?\s*(?:\d{1,3}|[ivxlcdm]+)\b[\s.:\-–—]*",
    re.IGNORECASE,
)
_LEADING_NUMBER_PATTERN = re.compile(r"^\s*\d{1,3}\s*[.)\-:]\s+")
_TRAILING_PART_PATTERN = re.compile(r"\s*(?:\(\s*part\s*\d+\s*\)|[-_]\s*part\s*\d+)\s*$", re.IGNORECASE)
_YEAR_PATTERN = re.compile(r"\b(?:19|20)\d{2}\s*[-/]\s*\d{2,4}\b")
_WHITESPACE_PATTERN = re.compile(r"\s+")
_POSSESSIVE_PATTERN = re.compile(r"(?<=[A-Za-z])_s(?=_|\b)")

_SMALL_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into",
    "of", "on", "or", "the", "to", "vs", "with",
}
_ACRONYMS = {
    "cbse", "evs", "ict", "ncert", "ph", "un", "who",
}
_ROMAN_NUMERALS = {
    "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
}
_POSSESSIVES = {
    "natures": "nature's",
    "indias": "india's",
}

_EXACT_CLASSIFICATIONS = {
    "answers": REFERENCE,
    "contents": REFERENCE,
    "glossary": REFERENCE,
    "introduction": FRONT_MATTER,
    "curiosity": FULL_TEXTBOOK,
    "class_5_environmental_studies_evs": FULL_TEXTBOOK,
    "class_6_science_textbook": FULL_TEXTBOOK,
    "class_7_science_textbook_titled_curiosity": FULL_TEXTBOOK,
    "class_9_science_textbook_is_titled_science": REFERENCE,
    "class_9_science_textbook_is_titled_science_2": FULL_TEXTBOOK,
    "social_science_textbook_for_class_7_part_1": FULL_TEXTBOOK,
    "social_science_textbook_for_class_7_part_2": FULL_TEXTBOOK,
    "social_science_textbook_for_class_8": FULL_TEXTBOOK,
    "democratic_politics_class_9": FULL_TEXTBOOK,
    "contemporary_india_2": FULL_TEXTBOOK,
    "contemporary_india_i": FULL_TEXTBOOK,
    "democratic_politics_ii": FULL_TEXTBOOK,
    "economics": FULL_TEXTBOOK,
    "understanding_economic_development": FULL_TEXTBOOK,
    "india_and_the_contemporary_world_ii": FULL_TEXTBOOK,
}

_CODE_FILENAME_PATTERN = re.compile(r"^[a-z]{2,4}\d{3}$", re.IGNORECASE)
_OVERRIDE_TITLE_KEYS = ("title", "chapter_title", "chapter_name")


def _relative_key(relative_path: str | Path | None) -> str:
    if relative_path is None:
        return ""
    return Path(str(relative_path)).as_posix().lstrip("./")


@lru_cache(maxsize=1)
def _load_overrides() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(_OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}

    result: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        normalized_key = _relative_key(key)
        if not normalized_key:
            continue
        if isinstance(value, str):
            result[normalized_key] = {"title": value}
        elif isinstance(value, dict):
            result[normalized_key] = value
    return result


def _override_for(relative_path: str | Path | None) -> dict[str, Any] | None:
    overrides = _load_overrides()
    key = _relative_key(relative_path)
    if not key:
        return None
    if key in overrides:
        return overrides[key]

    basename = Path(key).name
    matches = [value for path, value in overrides.items() if Path(path).name == basename]
    if len(matches) == 1:
        return matches[0]
    return None


def _override_title(override: dict[str, Any] | None) -> str:
    if not override:
        return ""
    for key in _OVERRIDE_TITLE_KEYS:
        value = override.get(key)
        if isinstance(value, str) and value.strip():
            return _WHITESPACE_PATTERN.sub(" ", value).strip()
    return ""


def _title_case_token(token: str, *, first: bool, last: bool) -> str:
    if not token:
        return token

    lowered = token.lower()
    if lowered in _POSSESSIVES:
        token = _POSSESSIVES[lowered]
        lowered = token.lower()
    if lowered in _ACRONYMS:
        return lowered.upper()
    if lowered in _ROMAN_NUMERALS and (first or last):
        return lowered.upper()
    if not (first or last) and lowered in _SMALL_WORDS:
        return lowered
    if token.isupper() and len(token) > 1:
        return token
    if "-" in token:
        return "-".join(_title_case_token(part, first=first, last=last) for part in token.split("-"))

    match = re.match(r"^([^A-Za-z0-9]*)(.*?)([^A-Za-z0-9]*)$", token)
    if not match:
        return token
    prefix, core, suffix = match.groups()
    if not core:
        return token
    return f"{prefix}{core[:1].upper()}{core[1:]}{suffix}"


def _title_case(value: str) -> str:
    tokens = value.split()
    rendered: list[str] = []
    for index, token in enumerate(tokens):
        rendered.append(_title_case_token(token, first=index == 0, last=index == len(tokens) - 1))
    return " ".join(rendered)



def canonical_chapter_title(value: str | None, *, relative_path: str | Path | None = None) -> str:
    override = _override_for(relative_path)
    override_title = _override_title(override)
    if override_title:
        return override_title

    if value is None:
        return ""

    text = str(value).strip()
    if not text:
        return ""
    text = re.sub(r"\.pdf$", "", text, flags=re.IGNORECASE)
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    text = _POSSESSIVE_PATTERN.sub("'s", text)
    text = _YEAR_PATTERN.sub("", text)
    text = _TRAILING_PART_PATTERN.sub("", text)
    text = text.replace("_", " ")
    text = _CHAPTER_PREFIX_PATTERN.sub("", text)
    text = _LEADING_NUMBER_PATTERN.sub("", text)
    text = re.sub(r"\s*[-–—]\s*$", "", text)
    text = _WHITESPACE_PATTERN.sub(" ", text).strip(" .:-")
    return _title_case(text)


def extract_chapter_number(value: str | None, *, relative_path: str | Path | None = None) -> int | None:
    override = _override_for(relative_path)
    if override:
        raw_number = override.get("chapter_number", override.get("number"))
        try:
            if raw_number is not None:
                return int(raw_number)
        except (TypeError, ValueError):
            pass

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"\.pdf$", "", text, flags=re.IGNORECASE).replace("_", " ")
    match = re.search(
        r"\b(?:chapter|unit|lesson|topic)\s*[-:#.]?\s*(\d{1,3})\b",
        text,
        re.IGNORECASE,
    )
    if match:
        return int(match.group(1))
    return None


def classify_pdf_path(relative_path: str | Path) -> str:
    override = _override_for(relative_path)
    if override:
        kind = override.get("kind", override.get("classification"))
        if isinstance(kind, str) and kind.strip():
            return kind.strip().lower()

    stem = Path(_relative_key(relative_path)).stem.lower()
    if stem in _EXACT_CLASSIFICATIONS:
        return _EXACT_CLASSIFICATIONS[stem]

    if _CODE_FILENAME_PATTERN.fullmatch(stem):
        return CHAPTER

    return CHAPTER


def chapter_title_for_pdf(relative_path: str | Path, filename: str | None = None) -> str:
    name = filename or Path(_relative_key(relative_path)).name
    return canonical_chapter_title(name, relative_path=relative_path)


def chapter_number_for_pdf(relative_path: str | Path, filename: str | None = None) -> int | None:
    name = filename or Path(_relative_key(relative_path)).name
    return extract_chapter_number(name, relative_path=relative_path)
