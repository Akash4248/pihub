from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz

logger = logging.getLogger(__name__)


@dataclass
class ExtractedImage:
    filename: str
    page_number: int
    width: int
    height: int
    image_index: int
    saved_path: str
    format: str = "png"
    description: str = ""
    is_figure: bool = False
    caption: str | None = None


class ImageExtractor:
    """Extract important images/figures from PDF pages using PyMuPDF.

    Filters out small/decorative images (icons, bullets, logos) and
    full-page background images, saving only meaningful figures.
    """

    MIN_FIGURE_SIZE = 100
    MAX_FIGURE_SIZE = 2000
    SAVED_FORMAT = "png"

    def __init__(self, job_dir: str | None = None):
        self.job_dir = job_dir

    def set_job_dir(self, job_dir: str) -> None:
        self.job_dir = job_dir

    def extract(self, pdf_path: str) -> dict[str, Any]:
        """Extract important images from a PDF.

        Args:
            pdf_path: Absolute path to the PDF file.

        Returns:
            Dict with:
              - images: list of ExtractedImage metadata
              - image_dir: path where images were saved
              - total_found: total image refs in PDF
              - figures_saved: number saved
        """
        pdf = Path(pdf_path)
        if not pdf.exists():
            logger.warning("PDF not found: %s", pdf_path)
            return {"images": [], "image_dir": "", "total_found": 0, "figures_saved": 0}

        image_dir = self._get_image_dir(pdf)
        os.makedirs(image_dir, exist_ok=True)

        doc = fitz.open(str(pdf))
        extracted: list[ExtractedImage] = []
        total_found = 0

        for page_num in range(doc.page_count):
            page = doc[page_num]
            images = page.get_images(full=True)
            total_found += len(images)

            for img_idx, img in enumerate(images):
                xref = img[0]
                try:
                    pix = fitz.Pixmap(doc, xref)
                except Exception:
                    continue

                w, h = pix.width, pix.height

                if w < self.MIN_FIGURE_SIZE or h < self.MIN_FIGURE_SIZE:
                    pix = None
                    continue
                if w >= self.MAX_FIGURE_SIZE or h >= self.MAX_FIGURE_SIZE:
                    pix = None
                    continue

                saved = self._save_pixmap(pix, pdf, page_num, img_idx)
                pix = None

                if saved:
                    extracted.append(ExtractedImage(
                        filename=saved["filename"],
                        page_number=page_num + 1,
                        width=w,
                        height=h,
                        image_index=total_found,
                        saved_path=saved["path"],
                        format=self.SAVED_FORMAT,
                        is_figure=(w > 150 and h > 150),
                    ))

        doc.close()
        logger.info(
            "Extracted %d/%d images from %s (saved to %s)",
            len(extracted), total_found, pdf.name, image_dir,
        )

        return {
            "images": [self._to_dict(img) for img in extracted],
            "image_dir": str(image_dir),
            "total_found": total_found,
            "figures_saved": len(extracted),
        }

    def _get_image_dir(self, pdf: Path) -> Path:
        if self.job_dir:
            base = Path(self.job_dir)
        else:
            from shared.config import get_settings
            try:
                base = Path(get_settings().upload_dir) / "images" / pdf.stem
            except Exception:
                base = Path("/tmp/pihub-images") / pdf.stem
        img_dir = base / "figures"
        img_dir.mkdir(parents=True, exist_ok=True)
        return img_dir

    def _save_pixmap(
        self, pix: Any, pdf: Path, page_num: int, img_idx: int
    ) -> dict[str, Any] | None:
        try:
            if pix.n > 4:
                pix = fitz.Pixmap(fitz.csRGB, pix)
            img_bytes = pix.tobytes(self.SAVED_FORMAT)
        except Exception:
            return None

        filename = f"page{page_num + 1:03d}_img{img_idx:03d}.{self.SAVED_FORMAT}"
        save_dir = self._get_image_dir(pdf)
        save_path = save_dir / filename
        save_path.write_bytes(img_bytes)

        return {"filename": filename, "path": str(save_path)}

    def _to_dict(self, img: ExtractedImage) -> dict[str, Any]:
        return {
            "filename": img.filename,
            "page": img.page_number,
            "width": img.width,
            "height": img.height,
            "path": img.saved_path,
            "is_figure": img.is_figure,
        }
