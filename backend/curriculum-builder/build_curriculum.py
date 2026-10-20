#!/usr/bin/env python3
"""
Master curriculum builder and scanner.

Usage:
    python build_curriculum.py                          # Build all
    python build_curriculum.py --grade 7                # Build grade 7
    python build_curriculum.py --subject mathematics    # Build mathematics
    python build_curriculum.py --output /path/to/output # Custom output
"""

import argparse
import json
import logging
import sys
from pathlib import Path

# Add current directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from curriculum_manifest_builder import CurriculumManifestBuilder
from curriculum_scanner import CurriculumScanner
from shared.textbook_paths import find_textbooks_root as discover_textbooks_root

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def find_textbooks_root(start_path: Path = None) -> Path:
    """
    Find TEXTBOOKS directory by traversing up from start path.

    Args:
        start_path: Path to start searching from (defaults to current dir)

    Returns:
        Path to TEXTBOOKS directory

    Raises:
        ValueError: If TEXTBOOKS directory not found
    """
    textbooks = discover_textbooks_root(start_path)
    if textbooks:
        return textbooks
    raise ValueError(
        "Could not find TEXTBOOKS directory. Set TEXTBOOKS_ROOT or specify --textbooks-root."
    )


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Build master curriculum from TEXTBOOKS directory",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Build complete curriculum
  python build_curriculum.py

  # Build specific grade
  python build_curriculum.py --grade 7

  # Build specific subject
  python build_curriculum.py --subject mathematics

  # Custom output location
  python build_curriculum.py --output /shared/curriculum

  # Verbose output
  python build_curriculum.py --verbose
        """,
    )

    parser.add_argument(
        "--textbooks-root",
        type=Path,
        help="Path to TEXTBOOKS directory (auto-detected if not specified)",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/shared/curriculum"),
        help="Output directory for curriculum artifacts (default: /shared/curriculum)",
    )

    parser.add_argument(
        "--grade",
        type=int,
        help="Build specific grade only (e.g., 7)",
    )

    parser.add_argument(
        "--subject",
        help="Build specific subject only (e.g., mathematics, science)",
    )

    parser.add_argument(
        "--language",
        default="english",
        help="Filter by language (default: english)",
    )

    parser.add_argument(
        "--version",
        default="1.0.0",
        help="Curriculum version (default: 1.0.0)",
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
        # Find TEXTBOOKS root
        if args.textbooks_root:
            textbooks_root = Path(args.textbooks_root).resolve()
        else:
            textbooks_root = find_textbooks_root()

        logger.info(f"Using TEXTBOOKS root: {textbooks_root}")
        logger.info(f"Output directory: {args.output}")

        # Create scanner
        scanner = CurriculumScanner(textbooks_root)

        # Perform scan
        logger.info("Scanning curriculum...")
        scan_data = scanner.scan()

        # Filter by criteria if specified
        if args.grade or args.subject:
            logger.info(f"Filtering: grade={args.grade}, subject={args.subject}")
            scan_data = _filter_curriculum(scan_data, args.grade, args.subject, args.language)

        # Build manifest
        logger.info("Building curriculum manifest...")
        manifest = CurriculumManifestBuilder.create_manifest_from_scan(scan_data, version=args.version)

        # Validate manifest
        if not CurriculumManifestBuilder.validate_manifest(manifest):
            logger.error("Manifest validation failed")
            return 1

        # Create output directory
        output_path = Path(args.output)
        output_path.mkdir(parents=True, exist_ok=True)

        # Save artifacts
        scan_output = output_path / "curriculum_scan.json"
        manifest_output = output_path / "curriculum_manifest.json"

        scanner.save_scan_result(scan_output)
        CurriculumManifestBuilder.save_manifest(manifest, manifest_output)

        # Print summary
        if not args.no_summary:
            scanner.print_summary()
            CurriculumManifestBuilder.print_manifest_summary(manifest)

        logger.info("✓ Curriculum build complete!")
        logger.info(f"Scan results: {scan_output}")
        logger.info(f"Manifest: {manifest_output}")

        return 0

    except Exception as e:
        logger.error(f"Error building curriculum: {e}", exc_info=args.verbose)
        return 1


def _filter_curriculum(scan_data: dict, grade: int = None, subject: str = None, language: str = None) -> dict:
    """
    Filter curriculum data by grade and subject.

    Args:
        scan_data: Scan data from scanner
        grade: Grade to filter by
        subject: Subject to filter by
        language: Language to filter by

    Returns:
        Filtered scan data
    """
    filtered = {
        "metadata": scan_data["metadata"].copy(),
        "curriculum": {},
    }

    # Update metadata to reflect filters
    if grade is not None:
        filtered["metadata"]["grades"] = [grade]
    if subject is not None:
        filtered["metadata"]["subjects"] = [subject]
    if language is not None:
        filtered["metadata"]["languages"] = [language]

    # Filter curriculum
    for key, entry in scan_data["curriculum"].items():
        if grade is not None and entry["grade"] != grade:
            continue

        if subject is not None and entry["subject"] != subject:
            continue

        if language is not None and entry["language"] != language:
            continue

        filtered["curriculum"][key] = entry

    # Recount chapters
    filtered["metadata"]["total_pdfs"] = sum(len(entry["chapters"]) for entry in filtered["curriculum"].values())

    return filtered


if __name__ == "__main__":
    sys.exit(main())
