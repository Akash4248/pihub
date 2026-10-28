# Voice Frontend API Contract

Base path:

```text
/api/voice
```

All frontend calls should go through the discovered PIHUB gateway URL. The voice service remains internal.

## Start Recording

Frontend-only operation.

Expected client behavior:

```text
Request microphone permission
Start local recording
Encode audio as wav/m4a/webm depending on platform support
Upload via POST /api/voice/stt
```

## Upload Audio For STT

```http
POST /api/voice/stt?language=en&enable_partial_transcripts=true
Content-Type: multipart/form-data
```

Form field:

```text
file=<audio file>
```

Response:

```json
{
  "success": true,
  "transcript": "what is photosynthesis",
  "language": "en",
  "partial_transcripts": [],
  "confidence": 0.9,
  "metrics": {}
}
```

Current runtime boundary:

```text
501 until Distil-Whisper Large-v3 adapter is wired.
```

## Ask Voice Tutor

```http
POST /api/voice/query
Content-Type: application/json
```

Request:

```json
{
  "question": "What is photosynthesis?",
  "grade": 8,
  "subject": "science",
  "chapter_id": "photosynthesis",
  "topic": "photosynthesis",
  "language": "en",
  "prefer_cached_audio": true,
  "require_curriculum_context": true
}
```

Response:

```json
{
  "success": true,
  "answer_text": "...",
  "audio_id": "...",
  "audio_url": "/voice/audio/...",
  "cache_status": "miss",
  "response_source": "rag_tutor",
  "context_used": [],
  "metrics": {}
}
```

Gateway audio URL:

```text
/api/voice/audio/{audio_id}
```

## Text To Speech

```http
POST /api/voice/tts
Content-Type: application/json
```

Request:

```json
{
  "text": "Photosynthesis is the process by which plants make food.",
  "voice": "default",
  "language": "en",
  "stream": false,
  "format": "wav",
  "cache": true
}
```

Response:

```json
{
  "success": true,
  "audio_id": "...",
  "audio_url": "/voice/audio/...",
  "cache_status": "miss",
  "format": "wav",
  "duration_ms": null
}
```

Current runtime boundary:

```text
501 until Svara TTS runtime adapter is wired.
```

## Stream Audio

```http
POST /api/voice/tts
Content-Type: application/json
```

Request:

```json
{
  "text": "Read this explanation aloud.",
  "stream": true,
  "format": "wav"
}
```

Response:

```text
Streaming audio bytes
```

## Play Audio Asset

```http
GET /api/voice/audio/{asset_id}
```

Supports range requests:

```http
Range: bytes=0-1023
```

Expected response headers:

```text
Accept-Ranges
Content-Length
Content-Range for partial responses
ETag
Cache-Control
Content-Type
```

## Metrics

```http
GET /api/voice/metrics
```

Response:

```json
{
  "stt_requests": 0,
  "tts_requests": 0,
  "voice_query_requests": 0,
  "audio_cache_hits": 0,
  "audio_cache_misses": 0
}
```
