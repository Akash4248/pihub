#!/usr/bin/env python3
"""
Comprehensive curriculum precompilation build orchestrator.

This script orchestrates the entire curriculum precompilation pipeline:
  1. Scan curriculum structure
  2. Build master curriculum manifest
  3. Create bulk compilation plan
  4. Execute bulk compilation (placeholder for now)
  5. Register all packs
  6. Create enrichment registry
  7. Generate build reports

Usage:
    python build_complete_curriculum.py                    # Full build
    python build_complete_curriculum.py --dry-run          # Plan only
    python build_complete_curriculum.py --grade 7          # Build grade 7
    python build_complete_curriculum.py --parallel 4       # 4 concurrent tasks
    python build_complete_curriculum.py --output /path     # Custom output
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Add current directory and backend root to path for imports
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(BACKEND_ROOT))

from shared.textbook_paths import find_textbooks_root
from bulk_curriculum_compiler import BulkCurriculumCompiler
from curriculum_manifest_builder import CurriculumManifestBuilder
from curriculum_scanner import CurriculumScanner
from enrichment_registry import create_default_enrichment_registry
from master_pack_registry import MasterPackRegistry

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Build precompiled curriculum distribution platform",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Complete curriculum build
  python build_complete_curriculum.py

  # Dry run (plan only)
  python build_complete_curriculum.py --dry-run

  # Build specific grade
  python build_complete_curriculum.py --grade 7

  # Build with 4 concurrent tasks
  python build_complete_curriculum.py --parallel 4

  # Custom output location
  python build_complete_curriculum.py --output /shared/curriculum

  # Verbose output
  python build_complete_curriculum.py --verbose
        """,
    )

    parser.add_argument(
        "--textbooks-root",
        type=Path,
        help="Path to TEXTBOOKS directory",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/shared/curriculum"),
        help="Output directory for curriculum artifacts",
    )

    parser.add_argument(
        "--grade",
        type=int,
        help="Build specific grade only",
    )

    parser.add_argument(
        "--subject",
        help="Build specific subject only",
    )

    parser.add_argument(
        "--parallel",
        type=int,
        default=2,
        help="Max concurrent compilation tasks (default: 2)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only, don't compile",
    )

    parser.add_argument(
        "--skip-scan",
        action="store_true",
        help="Use existing curriculum scan",
    )

    parser.add_argument(
        "--skip-enrichment",
        action="store_true",
        help="Skip enrichment registry creation",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    parser.add_argument(
        "--no-summary",
        action="store_true",
        help="Skip summary output",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        # Determine TEXTBOOKS root
        if args.textbooks_root:
            textbooks_root = Path(args.textbooks_root).resolve()
        else:
            textbooks_root = find_textbooks_root(Path(__file__))
            if not textbooks_root:
                raise ValueError(
                    "Could not locate TEXTBOOKS. Pass --textbooks-root /path/to/TEXTBOOKS."
                )

        output_path = Path(args.output)

        logger.info("=" * 70)
        logger.info("CURRICULUM PRECOMPILATION BUILD ORCHESTRATOR")
        logger.info("=" * 70)
        logger.info(f"TEXTBOOKS Root: {textbooks_root}")
        logger.info(f"Output Directory: {output_path}")
        logger.info(f"Max Concurrent Tasks: {args.parallel}")
        logger.info(f"Dry Run: {args.dry_run}")
        logger.info("=" * 70)

        # Step 1: Scan Curriculum
        logger.info("\n[1/6] SCANNING CURRICULUM STRUCTURE...")
        scan_file = output_path / "curriculum_scan.json"

        if args.skip_scan and scan_file.exists():
            logger.info(f"Using existing scan: {scan_file}")
            with open(scan_file, "r") as f:
                scan_data = json.load(f)
        else:
            scanner = CurriculumScanner(textbooks_root)
            scan_data = scanner.scan()

            if args.grade or args.subject:
                scan_data = _filter_curriculum(scan_data, args.grade, args.subject)

            scanner.save_scan_result(scan_file)

            if not args.no_summary:
                scanner.print_summary()

        # Step 2: Build Master Curriculum Manifest
        logger.info("\n[2/6] BUILDING CURRICULUM MANIFEST...")
        manifest_file = output_path / "curriculum_manifest.json"

        manifest = CurriculumManifestBuilder.create_manifest_from_scan(
            scan_data,
            version=args.output.name if hasattr(args.output, "name") else "1.0.0",
        )

        if not CurriculumManifestBuilder.validate_manifest(manifest):
            logger.error("Manifest validation failed")
            return 1

        CurriculumManifestBuilder.save_manifest(manifest, manifest_file)

        if not args.no_summary:
            CurriculumManifestBuilder.print_manifest_summary(manifest)

        # Step 3: Create Compilation Plan
        logger.info("\n[3/6] CREATING COMPILATION PLAN...")
        compiler = BulkCurriculumCompiler(
            textbooks_root=textbooks_root,
            curriculum_manifest=manifest,
            output_dir=output_path,
            max_concurrent_tasks=args.parallel,
        )

        tasks = compiler.create_tasks_from_manifest()
        logger.info(f"Created {len(tasks)} compilation tasks")

        # Step 4: Execute Bulk Compilation (or dry run)
        logger.info("\n[4/6] EXECUTING BULK COMPILATION...")

        if args.dry_run:
            logger.info("DRY RUN - Planning compilation only")
            report = asyncio.run(compiler.compile_all(dry_run=True))
        else:
            logger.info(f"Compiling {len(tasks)} tasks with up to {args.parallel} concurrent tasks")
            report = asyncio.run(compiler.compile_all(dry_run=False))

        compiler.save_report(output_path / "compilation_report.json")

        if not args.no_summary:
            compiler.print_report()

        # Step 5: Build Master Pack Registry
        logger.info("\n[5/6] BUILDING MASTER PACK REGISTRY...")
        pack_registry = MasterPackRegistry()

        # Register packs from manifest
        for key, curriculum in manifest["curriculum_index"].items():
            grade = curriculum.get("grade", 0)
            subject = curriculum.get("subject", "unknown")
            language = curriculum.get("language", "english")

            for idx, chapter in enumerate(curriculum.get("chapters", [])):
                pack_id = chapter.get("chapter_id", f"{key}_ch{idx:03d}")

                pack_registry.register_pack(
                    pack_id=pack_id,
                    grade=grade,
                    subject=subject,
                    chapter=chapter.get("chapter_name", ""),
                    language=language,
                    version="1.0.0",
                    checksum="",  # Would be computed during compilation
                    size_bytes=0,  # Would be computed during compilation
                )

        registry_file = output_path / "pack_registry.json"
        pack_registry.save(registry_file)

        if not args.no_summary:
            pack_registry.print_summary()

        # Step 6: Create Enrichment Registry
        if not args.skip_enrichment:
            logger.info("\n[6/6] BUILDING ENRICHMENT REGISTRY...")

            enrichment_registry = create_default_enrichment_registry()
            enrichment_file = output_path / "enrichment_registry.json"
            enrichment_registry.save(enrichment_file)

            logger.info(f"Enrichment registry saved: {enrichment_file}")

        else:
            logger.info("\n[6/6] SKIPPING ENRICHMENT REGISTRY")

        # Final Summary
        logger.info("\n" + "=" * 70)
        logger.info("CURRICULUM BUILD COMPLETE")
        logger.info("=" * 70)
        logger.info(f"Scan Results: {scan_file}")
        logger.info(f"Curriculum Manifest: {manifest_file}")
        logger.info(f"Compilation Report: {output_path / 'compilation_report.json'}")
        logger.info(f"Pack Registry: {registry_file}")

        if not args.skip_enrichment:
            logger.info(f"Enrichment Registry: {enrichment_file}")

        logger.info("=" * 70)

        logger.info("✓ All artifacts ready for production distribution!")

        return 0

    except Exception as e:
        logger.error(f"Error building curriculum: {e}", exc_info=args.verbose)
        return 1


def _filter_curriculum(scan_data: dict, grade: int = None, subject: str = None) -> dict:
    """
    Filter curriculum data by grade and subject.

    Args:
        scan_data: Scan data from scanner
        grade: Grade to filter by
        subject: Subject to filter by

    Returns:
        Filtered scan data
    """
    filtered = {
        "metadata": scan_data["metadata"].copy(),
        "curriculum": {},
    }

    # Filter curriculum
    for key, entry in scan_data["curriculum"].items():
        if grade is not None and entry["grade"] != grade:
            continue

        if subject is not None and entry["subject"] != subject:
            continue

        filtered["curriculum"][key] = entry

    # Update metadata
    if grade is not None:
        filtered["metadata"]["grades"] = [grade]
    if subject is not None:
        filtered["metadata"]["subjects"] = [subject]

    filtered["metadata"]["total_pdfs"] = sum(len(entry["chapters"]) for entry in filtered["curriculum"].values())

    return filtered


if __name__ == "__main__":
    sys.exit(main())
