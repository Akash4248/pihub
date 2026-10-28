"""
Pack Management Schemas for Distributed PiHub Architecture

Defines data structures for:
- Pack manifest generation
- Pack metadata
- Pack synchronization
- Pack integrity validation
"""

from datetime import datetime
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field


class PackChunk(BaseModel):
    """Educational chunk metadata for pack inclusion"""
    chunk_id: str = Field(..., description="Unique chunk identifier")
    text: str = Field(..., description="Chunk content")
    embedding: List[float] = Field(..., description="Vector embedding")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Chunk metadata (grade, subject, chapter, topic, etc.)")
    score: float = Field(default=0.0, description="Retrieval score")


class PackMetadata(BaseModel):
    """Core pack metadata"""
    pack_id: str = Field(..., description="Unique pack identifier (e.g., class7_science)")
    version: str = Field(default="1.0", description="Pack version (semantic versioning)")
    grade: Optional[int] = Field(default=None, description="Grade level")
    subject: Optional[str] = Field(default=None, description="Subject name")
    language: Optional[str] = Field(default=None, description="Language code (e.g., en, kn)")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="Pack creation timestamp")
    updated_at: datetime = Field(default_factory=datetime.utcnow, description="Last update timestamp")
    chunk_count: int = Field(default=0, description="Total chunks in pack")
    media_count: int = Field(default=0, description="Total media items in pack")
    compressed_size_mb: float = Field(default=0.0, description="Compressed pack size in MB")
    uncompressed_size_mb: float = Field(default=0.0, description="Uncompressed pack size in MB")
    compression_ratio: float = Field(default=0.0, description="Compression ratio (0.0-1.0)")


class PackManifest(BaseModel):
    """Complete pack manifest with integrity information"""
    metadata: PackMetadata = Field(..., description="Pack metadata")
    chunks: List[PackChunk] = Field(default_factory=list, description="Educational chunks")
    media_files: Dict[str, str] = Field(default_factory=dict, description="Media file paths")
    checksum: Optional[str] = Field(default=None, description="SHA256 checksum of pack contents")
    archive_path: Optional[str] = Field(default=None, description="Path to compressed pack archive")
    
    class Config:
        json_schema_extra = {
            "example": {
                "metadata": {
                    "pack_id": "class7_science",
                    "version": "1.0",
                    "grade": 7,
                    "subject": "science",
                    "language": "english",
                    "chunk_count": 145,
                    "media_count": 12,
                    "compressed_size_mb": 5.2,
                    "compression_ratio": 0.15
                },
                "chunks": [
                    {
                        "chunk_id": "chunk_001",
                        "text": "Photosynthesis is the process...",
                        "embedding": [0.1, 0.2, -0.3],
                        "metadata": {"chapter": "Nutrition", "topic": "Photosynthesis"}
                    }
                ],
                "media_files": {
                    "diagram_1": "media/photosynthesis_diagram.png"
                }
            }
        }


class PackGenerationRequest(BaseModel):
    """Request to generate a new pack"""
    pack_type: str = Field(..., description="Type: 'class', 'chapter', 'language', 'media'")
    grade: Optional[int] = Field(default=None, description="Grade level for class packs")
    subject: Optional[str] = Field(default=None, description="Subject for class packs")
    chapter: Optional[str] = Field(default=None, description="Chapter for chapter packs")
    language: Optional[str] = Field(default=None, description="Language code for language packs")
    include_media: bool = Field(default=False, description="Include media files")
    compression: str = Field(default="gzip", description="Compression format: gzip, zstd, xz")
    quantize_embeddings: bool = Field(default=False, description="Use quantized embeddings")


class PackGenerationResponse(BaseModel):
    """Response from pack generation"""
    pack_id: str = Field(..., description="Generated pack ID")
    version: str = Field(..., description="Pack version")
    status: str = Field(..., description="Generation status: pending, processing, completed, failed")
    chunk_count: int = Field(..., description="Total chunks")
    media_count: int = Field(..., description="Total media items")
    estimated_size_mb: float = Field(..., description="Estimated compressed size")
    manifest_url: Optional[str] = Field(default=None, description="URL to download manifest")
    download_url: Optional[str] = Field(default=None, description="URL to download compressed pack")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="Generation start time")


class PackListResponse(BaseModel):
    """List of available packs"""
    packs: List[PackMetadata] = Field(default_factory=list, description="Available packs")
    total_count: int = Field(default=0, description="Total pack count")
    total_size_mb: float = Field(default=0.0, description="Total storage used")


class PackDownloadRequest(BaseModel):
    """Request to download a pack"""
    pack_id: str = Field(..., description="Pack to download")
    version: Optional[str] = Field(default=None, description="Specific version (defaults to latest)")
    include_manifest: bool = Field(default=True, description="Include manifest with download")


class PackDownloadStatus(BaseModel):
    """Download progress status"""
    pack_id: str = Field(..., description="Pack ID being downloaded")
    status: str = Field(..., description="Status: pending, downloading, paused, completed, failed")
    bytes_downloaded: int = Field(default=0, description="Bytes downloaded so far")
    total_bytes: int = Field(default=0, description="Total bytes to download")
    progress_percent: float = Field(default=0.0, description="Progress percentage (0-100)")
    eta_seconds: Optional[int] = Field(default=None, description="Estimated time remaining")
    error_message: Optional[str] = Field(default=None, description="Error message if failed")
    resumed_count: int = Field(default=0, description="Download resume count")


class PackIntegrityCheck(BaseModel):
    """Integrity validation results"""
    pack_id: str = Field(..., description="Pack ID")
    is_valid: bool = Field(..., description="Pack integrity valid")
    checksum_matches: bool = Field(..., description="Checksum validation")
    chunk_count_matches: bool = Field(..., description="Chunk count matches manifest")
    media_files_intact: bool = Field(..., description="All media files present")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Check timestamp")
    error_details: Optional[str] = Field(default=None, description="Details if invalid")


class SyncManifestEntry(BaseModel):
    """Single pack entry in sync manifest"""
    pack_id: str = Field(..., description="Pack ID")
    version: str = Field(..., description="Pack version")
    checksum: str = Field(..., description="Content checksum")
    compressed_size_mb: float = Field(..., description="Compressed size")
    language: Optional[str] = Field(default=None, description="Language")
    grade: Optional[int] = Field(default=None, description="Grade level")
    subject: Optional[str] = Field(default=None, description="Subject")


class SyncManifest(BaseModel):
    """Manifest for synchronization between host and Pi"""
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Manifest generation time")
    host_version: str = Field(..., description="Host curriculum version")
    packs: List[SyncManifestEntry] = Field(default_factory=list, description="Available packs")
    total_packs: int = Field(default=0, description="Total pack count")
    total_size_mb: float = Field(default=0.0, description="Total storage needed")


class DeltaSyncRequest(BaseModel):
    """Request delta sync between Pi and host"""
    current_packs: Dict[str, str] = Field(default_factory=dict, description="Pi's current packs {pack_id: version}")
    pi_version: str = Field(..., description="Pi curriculum version")
    max_download_size_mb: int = Field(default=500, description="Max download size for this sync")


class DeltaSyncResponse(BaseModel):
    """Delta sync response"""
    packs_to_add: List[str] = Field(default_factory=list, description="Pack IDs to add")
    packs_to_remove: List[str] = Field(default_factory=list, description="Pack IDs to remove")
    packs_to_update: List[str] = Field(default_factory=list, description="Pack IDs with new versions")
    total_download_size_mb: float = Field(default=0.0, description="Total size for all updates")
    sync_priority: List[str] = Field(default_factory=list, description="Priority order for syncing")


# ============================================================================
# Educational Intelligence & Pedagogical Artifact Contracts
# ============================================================================

class ConceptNode(BaseModel):
    """Concept node in educational knowledge graph"""
    name: str = Field(..., description="Canonical academic concept name")
    definition: str = Field(..., description="Textbook explanation of concept")
    difficulty: str = Field(default="intermediate", description="easy, intermediate, or advanced")
    prerequisites: List[str] = Field(default_factory=list, description="Prerequisite concept names")
    related_concepts: List[str] = Field(default_factory=list, description="Directly related concept names")
    misconceptions: List[str] = Field(default_factory=list, description="Common student misconceptions")


class QuizQuestion(BaseModel):
    """4-choice multiple choice question with pedagogical explanation"""
    question_id: Optional[str] = Field(default=None, description="Unique question identifier")
    question: str = Field(..., description="Question stem text")
    option_a: str = Field(..., description="Option A")
    option_b: str = Field(..., description="Option B")
    option_c: str = Field(..., description="Option C")
    option_d: str = Field(..., description="Option D")
    correct_option: int = Field(..., ge=0, le=3, description="Index of correct option (0=A, 1=B, 2=C, 3=D)")
    explanation: str = Field(..., description="Step-by-step pedagogical explanation")


class Flashcard(BaseModel):
    """Revision flashcard with prompt/term and definition"""
    card_id: Optional[str] = Field(default=None, description="Unique flashcard identifier")
    term: str = Field(..., description="Front of flashcard (term or question)")
    definition: str = Field(..., description="Back of flashcard (definition or answer)")


class ChapterNotes(BaseModel):
    """Structured chapter summary and key revision takeaways"""
    key_takeaways: List[str] = Field(default_factory=list, description="Key takeaway bullets")
    summary: str = Field(default="", description="Comprehensive textbook chapter summary")
    real_world_applications: List[str] = Field(default_factory=list, description="Real world applications")


class ChapterArtifactsRequest(BaseModel):
    """Unified request to generate chapter intelligence & educational artifacts"""
    grade: Optional[int] = Field(default=None, description="Grade level (1-12)")
    subject: Optional[str] = Field(default=None, description="Subject name")
    chapter: Optional[str] = Field(default=None, description="Chapter title")
    language: Optional[str] = Field(default="english", description="Language code")
    pack_id: Optional[str] = Field(default=None, description="Target pack ID")
    chunks: Optional[List[Dict[str, Any]]] = Field(default=None, description="Pre-loaded chunk dicts")
    path: Optional[str] = Field(default=None, description="Relative or absolute path to textbook")
    options: Optional[Dict[str, Any]] = Field(default=None, description="Generation options")


class ChapterArtifactsResponse(BaseModel):
    """Unified response containing all structured pedagogical artifacts"""
    chapter_id: str = Field(..., description="Chapter or pack identifier")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Chapter metadata")
    content: List[Dict[str, Any]] = Field(default_factory=list, description="Processed chunk content")
    textbook: Dict[str, Any] = Field(default_factory=dict, description="Structured textbook hierarchy")
    concept_graph: Dict[str, Any] = Field(default_factory=dict, description="NetworkX-derived concept graph")
    worked_examples: List[Dict[str, Any]] = Field(default_factory=list, description="Worked examples")
    tutor_contexts: List[Dict[str, Any]] = Field(default_factory=list, description="Offline tutor context cards")
    artifacts: Dict[str, Any] = Field(default_factory=dict, description="Quizzes, flashcards, notes, etc.")
    quality_gate: Dict[str, Any] = Field(default_factory=dict, description="Quality verification report")
    reports: Dict[str, Any] = Field(default_factory=dict, description="Execution and coverage metrics")

