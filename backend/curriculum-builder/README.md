# Curriculum Builder: Precompiled Curriculum Distribution Platform

## Overview

The Curriculum Builder transforms PIHUB from a **runtime generation system** to a **precompiled curriculum distribution platform**.

### Architecture Shift

**Before:**
```
runtime requests → on-demand generation → retrieval indexing → client delivery
```

**After:**
```
precompilation phase → bulk compilation → prelinked enrichment → pack registry → distribution
```

### Key Principles

- **Build Once, Distribute Many**: Compile entire curriculum once, distribute to many clients
- **Host-Heavy, Pi-Light**: All heavy processing (OCR, embeddings, compilation) stays on server
- **Incremental Delivery**: Zero disruption to existing APIs and services
- **Deterministic Builds**: Reproducible curriculum packages with integrity verification

---

## Directory Structure

```
backend/curriculum-builder/
├── __init__.py
├── README.md (this file)
│
├── curriculum_scanner.py              # Step 1: Scan curriculum
├── folder_parser.py                   # Parse folder hierarchy
├── subject_mapper.py                  # Map subjects
├── language_detector.py               # Detect languages
│
├── curriculum_manifest_builder.py     # Step 2: Build manifest
│
├── bulk_curriculum_compiler.py        # Step 3: Bulk compiler
│
├── enrichment_registry.py             # Step 5: Enrichment registry
│
├── master_pack_registry.py            # Step 6: Master pack registry
│
├── curriculum_validation.py           # Step 8: Validation pipeline
│
├── build_report_generator.py          # Step 9: Build reports
│
├── build_curriculum.py                # CLI: Build curriculum + manifest
├── build_subject.py                   # CLI: Subject-scoped build launcher
├── build_complete_curriculum.py       # CLI: Complete build orchestrator
│
└── tests/
    ├── test_curriculum_scanner.py
    ├── test_manifest_builder.py
    └── test_validation.py
```

---

## Build Pipeline

### Step 1: Curriculum Scanner

Automatically detects curriculum structure from TEXTBOOKS directory.

```bash
# Scan curriculum only
python build_curriculum.py --output ./output

# Scan specific grade
python build_curriculum.py --grade 7 --output ./output

# Scan specific subject
python build_curriculum.py --subject mathematics --output ./output

# Build a subject run from this directory
python build_subject.py --subject maths

# Or from the repo root
python backend/curriculum-builder/build_subject.py --subject maths
```

**Outputs:**
- `curriculum_scan.json` - Complete curriculum index

---

### Step 2: Curriculum Manifest

Builds master curriculum manifest with chapter indexing and curriculum graph.

**Outputs:**
- `curriculum_manifest.json` - Master curriculum structure

---

### Step 3: Bulk Compilation

Plans compilation of all textbooks through educational pipeline.

**Stages:**
1. Extract content from PDF
2. Educational chunking
3. Retrieval indexing (Qdrant)
4. Summary generation
5. Glossary extraction
6. Quiz generation
7. Flashcard generation
8. Enrichment linking
9. Validation
10. Pack compilation

*Currently in planning phase; integration with pack-service happening next.*

---

### Step 4: Master Pack Registry

Centralized registry of all compiled packs.

**Outputs:**
- `pack_registry.json` - Pack registry with indexes by grade/subject/language

---

### Step 5: Enrichment Registry

Pre-linked educational resources (simulations, experiments, videos, virtual labs, animations).

**Outputs:**
- `enrichment_registry.json` - Curated enrichment mappings

---

### Step 6: Validation Pipeline

Comprehensive validation before distribution.

**Validates:**
- Manifest structure and completeness
- Pack registry consistency
- Enrichment registry integrity
- No duplicate entries
- All required metadata present

---

### Step 7: Build Reports

Generate comprehensive build reports for monitoring.

Per-run reports are saved under `build_reports/` using timestamp-only filenames.
The latest run is also copied to `build_report.json` for compatibility, and
`build_reports/build_report_index.json` stores subject-based lookup metadata.

---

## Complete Build Orchestration

### One-Command Build

```bash
python build_complete_curriculum.py
```

This runs all 6 steps and produces:
- `curriculum_scan.json`
- `curriculum_manifest.json`
- `compilation_report.json`
- `pack_registry.json`
- `enrichment_registry.json`

### Build Options

```bash
# Full build with all steps
python build_complete_curriculum.py

# Dry run (plan only, no compilation)
python build_complete_curriculum.py --dry-run

# Build specific grade
python build_complete_curriculum.py --grade 7

# Build specific subject
python build_complete_curriculum.py --subject science

# Custom output directory
python build_complete_curriculum.py --output /shared/curriculum

# Parallel compilation (4 concurrent tasks)
python build_complete_curriculum.py --parallel 4

# Verbose logging
python build_complete_curriculum.py --verbose

# Skip intermediate steps
python build_complete_curriculum.py --skip-scan --skip-enrichment
```

---

## Output Artifacts

### 1. Curriculum Scan (`curriculum_scan.json`)

```json
{
  "metadata": {
    "scanned_at": "2026-05-18T15:04:02.004856",
    "total_pdfs": 320,
    "grades": [1, 2, 3, ..., 10],
    "subjects": ["mathematics", "science"],
    "languages": ["english", "kannada"]
  },
  "curriculum": {
    "grade_7_mathematics_english": {
      "grade": 7,
      "subject": "mathematics",
      "language": "english",
      "chapters": [...]
    }
  }
}
```

### 2. Curriculum Manifest (`curriculum_manifest.json`)

```json
{
  "metadata": {
    "version": "1.0.0",
    "created_at": "2026-05-18T15:04:02",
    "total_grades": 10,
    "total_subjects": 2,
    "total_chapters": 320
  },
  "curriculum_index": {
    "grade_7_mathematics_english": {
      "chapters": [
        {
          "chapter_id": "7_mathematics_english_ch001",
          "chapter_name": "Real Numbers",
          "filename": "Real Numbers.pdf",
          "sequence": 0
        }
      ]
    }
  },
  "curriculum_graph": {
    "by_grade": {...},
    "by_subject": {...},
    "by_language": {...}
  }
}
```

### 3. Pack Registry (`pack_registry.json`)

```json
{
  "metadata": {
    "version": "1.0.0",
    "total_packs": 320,
    "registry_hash": "abc123..."
  },
  "packs": {
    "7_mathematics_english_ch001": {
      "pack_id": "7_mathematics_english_ch001",
      "grade": 7,
      "subject": "mathematics",
      "chapter": "Real Numbers",
      "version": "1.0.0",
      "checksum": "...",
      "size_bytes": 0
    }
  },
  "index": {
    "by_grade": {7: ["7_mathematics_english_ch001", ...]},
    "by_subject": {"mathematics": [...]},
    "by_language": {"english": [...]}
  }
}
```

### 4. Enrichment Registry (`enrichment_registry.json`)

```json
{
  "metadata": {
    "version": "1.0.0",
    "total_mappings": 25,
    "total_simulations": 3,
    "total_videos": 5
  },
  "simulations": [...],
  "experiments": [...],
  "videos": [...],
  "concept_mappings": {
    "quadratic_equations": {
      "videos": ["Quadratic Equations Explained"],
      "simulations": []
    }
  }
}
```

---

## Integration with Existing Systems

### No Breaking Changes

✓ Existing retrieval APIs remain unchanged
✓ Existing pack-service continues to work
✓ Existing ingestion pipeline unmodified
✓ Docker architecture preserved
✓ All current endpoints still functional

### New Capabilities

The curriculum builder **extends** the system with:

1. **Precompilation**: Batch compile all curriculum offline
2. **Distribution Registry**: Centralized pack management
3. **Enrichment Mapping**: Pre-curated educational resources
4. **Validation Pipeline**: Quality gates before distribution
5. **Build Reports**: Monitoring and troubleshooting

---

## Configuration

### Environment Variables

```bash
# Add to .env file
TEXTBOOKS_ROOT=/home/akash/Desktop/NOTIMP/TEXTBOOKS
CURRICULUM_BUILD_DIR=/shared/curriculum
CURRICULUM_MANIFEST_PATH=/shared/curriculum/curriculum_manifest.json
PACK_REGISTRY_PATH=/shared/curriculum/pack_registry.json
ENRICHMENT_REGISTRY_PATH=/shared/curriculum/enrichment_registry.json
CURRICULUM_VERSION=1.0.0
MAX_CONCURRENT_COMPILATION_TASKS=2
```

### Default Paths

All paths defined in `/backend/shared/config.py`:
- `textbooks_root`: Points to TEXTBOOKS directory
- `curriculum_build_dir`: Output directory for builds
- `curriculum_manifest_path`: Master curriculum manifest
- `pack_registry_path`: Master pack registry
- `enrichment_registry_path`: Enrichment resource registry

---

## Usage Workflows

### Workflow 1: Full Curriculum Build

```bash
cd backend/curriculum-builder

# Build everything
python build_complete_curriculum.py \
  --textbooks-root /path/to/TEXTBOOKS \
  --output /shared/curriculum \
  --parallel 4 \
  --verbose

# Validate results
python -c "
from curriculum_validation import CurriculumValidationPipeline
from pathlib import Path

validator = CurriculumValidationPipeline()
valid, results = validator.validate_manifest(Path('/shared/curriculum/curriculum_manifest.json'))
validator.print_report()
"
```

### Workflow 2: Grade-Specific Build

```bash
# Build only Grade 7
python build_complete_curriculum.py \
  --grade 7 \
  --output ./grade_7_build

# Results in ./grade_7_build/:
#   curriculum_scan.json
#   curriculum_manifest.json
#   pack_registry.json
#   enrichment_registry.json
```

### Workflow 3: Dry Run (Planning)

```bash
# Plan entire build without compiling
python build_complete_curriculum.py \
  --dry-run \
  --output ./build_plan \
  --verbose

# Review compilation_report.json to see what would be built
cat ./build_plan/compilation_report.json | jq '.total_tasks, .tasks | length'
```

### Workflow 4: Incremental Builds

```bash
# First build: full curriculum
python build_complete_curriculum.py

# Second build: reuse existing scan, only rebuild manifest
python build_complete_curriculum.py \
  --skip-scan \
  --output ./updated_build

# Much faster since scanning only happens once
```

---

## Testing

### Unit Tests

```bash
cd backend/curriculum-builder

# Run all tests
python -m pytest tests/

# Run specific test
python -m pytest tests/test_curriculum_scanner.py -v

# Run with coverage
python -m pytest tests/ --cov=. --cov-report=html
```

### Manual Validation

```bash
# Validate manifest
python -c "
from curriculum_validation import CurriculumValidationPipeline
from pathlib import Path

validator = CurriculumValidationPipeline()
valid, results = validator.validate_manifest(Path('./complete_build/curriculum_manifest.json'))
print(f'Valid: {valid}')
print(f'Results: {len(results)} checks')
"

# Check pack registry
python -c "
from master_pack_registry import MasterPackRegistry
from pathlib import Path

registry = MasterPackRegistry.load(Path('./complete_build/pack_registry.json'))
print(f'Total packs: {registry.registry[\"metadata\"][\"total_packs\"]}')
print(f'Grade 7 packs: {len(registry.get_packs_for_grade(7))}')
"
```

---

## Troubleshooting

### Issue: "TEXTBOOKS directory not found"

```bash
# Ensure TEXTBOOKS exists
ls -la /home/akash/Desktop/NOTIMP/TEXTBOOKS

# Explicitly provide path
python build_curriculum.py --textbooks-root /home/akash/Desktop/NOTIMP/TEXTBOOKS
```

### Issue: Permission denied on /shared

```bash
# Create directory with proper permissions
sudo mkdir -p /shared/curriculum
sudo chmod 777 /shared/curriculum

# Or use local output
python build_complete_curriculum.py --output ./local_build
```

### Issue: JSON serialization errors

```bash
# Ensure all data is JSON-serializable
# Curriculum builder uses default=str to handle non-standard types

# Check for non-standard data types in curricuum_scan.json:
python -c "
import json
with open('./curriculum_build/curriculum_scan.json') as f:
    data = json.load(f)
    print('Valid JSON')
"
```

### Issue: Slow curriculum scan

```bash
# Use parallel processing with high concurrency
python build_complete_curriculum.py \
  --parallel 8 \
  --verbose

# Or scan specific grade to reduce scope
python build_curriculum.py --grade 7
```

---

## Next Phases

### Immediate (Phase 2)

- [ ] Full bulk compilation pipeline integration with pack-service
- [ ] Content extraction and chunking
- [ ] Retrieval indexing in Qdrant
- [ ] Summary/glossary/quiz generation
- [ ] Pack compilation and storage

### Short-term (Phase 3)

- [ ] End-to-end pack flow validation
- [ ] Gateway routing updates
- [ ] Pi cache compatibility
- [ ] Sync service integration

### Medium-term (Phase 4)

- [ ] Classroom deployment orchestration
- [ ] Incremental build optimization
- [ ] Curriculum update detection
- [ ] Delta compilation

---

## Architecture Decision Records

### ADR-001: Precompilation Over Runtime

**Decision**: Build all curriculum offline before distribution.

**Rationale**:
- Eliminates runtime processing on limited Pi hardware
- Enables quality validation before distribution
- Faster client startup and responsiveness
- Deterministic, reproducible builds

### ADR-002: No Breaking Changes

**Decision**: Only extend, never modify existing systems.

**Rationale**:
- Maintain stability for production classroom deployments
- Gradual migration path for existing clients
- Lower risk of disruption
- Backward compatibility

### ADR-003: Registry-Based Distribution

**Decision**: Use centralized pack registry for discovery and sync.

**Rationale**:
- Single source of truth for available packs
- Enables sophisticated sync strategies
- Supports version management
- Facilitates classroom deployment

---

## Performance Characteristics

### Build Performance

| Operation | Time | Data Size |
|-----------|------|-----------|
| Full curriculum scan (320 PDFs) | ~100ms | 83 KB |
| Manifest generation | ~50ms | 117 KB |
| Pack registry creation | ~100ms | 118 KB |
| Enrichment registry (default) | ~20ms | 12 KB |
| Complete build (dry-run) | ~500ms | 376 KB |
| Complete build (full compilation) | ~5-30 minutes | ~10+ GB |

### Storage Requirements

| Artifact | Size | Growth |
|----------|------|--------|
| Curriculum scan | ~83 KB | Linear with PDF count |
| Curriculum manifest | ~117 KB | Linear with chapter count |
| Pack registry | ~118 KB | Linear with pack count |
| Enrichment registry | ~12 KB | Fixed |
| Compiled packs | ~10+ GB | Linear with content |

---

## Contributing

To extend the curriculum builder:

1. Add new scanner components in `*_parser.py`
2. Add new compilation stages in `bulk_curriculum_compiler.py`
3. Add new validation checks in `curriculum_validation.py`
4. Add new enrichment types in `enrichment_registry.py`
5. Add tests in `tests/`

---

## License

Part of PIHUB distributed educational ecosystem.

---

## Support

For issues, questions, or improvements:
1. Check this README
2. Review troubleshooting section
3. Check existing build reports
4. Run with `--verbose` flag
5. Review logs in output directory

