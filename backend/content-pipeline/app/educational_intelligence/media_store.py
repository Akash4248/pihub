from __future__ import annotations

"""Offline media store for the enriched content agent.

Downloads enrichment media (videos + images) to local disk so the PIHUB app can
serve them without internet, compresses videos with ffmpeg (H.264 CRF — high
visual quality at a fraction of the size), and records metadata in MongoDB.

Everything degrades gracefully: if Cobalt / ffmpeg / MongoDB are unavailable the
store logs a warning and returns what it can. Media is content-addressed by a
hash of the source URL so re-runs are idempotent (no duplicate downloads).
"""

import asyncio
import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shared.config import get_settings

logger = logging.getLogger(__name__)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


@dataclass
class StoredMedia:
    media_id: str
    source_url: str
    media_type: str  # "video" | "image"
    local_path: str
    title: str = ""
    source: str = ""
    language: str = "en"
    topic: str = ""
    size_bytes: int = 0
    compressed: bool = False
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "source_url": self.source_url,
            "media_type": self.media_type,
            "local_path": self.local_path,
            "title": self.title,
            "source": self.source,
            "language": self.language,
            "topic": self.topic,
            "size_bytes": self.size_bytes,
            "compressed": self.compressed,
            "error": self.error,
            **({"extra": self.extra} if self.extra else {}),
        }


def _media_id(url: str) -> str:
    return hashlib.blake2b(url.encode("utf-8"), digest_size=12).hexdigest()


class MediaStore:
    """Download + compress + persist enrichment media for offline access."""

    def __init__(
        self,
        storage_path: str | None = None,
        mongo_url: str | None = None,
        mongo_db: str | None = None,
    ) -> None:
        s = get_settings()
        self.enabled = s.enable_media_download
        self.storage_path = Path(storage_path or s.media_storage_path)
        self.video_dir = self.storage_path / "videos"
        self.image_dir = self.storage_path / "images"
        self.timeout = s.media_download_timeout_seconds
        self.max_height = s.media_max_video_height
        self.crf = s.media_video_crf
        self.preset = s.media_video_preset
        self.user_agent = s.enrichment_user_agent
        self._mongo_url = mongo_url or s.mongo_url
        self._mongo_db_name = mongo_db or s.mongo_db
        self._enable_mongo = s.enable_mongo_storage
        self._mongo: Any | None = None
        self.cobalt_url = s.cobalt_api_url.rstrip("/")
        self._media_max_total_gb = float(getattr(s, "media_max_total_gb", 0.0) or 0.0)
        self._has_ffmpeg = shutil.which("ffmpeg") is not None
        if self.enabled:
            try:
                self.video_dir.mkdir(parents=True, exist_ok=True)
                self.image_dir.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                fallback_base = Path.cwd() / ".local_run" / "media"
                self.storage_path = fallback_base
                self.video_dir = fallback_base / "videos"
                self.image_dir = fallback_base / "images"
                try:
                    self.video_dir.mkdir(parents=True, exist_ok=True)
                    self.image_dir.mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass
                logger.warning("Permission denied for storage_path, falling back to %s", self.storage_path)


    # ------------------------------------------------------------------ mongo
    def _collection(self):
        if not self._enable_mongo:
            return None
        if self._mongo is None:
            try:
                from pymongo import MongoClient

                client = MongoClient(self._mongo_url, serverSelectionTimeoutMS=3000)
                self._mongo = client[self._mongo_db_name]
            except Exception as exc:  # noqa: BLE001 - offline-safe
                logger.warning("MongoDB unavailable (%s); media metadata not persisted", str(exc)[:150])
                self._enable_mongo = False
                return None
        db = self._mongo
        return db["media"] if db is not None else None

    def _persist(self, media: StoredMedia) -> None:
        col = self._collection()
        if col is None:
            return
        try:
            col.update_one(
                {"media_id": media.media_id, "language": media.language},
                {"$set": media.to_dict()},
                upsert=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Mongo persist failed for %s: %s", media.media_id, str(exc)[:150])

    def _find(self, media_id: str, language: str) -> dict[str, Any] | None:
        col = self._collection()
        if col is None:
            return None
        try:
            return col.find_one({"media_id": media_id, "language": language}, {"_id": 0})
        except Exception:
            return None

    # ----------------------------------------------------- subject dedup store
    # Persists the set of "important" topic keys per subject so cross-chapter
    # dedup survives container restarts (agent's in-memory set resets otherwise).
    def _dedup_col(self):
        db = self._mongo
        return db["seen_topics"] if db is not None else None

    def get_seen_topics(self, subject: str) -> list[str]:
        col = self._dedup_col()
        if col is None:
            return []
        try:
            doc = col.find_one({"_id": subject}, {"_id": 0, "topics": 1})
            return list(doc.get("topics") or []) if doc else []
        except Exception:
            return []

    def is_topic_seen(self, subject: str, topic_key: str) -> bool:
        col = self._dedup_col()
        if col is None:
            return False
        try:
            doc = col.find_one({"_id": subject}, {"_id": 0, "topics": 1})
            return bool(doc and topic_key in (doc.get("topics") or []))
        except Exception:
            return False

    def mark_topic_seen(self, subject: str, topic_key: str) -> None:
        col = self._dedup_col()
        if col is None:
            return
        try:
            col.update_one(
                {"_id": subject},
                {"$addToSet": {"topics": topic_key}},
                upsert=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Mongo dedup mark failed: %s", str(exc)[:150])

    def clear_subject_seen(self, subject: str | None = None) -> None:
        col = self._dedup_col()
        if col is None:
            return
        try:
            if subject is None:
                col.delete_many({})
            else:
                col.delete_one({"_id": subject})
        except Exception:
            pass

    # ----------------------------------------------------------- media budget
    # Hard cap on total local media size so the offline volume can't fill the
    # Pi's disk across many enrichment runs. 0 == unbounded.
    def _budget_ok(self) -> bool:
        cap_gb = getattr(self, "_media_max_total_gb", 0.0) or 0.0
        if cap_gb <= 0:
            return True
        try:
            used = sum(
                f.stat().st_size for f in self.storage_path.rglob("*") if f.is_file()
            )
            return used <= cap_gb * 1024**3
        except Exception:
            return True

    # ------------------------------------------------------------------ public
    async def store_video(self, url: str, title: str = "", source: str = "", language: str = "en", topic: str = "") -> StoredMedia:
        mid = _media_id(url)
        return await asyncio.to_thread(self._store_video_sync, mid, url, title, source, language, topic)

    async def store_image(self, url: str, title: str = "", source: str = "", language: str = "en", topic: str = "") -> StoredMedia:
        mid = _media_id(url)
        return await asyncio.to_thread(self._store_image_sync, mid, url, title, source, language, topic)

    # ------------------------------------------------------------------ video
    def _store_video_sync(self, mid: str, url: str, title: str, source: str, language: str, topic: str) -> StoredMedia:
        target = self.video_dir / f"{mid}.mp4"
        if target.exists():
            cached = self._find(mid, language)
            if cached:
                return StoredMedia(**{k: cached[k] for k in cached if k != "extra"})
            return self._record_existing(mid, url, "video", target, title, source, language, topic, compressed=True)

        if not self.enabled:
            return StoredMedia(mid, url, "video", "", title, source, language, topic, error="media_download_disabled")

        if not self._budget_ok():
            logger.warning("Media budget reached (%.0f GB cap); skipping download of %s", self._media_max_total_gb, url)
            return StoredMedia(mid, url, "video", "", title, source, language, topic, error="media_budget_exceeded")

        raw = self.video_dir / f"{mid}.raw.mp4"
        try:
            self._cobalt_download(url, raw)
        except Exception as exc:  # noqa: BLE001
            self._cleanup(raw)
            return StoredMedia(mid, url, "video", "", title, source, language, topic, error=f"download_failed: {str(exc)[:120]}")

        downloaded = raw if raw.exists() and raw.stat().st_size > 0 else None
        if downloaded is None:
            return StoredMedia(mid, url, "video", "", title, source, language, topic, error="download_produced_no_file")

        compressed = False
        if self._has_ffmpeg:
            try:
                self._compress_video(downloaded, target)
                compressed = True
                self._cleanup(downloaded)
            except Exception as exc:  # noqa: BLE001
                logger.warning("ffmpeg compression failed for %s: %s; keeping raw", mid, str(exc)[:120])
                downloaded.rename(target)
        else:
            downloaded.rename(target)

        media = StoredMedia(
            media_id=mid, source_url=url, media_type="video", local_path=str(target),
            title=title, source=source, language=language, topic=topic,
            size_bytes=target.stat().st_size if target.exists() else 0, compressed=compressed,
        )
        self._persist(media)
        return media

    def _compress_video(self, src: Path, dst: Path) -> None:
        """H.264 CRF encode — high perceptual quality, much smaller. Scales down
        to max_height while preserving aspect ratio (only if larger)."""
        vf = f"scale=-2:'min({self.max_height},ih)'"
        self._run(
            [
                "ffmpeg", "-y", "-i", str(src),
                "-vf", vf,
                "-c:v", "libx264", "-crf", str(self.crf), "-preset", self.preset,
                "-c:a", "aac", "-b:a", "96k",
                "-movflags", "+faststart",
                str(dst),
            ]
        )

    # ------------------------------------------------------------------ cobalt
    def _cobalt_download(self, url: str, dest: Path) -> None:
        """Fetch a video via the self-hosted Cobalt API (no yt-dlp).

        POST / with the source URL -> cobalt replies with a ``tunnel`` /
        ``redirect`` URL (or a ``picker`` list); we stream that to ``dest``.
        """
        import httpx

        quality = str(self.max_height) if self.max_height in {144, 240, 360, 480, 720, 1080, 1440, 2160, 4320} else "480"
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            resp = client.post(
                self.cobalt_url + "/",
                json={
                    "url": url,
                    "videoQuality": quality,
                    "youtubeVideoCodec": "h264",
                    "youtubeVideoContainer": "mp4",
                    "filenameStyle": "basic",
                },
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status")
            if status in {"tunnel", "redirect"}:
                media_url = data["url"]
            elif status == "picker" and data.get("picker"):
                media_url = data["picker"][0].get("url", "")
            else:
                raise RuntimeError(f"cobalt status={status}: {str(data.get('error', ''))[:120]}")
            if not media_url:
                raise RuntimeError("cobalt returned no media url")

            with client.stream("GET", media_url) as stream:
                stream.raise_for_status()
                with dest.open("wb") as fh:
                    for chunk in stream.iter_bytes(chunk_size=1 << 20):
                        fh.write(chunk)

    # ------------------------------------------------------------------ image
    def _store_image_sync(self, mid: str, url: str, title: str, source: str, language: str, topic: str) -> StoredMedia:
        ext = self._url_ext(url) or ".jpg"
        target = self.image_dir / f"{mid}{ext}"
        if target.exists():
            cached = self._find(mid, language)
            if cached:
                return StoredMedia(**{k: cached[k] for k in cached if k != "extra"})
            return self._record_existing(mid, url, "image", target, title, source, language, topic, compressed=False)

        if not self.enabled:
            return StoredMedia(mid, url, "image", "", title, source, language, topic, error="media_download_disabled")

        try:
            import httpx

            with httpx.Client(timeout=self.timeout, headers={"User-Agent": self.user_agent}, follow_redirects=True) as c:
                resp = c.get(url)
                resp.raise_for_status()
                target.write_bytes(resp.content)
        except Exception as exc:  # noqa: BLE001
            self._cleanup(target)
            return StoredMedia(mid, url, "image", "", title, source, language, topic, error=f"download_failed: {str(exc)[:120]}")

        media = StoredMedia(
            media_id=mid, source_url=url, media_type="image", local_path=str(target),
            title=title, source=source, language=language, topic=topic,
            size_bytes=target.stat().st_size, compressed=False,
        )
        self._persist(media)
        return media

    # ------------------------------------------------------------------ helpers
    def _record_existing(self, mid, url, mtype, target: Path, title, source, language, topic, compressed) -> StoredMedia:
        media = StoredMedia(
            media_id=mid, source_url=url, media_type=mtype, local_path=str(target),
            title=title, source=source, language=language, topic=topic,
            size_bytes=target.stat().st_size if target.exists() else 0, compressed=compressed,
        )
        self._persist(media)
        return media

    def _run(self, cmd: list[str]) -> None:
        import subprocess

        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=self.timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "command failed")[-200:])

    @staticmethod
    def _cleanup(path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    @staticmethod
    def _url_ext(url: str) -> str:
        import urllib.parse

        path = urllib.parse.urlparse(url).path.lower()
        for ext in _IMAGE_EXTS:
            if path.endswith(ext):
                return ext
        return ""
