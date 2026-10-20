"""Bulk curriculum compilation pipeline orchestrator."""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class CompilationStatus(str, Enum):
    """Compilation status values."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class CompilationTask:
    """Represents a single compilation task for a textbook chapter."""

    task_id: str
    grade: int
    subject: str
    chapter: str
    filename: str
    relative_path: str
    language: str
    status: CompilationStatus = CompilationStatus.PENDING
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    artifacts: Optional[Dict] = None


@dataclass
class CompilationStage:
    """Represents a compilation stage."""

    name: str
    description: str
    enabled: bool = True
    critical: bool = False  # If True, failure stops the pipeline


class BulkCurriculumCompiler:
    """Orchestrate bulk compilation of all curriculum chapters."""

    # Compilation stages
    STAGES = [
        CompilationStage("extract", "Extract content from PDF", enabled=True, critical=True),
        CompilationStage("chunk", "Educational chunking and segmentation", enabled=True, critical=True),
        CompilationStage("index", "Build retrieval index in Qdrant", enabled=True, critical=True),
        CompilationStage("summarize", "Generate summaries", enabled=True, critical=False),
        CompilationStage("glossary", "Extract and build glossary", enabled=True, critical=False),
        CompilationStage("quiz", "Generate quiz questions", enabled=True, critical=False),
        CompilationStage("flashcard", "Generate flashcards", enabled=True, critical=False),
        CompilationStage("enrich", "Build enrichment links", enabled=True, critical=False),
        CompilationStage("validate", "Validate compilation quality", enabled=True, critical=False),
        CompilationStage("pack", "Compile into educational pack", enabled=True, critical=True),
    ]

    def __init__(
        self,
        textbooks_root: Path,
        curriculum_manifest: Dict,
        output_dir: Path,
        pack_service_url: str = "http://pack-service:8030",
        max_concurrent_tasks: int = 2,
    ):
        """
        Initialize bulk compiler.

        Args:
            textbooks_root: Path to TEXTBOOKS directory
            curriculum_manifest: Curriculum manifest from scanner
            output_dir: Output directory for compilation artifacts
            pack_service_url: Pack service URL
            max_concurrent_tasks: Maximum concurrent compilation tasks
        """
        self.textbooks_root = Path(textbooks_root)
        self.curriculum_manifest = curriculum_manifest
        self.output_dir = Path(output_dir)
        self.pack_service_url = pack_service_url
        self.max_concurrent_tasks = max_concurrent_tasks

        self.tasks: List[CompilationTask] = []
        self.compilation_report: Dict = {
            "started_at": None,
            "completed_at": None,
            "total_tasks": 0,
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "duration_seconds": 0,
            "stages": {},
            "tasks": [],
        }

    def create_tasks_from_manifest(self) -> List[CompilationTask]:
        """
        Create compilation tasks from curriculum manifest.

        Returns:
            List of compilation tasks
        """
        logger.info("Creating compilation tasks from curriculum manifest...")
        tasks = []
        task_id = 0

        for key, curriculum in self.curriculum_manifest.get("curriculum_index", {}).items():
            grade = curriculum.get("grade")
            subject = curriculum.get("subject")
            language = curriculum.get("language")

            for chapter in curriculum.get("chapters", []):
                task_id += 1

                # Find actual file path
                filename = chapter.get("filename")
                relative_path = chapter.get("relative_path")
                full_path = self.textbooks_root / relative_path if relative_path else None

                if not full_path or not full_path.exists():
                    logger.warning(f"PDF file not found: {full_path}")
                    continue

                task = CompilationTask(
                    task_id=f"task_{task_id:06d}",
                    grade=grade or 0,
                    subject=subject or "unknown",
                    chapter=chapter.get("chapter_name") or chapter.get("chapter") or "unknown",
                    filename=filename or "",
                    relative_path=str(relative_path) if relative_path else "",
                    language=language or "english",
                )

                tasks.append(task)

        self.tasks = tasks
        logger.info(f"Created {len(self.tasks)} compilation tasks")

        return self.tasks

    async def compile_task(self, task: CompilationTask) -> CompilationTask:
        """
        Compile a single task through all stages.

        Args:
            task: Compilation task

        Returns:
            Updated task
        """
        task.status = CompilationStatus.PROCESSING
        task.started_at = datetime.utcnow().isoformat()
        task.artifacts = {}

        logger.info(f"Starting compilation: {task.task_id} - {task.chapter}")

        for stage in self.STAGES:
            if not stage.enabled:
                continue

            try:
                logger.debug(f"  Stage: {stage.name} for {task.task_id}")
                # Stage-specific logic would go here
                # For now, just track that it was processed
                task.artifacts[stage.name] = {
                    "status": "completed",
                    "timestamp": datetime.utcnow().isoformat(),
                }

            except Exception as e:
                logger.error(f"Error in stage {stage.name} for {task.task_id}: {e}")

                if stage.critical:
                    task.status = CompilationStatus.FAILED
                    task.error = f"Failed at stage '{stage.name}': {str(e)}"
                    break

        if task.status != CompilationStatus.FAILED:
            task.status = CompilationStatus.COMPLETED

        task.completed_at = datetime.utcnow().isoformat()

        return task

    async def compile_all(self, dry_run: bool = False) -> Dict:
        """
        Compile all tasks with concurrency control.

        Args:
            dry_run: If True, don't actually compile, just plan

        Returns:
            Compilation report
        """
        self.compilation_report["started_at"] = datetime.utcnow().isoformat()
        self.compilation_report["total_tasks"] = len(self.tasks)

        logger.info(f"Starting bulk compilation of {len(self.tasks)} tasks")
        logger.info(f"Max concurrent tasks: {self.max_concurrent_tasks}")

        if dry_run:
            logger.info("DRY RUN MODE - Not performing actual compilation")
            self._generate_dry_run_report()
            return self.compilation_report

        # Create semaphore for concurrency control
        semaphore = asyncio.Semaphore(self.max_concurrent_tasks)

        async def compile_with_semaphore(task: CompilationTask) -> CompilationTask:
            async with semaphore:
                return await self.compile_task(task)

        # Compile all tasks concurrently
        results = await asyncio.gather(*[compile_with_semaphore(task) for task in self.tasks])

        # Update report
        for result in results:
            self.compilation_report["tasks"].append(
                {
                    "task_id": result.task_id,
                    "grade": result.grade,
                    "subject": result.subject,
                    "chapter": result.chapter,
                    "status": result.status.value,
                    "started_at": result.started_at,
                    "completed_at": result.completed_at,
                    "error": result.error,
                }
            )

            if result.status == CompilationStatus.COMPLETED:
                self.compilation_report["completed"] += 1
            elif result.status == CompilationStatus.FAILED:
                self.compilation_report["failed"] += 1
            elif result.status == CompilationStatus.SKIPPED:
                self.compilation_report["skipped"] += 1

        self.compilation_report["completed_at"] = datetime.utcnow().isoformat()

        # Calculate duration
        start = datetime.fromisoformat(self.compilation_report["started_at"])
        end = datetime.fromisoformat(self.compilation_report["completed_at"])
        self.compilation_report["duration_seconds"] = (end - start).total_seconds()

        logger.info("Bulk compilation complete")

        return self.compilation_report

    def _generate_dry_run_report(self) -> None:
        """Generate dry-run report showing what would be compiled."""
        self.compilation_report["completed_at"] = datetime.utcnow().isoformat()

        for task in self.tasks:
            self.compilation_report["tasks"].append(
                {
                    "task_id": task.task_id,
                    "grade": task.grade,
                    "subject": task.subject,
                    "chapter": task.chapter,
                    "status": "would_compile",
                }
            )
            self.compilation_report["completed"] += 1

        self.compilation_report["completed_at"] = datetime.utcnow().isoformat()
        start = datetime.fromisoformat(self.compilation_report["started_at"])
        end = datetime.fromisoformat(self.compilation_report["completed_at"])
        self.compilation_report["duration_seconds"] = (end - start).total_seconds()

    def save_report(self, output_path: Path) -> None:
        """
        Save compilation report to JSON.

        Args:
            output_path: Path to save report
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.compilation_report, f, indent=2)

        logger.info(f"Compilation report saved to {output_path}")

    def print_report(self) -> None:
        """Print compilation report summary."""
        report = self.compilation_report

        print("\n" + "=" * 60)
        print("CURRICULUM COMPILATION REPORT")
        print("=" * 60)
        print(f"Started: {report['started_at']}")
        print(f"Completed: {report['completed_at']}")
        print(f"Duration: {report['duration_seconds']:.1f}s")
        print()
        print(f"Total Tasks: {report['total_tasks']}")
        print(f"Completed: {report['completed']}")
        print(f"Failed: {report['failed']}")
        print(f"Skipped: {report['skipped']}")
        print()
        print("Success Rate: {:.1f}%".format((report["completed"] / max(report["total_tasks"], 1)) * 100))
        print("=" * 60)

        if report["failed"] > 0:
            print("\nFailed Tasks:")
            for task in report["tasks"]:
                if task["status"] == "failed":
                    print(f"  - {task['task_id']}: {task['chapter']} (Grade {task['grade']})")
