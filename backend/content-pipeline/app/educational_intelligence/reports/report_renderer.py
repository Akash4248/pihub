import json
import os
from pathlib import Path

try:
    from jinja2 import Template
except ImportError:
    Template = None

# Resolved from WORK_DIR (exported by run_backend.sh / compose) so bare
# Pipeline() works outside Docker; falls back to the legacy /shared path.
_REPORTS_ROOT = Path(os.environ.get("WORK_DIR", "/shared/work")) / "reports"


def _load_json(path):
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def _compute_scores(artifacts):
    if not artifacts:
        return {}
    raw = artifacts.get("artifacts") or artifacts
    scores = {}
    for art_type, items in raw.items():
        if isinstance(items, list) and items:
            first = items[0] if isinstance(items[0], dict) else {}
            keys = ["accuracy", "educational_value", "age_appropriateness",
                    "completeness", "clarity"]
            if any(k in first for k in keys):
                sc = {k: first.get(k, 0) for k in keys}
                sc["overall"] = sum(first.get(k, 0) for k in keys) / 5
                scores[art_type] = sc
    return scores


def _count_artifacts(artifacts):
    if not artifacts:
        return {}
    raw = artifacts.get("artifacts") or artifacts
    return {k: len(v) if isinstance(v, list) else 0 for k, v in raw.items()}


class ReportRenderer:
    def __init__(self, template_dir=None):
        if template_dir is None:
            template_dir = Path(__file__).parent / "templates"
        self.template_dir = Path(template_dir)
        self._template = None

    def _load_template(self):
        if self._template is not None:
            return self._template
        tmpl_path = self.template_dir / "pipeline_report.html"
        if not tmpl_path.exists():
            raise FileNotFoundError(f"Template not found: {tmpl_path}")
        with open(tmpl_path) as f:
            text = f.read()
        self._template = Template(text) if Template else text
        return self._template

    def render_job(self, job_id: str, duration: int = 0) -> str:
        job_dir = _REPORTS_ROOT / job_id
        if not job_dir.exists():
            raise FileNotFoundError(f"Job directory not found: {job_dir}")

        quality = _load_json(job_dir / "00-quality.json")
        plan = _load_json(job_dir / "01-plan.json")
        artifacts = _load_json(job_dir / "02-artifacts.json")
        critique = _load_json(job_dir / "03-critique.json")
        pack = _load_json(job_dir / "05-pack.json")

        scores = critique or _compute_scores(artifacts)
        counts = _count_artifacts(artifacts)

        stages = [
            ("Quality Analysis", bool(quality)),
            ("Planning", bool(plan)),
            ("Generation", bool(artifacts)),
            ("Critique", bool(critique) or bool(scores)),
            ("Pack", bool(pack)),
            ("Report", True),
        ]

        quality_score = quality.get("quality", {}).get("quality_score",
                        quality.get("quality_score", "N/A"))

        meta = _load_json(job_dir / "00-meta.json")

        context = {
            "job_id": job_id,
            "pdf_path": meta.get("pdf_path", ""),
            "grade": meta.get("grade", ""),
            "subject": meta.get("subject", ""),
            "chapter": meta.get("chapter", ""),
            "language": meta.get("language", ""),
            "duration": duration,
            "quality_score": quality_score,
            "stages": stages,
            "scores": scores,
            "counts": counts,
            "pack_path": pack.get("pack_path", pack.get("path", "")),
            "chunk_count": quality.get("chunk_count", artifacts.get("chunk_count", 0)),
            "artifact_count": sum(counts.values()),
        }

        tmpl = self._load_template()
        if Template and isinstance(tmpl, Template):
            return tmpl.render(**context)
        html = tmpl
        for k, v in context.items():
            html = html.replace("{{ " + k + " }}", str(v))
        return html

    def list_jobs(self) -> list[dict]:
        if not _REPORTS_ROOT.exists():
            return []
        jobs = []
        for entry in sorted(_REPORTS_ROOT.iterdir(), reverse=True):
            if entry.is_dir():
                meta = _load_json(entry / "00-meta.json")
                quality = _load_json(entry / "00-quality.json")
                report_path = entry / "report.html"
                jobs.append({
                    "job_id": entry.name,
                    "pdf_path": meta.get("pdf_path", ""),
                    "grade": meta.get("grade", ""),
                    "subject": meta.get("subject", ""),
                    "chapter": meta.get("chapter", ""),
                    "language": meta.get("language", ""),
                    "quality_score": quality.get("quality", {}).get("quality_score", "N/A"),
                    "has_report": report_path.exists(),
                    "has_artifacts": (entry / "02-artifacts.json").exists(),
                })
        return jobs
