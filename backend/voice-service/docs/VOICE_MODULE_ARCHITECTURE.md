# Voice Module Architecture

## Scope

The voice module is implemented as a separate backend service and exposed through the PIHUB gateway and nginx.

It covers:

- Voice question answering
- Text to speech
- Speech to text
- Audio asset playback
- Streaming TTS
- Pre-generated chapter audio lookup
- Voice metrics

The module is split across two deployable layers:

- `voice-service`: owns the voice business logic, storage, STT/TTS boundaries, and metrics
- `gateway`: exposes the public API and proxies requests to `voice-service`

## High-Level Architecture

```text
Client
  -> nginx
  -> gateway
  -> voice-service
```

For voice endpoints, the public API is available in both forms:

```text
/api/voice/*
/voice/*
```

The gateway now exposes both aliases so the `audio_url` returned by the service remains directly usable.

## Internal Service Layout

```text
backend/voice-service/
  api/          FastAPI routes
  services/     VoiceGateway, tutor adapter, service errors
  models/       Request and response schemas
  audio/        Audio storage and manifest registry
  cache/        Voice cache abstraction and in-memory implementation
  stt/          STT engine boundary and providers
  tts/          TTS engine boundary and providers
  streaming/    Streaming TTS orchestration
  analytics/    Counters and latency snapshots
  tests/        Contract and integration tests
```

## Runtime Flow

### Voice Query

```text
POST /voice/query
  -> manifest lookup for pre-generated audio
  -> cache lookup
  -> tutor adapter
  -> TTS synthesis
  -> audio storage
  -> cached response
```

### TTS

```text
POST /voice/tts
  -> cache lookup if enabled
  -> TTS synthesis
  -> audio storage
  -> response with audio_id and audio_url
```

### STT

```text
POST /voice/stt
  -> upload file
  -> STT engine transcribe
  -> transcript response
```

### Audio Playback

```text
GET /voice/audio/{asset_id}
  -> filesystem audio lookup
  -> full response or Range response
```

## Public Endpoints

These are the endpoints intended for clients through nginx and the gateway.

| Method | Path | Purpose | Response |
| --- | --- | --- | --- |
| `POST` | `/api/voice/query` | Ask the voice tutor a question | JSON |
| `POST` | `/voice/query` | Public alias for voice query | JSON |
| `POST` | `/api/voice/tts` | Synthesize text to speech | JSON or audio stream |
| `POST` | `/voice/tts` | Public alias for TTS | JSON or audio stream |
| `POST` | `/api/voice/stt` | Speech to text upload | JSON |
| `POST` | `/voice/stt` | Public alias for STT | JSON |
| `GET` | `/api/voice/audio/{asset_id}` | Fetch audio bytes | `audio/wav` or other audio type |
| `GET` | `/voice/audio/{asset_id}` | Public alias for audio playback | `audio/wav` or other audio type |
| `GET` | `/api/voice/metrics` | Voice counters and latencies | JSON |
| `GET` | `/voice/metrics` | Public alias for metrics | JSON |
| `WS` | `/api/voice/stream` | Voice streaming session | WebSocket frames |
| `WS` | `/voice/stream` | Public alias for voice streaming | WebSocket frames |
| `WS` | `/api/v1/voice/stream` | Backward-compatible stream alias | WebSocket frames |
| `GET` | `/health` | Gateway health, includes voice health | JSON |
| `GET` | `/discovery` | Gateway discovery payload | JSON |

## Internal Voice-Service Endpoints

The gateway proxies to these internal service routes:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/voice/query` | Voice tutor orchestration |
| `POST` | `/voice/tts` | TTS synthesis |
| `POST` | `/voice/stt` | STT transcription |
| `GET` | `/voice/audio/{asset_id}` | Audio retrieval |
| `GET` | `/audio/{asset_id}` | Audio retrieval alias |
| `GET` | `/voice/metrics` | Metrics snapshot |
| `WS` | `/voice/stream` | Voice streaming session |
| `GET` | `/health` | Service health |
| `GET` | `/docs` | OpenAPI UI |
| `GET` | `/openapi.json` | OpenAPI schema |

## Important Implementation Details

- `VoiceGateway` resolves pre-generated chapter audio before calling the tutor or TTS runtime.
- `FileSystemAudioStorage` stores generated audio and supports byte-range reads.
- `AudioManifestRegistry` maps chapter manifests to summary and concept audio assets.
- `VoiceMetrics` tracks counters and latency samples in memory.
- `VoiceStreamer` supports chunked audio streaming for TTS flows.
- `RagTutorEngine` calls the existing inference-service tutor endpoint over HTTP.
- `VoiceBackendManager` selects the active STT backend and falls back from Gemma to Faster Whisper when needed.

See also:

- [Voice backend switching](docs/VOICE_BACKEND_SWITCHING.md)

## Endpoint Behavior Notes

- `audio_url` returned by query and TTS responses is `/voice/audio/{asset_id}`.
- Range requests are supported with `Accept-Ranges: bytes` and `Content-Range`.
- `cache_status` is returned as `hit` or `miss`.
- The voice service currently uses the configured runtime boundary for STT/TTS, with a mock path available for local development.

## Live Performance Snapshot

Measurements below were taken from the running local Docker stack on `http://127.0.0.1`.
They reflect gateway-to-service round trip time on localhost, not remote production latency.

### Endpoint Latency

Command pattern used:

```bash
curl -s -o /dev/null -w '%{http_code} %{time_total}' ...
```

| Endpoint | HTTP | Min | Avg | Max | Notes |
| --- | --- | ---: | ---: | ---: | --- |
| `GET /api/voice/metrics` | 200 | 0.0037s | 0.0044s | 0.0053s | Fast metadata path |
| `POST /api/voice/tts` | 200 | 0.0037s | 0.0038s | 0.0038s | Local mock/runtime path |
| `POST /api/voice/query` | 200 | 0.0034s | 0.0036s | 0.0038s | Includes tutor and synthesis plumbing |
| `GET /api/voice/audio/{asset_id}` | 200 | 0.0033s | 0.0035s | 0.0038s | Cached filesystem audio read |

### Current Metrics Snapshot

```json
{
  "voice_query_requests": 4,
  "audio_cache_misses": 2,
  "audio_cache_hits": 5,
  "tts_requests": 3
}
```

### Audio Retrieval Headers

Observed on `GET /api/voice/audio/{asset_id}`:

```text
Content-Type: audio/wav
Content-Length: 52
ETag: <sha256>
Accept-Ranges: bytes
Cache-Control: public, max-age=3600
```

## Observations

- The voice service is live and healthy in Docker.
- The gateway rebuild now exposes the public `/voice/*` aliases, so audio URLs returned by the service are reachable.
- The measured localhost latencies are very low because the stack is running in the same machine and the TTS/STT implementations are not doing heavy remote inference in this benchmark.

## Files To Inspect

- [Gateway routes](../gateway/app/main.py)
- [Voice API routes](api/routes.py)
- [Voice gateway logic](services/voice_gateway.py)
- [Voice metrics](analytics/metrics.py)
- [Audio storage](audio/storage.py)
- [Audio manifest registry](audio/manifest_registry.py)
