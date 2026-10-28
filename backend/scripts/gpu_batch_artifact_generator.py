#!/usr/bin/env python3
"""
GPU-Accelerated Educational Artifact Generator for NCERT Curriculum.
Powered by Qwen2.5-7B-Instruct (or any HuggingFace/vLLM open-weight model).

Generates:
1. Authentic Concepts with pedagogical Summaries and difficulty levels.
2. Multiple Choice Questions (MCQs) with genuine distractors and explanations.
3. Revision Flashcards.

Usage:
    python3 gpu_batch_artifact_generator.py --db educational_master.db
    python3 gpu_batch_artifact_generator.py --db educational_master.db --limit 5  # quick test
"""
from __future__ import annotations

import argparse
import gc
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("GPUArtifactGenerator")

SYSTEM_PROMPT = """You are an expert curriculum educator creating authentic, high-quality educational materials for NCERT textbooks.
Follow these pedagogical rules strictly:
1. CURRICULUM CONCEPTS & SUMMARIES:
   - Identify 3 to 5 core academic concepts that form the backbone of this chapter.
   - For each concept, write a clear, accurate 2-4 sentence textbook explanation covering its principles, formulations, and real-world relevance.
   - Assign appropriate difficulty: 'easy', 'intermediate', or 'advanced'.
   - Avoid storybook filler, characters (e.g. Pihu/Ravi), publisher metadata, or layout artifacts.
2. MULTIPLE CHOICE QUESTIONS (MCQs):
   - 4 distinct, plausible choices (A, B, C, D).
   - Only ONE unambiguously correct answer.
   - 3 realistic distractors (common student misconceptions or related concepts).
   - Step-by-step pedagogical explanation.
3. REVISION FLASHCARDS:
   - Front: Specific question or term (e.g. "What is Coulomb's Law?", "How do you find the perimeter of a rectangle?").
   - Back: Clear, accurate definition, formula, or key principle.

Output valid JSON only matching this schema:
{
  "concepts": [
    {
      "name": "Concept Name",
      "summary": "Comprehensive textbook explanation...",
      "difficulty": "intermediate"
    }
  ],
  "quizzes": [
    {
      "question": "Question text here?",
      "option_a": "Option A",
      "option_b": "Option B",
      "option_c": "Option C",
      "option_d": "Option D",
      "correct_option": 0,
      "explanation": "Detailed explanation..."
    }
  ],
  "flashcards": [
    {
      "term": "Front prompt / Term",
      "definition": "Back definition / explanation"
    }
  ]
}
"""


def load_model(model_id: str = "Qwen/Qwen2.5-7B-Instruct"):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    logger.info("CUDA available: %s", torch.cuda.is_available())
    if torch.cuda.is_available():
        logger.info(
            "Using GPU: %s (%s GB VRAM)",
            torch.cuda.get_device_name(0),
            round(torch.cuda.get_device_properties(0).total_memory / (1024**3), 1),
        )

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
    )

    logger.info("Loading tokenizer and model: %s ...", model_id)
    tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        quantization_config=bnb_config,
        device_map="auto",
        trust_remote_code=True,
    )
    logger.info("Model loaded successfully into VRAM!")
    return tokenizer, model


def generate_artifacts_for_chapter(tokenizer, model, grade: int, subject: str, title: str, optional_text: str = "") -> dict[str, Any] | None:
    import torch

    user_prompt = f"""Generate 3-5 authentic Concepts with Summaries, 4 MCQs, and 6 revision Flashcards for:
Grade: {grade}
Subject: {subject}
Chapter Title: {title}

Reference Text / Keywords (if available):
{optional_text[:2000]}

Return valid JSON adhering strictly to the schema."""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    prompt_text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer(prompt_text, return_tensors="pt").to(model.device)

    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens=1500,
            temperature=0.3,
            top_p=0.9,
            do_sample=True,
            pad_token_id=tokenizer.eos_token_id,
        )

    response_text = tokenizer.decode(outputs[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)

    match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="GPU Artifact Generator")
    parser.add_argument("--db", type=str, default="educational_master.db", help="Path to SQLite database")
    parser.add_argument("--model", type=str, default="Qwen/Qwen2.5-7B-Instruct", help="HuggingFace model ID")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of chapters to process (for testing)")
    parser.add_argument("--output", type=str, default="educational_master_generated.db", help="Output SQLite DB path")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        logger.error("Database not found at %s", db_path)
        sys.exit(1)

    out_path = Path(args.output).resolve()
    shutil.copy2(str(db_path), str(out_path))
    logger.info("Created working copy")

    tokenizer, model = load_model(args.model)

    conn = sqlite3.connect(str(out_path))
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS concepts (
        concept_id TEXT PRIMARY KEY,
        chapter_id TEXT,
        name TEXT,
        summary TEXT,
        difficulty TEXT,
        FOREIGN KEY (chapter_id) REFERENCES chapters(chapter_id)
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS quiz_questions (
        question_id TEXT PRIMARY KEY,
        chapter_id TEXT,
        question_text TEXT,
        option_a TEXT,
        option_b TEXT,
        option_c TEXT,
        option_d TEXT,
        correct_option INTEGER,
        explanation TEXT,
        FOREIGN KEY (chapter_id) REFERENCES chapters(chapter_id)
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS flashcards (
        card_id TEXT PRIMARY KEY,
        chapter_id TEXT,
        term TEXT,
        definition TEXT,
        FOREIGN KEY (chapter_id) REFERENCES chapters(chapter_id)
    );
    """)

    cursor.execute("SELECT chapter_id, grade, subject, chapter_title, description FROM chapters ORDER BY grade, subject")
    chapters = cursor.fetchall()
    if args.limit:
        chapters = chapters[:args.limit]

    logger.info("Processing %d chapters...", len(chapters))

    cursor.execute("DELETE FROM concepts;")
    cursor.execute("DELETE FROM quiz_questions;")
    cursor.execute("DELETE FROM flashcards;")
    conn.commit()

    total_concepts = 0
    total_quizzes = 0
    total_flashcards = 0
    start_time = time.time()

    for idx, (ch_id, grade, subject, title, desc) in enumerate(chapters, start=1):
        logger.info("[%d/%d] Generating for Gr%d %s - %s...", idx, len(chapters), grade, subject, title)
        data = generate_artifacts_for_chapter(tokenizer, model, grade, subject, title, desc or "")

        if not data or not isinstance(data, dict):
            logger.warning("Failed to parse generation for %s", ch_id)
            continue

        concepts = data.get("concepts", [])
        quizzes = data.get("quizzes", [])
        flashcards = data.get("flashcards", [])

        for c_idx, c in enumerate(concepts):
            cursor.execute(
                """
                INSERT INTO concepts (concept_id, chapter_id, name, summary, difficulty)
                VALUES (?, ?, ?, ?, ?)
                """,
                (f"{ch_id}_c_{c_idx}", ch_id, c.get("name", ""), c.get("summary", ""), c.get("difficulty", "intermediate")),
            )
            total_concepts += 1

        for q_idx, q in enumerate(quizzes):
            cursor.execute(
                """
                INSERT INTO quiz_questions (
                    question_id, chapter_id, question_text,
                    option_a, option_b, option_c, option_d,
                    correct_option, explanation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"{ch_id}_q_{q_idx}", ch_id, q.get("question", ""),
                    q.get("option_a", ""), q.get("option_b", ""),
                    q.get("option_c", ""), q.get("option_d", ""),
                    int(q.get("correct_option", 0)) % 4,
                    q.get("explanation", ""),
                ),
            )
            total_quizzes += 1

        for fc_idx, fc in enumerate(flashcards):
            cursor.execute(
                """
                INSERT INTO flashcards (
                    card_id, chapter_id, term, definition
                ) VALUES (?, ?, ?, ?)
                """,
                (f"{ch_id}_fc_{fc_idx}", ch_id, fc.get("term", ""), fc.get("definition", "")),
            )
            total_flashcards += 1

        if idx % 10 == 0:
            conn.commit()

    conn.commit()
    conn.close()

    elapsed = time.time() - start_time
    logger.info("============================================================")
    logger.info("COMPLETED IN %.2f SECONDS", elapsed)
    logger.info("Total Concepts: %d | Total Quizzes: %d | Total Flashcards: %d", total_concepts, total_quizzes, total_flashcards)
    logger.info("Output database written")
    logger.info("============================================================")


if __name__ == "__main__":
    main()
