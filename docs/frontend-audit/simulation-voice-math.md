# Simulation / Experiment / Voice / Math Studio

**Scope:** `Mentora/lib/features/simulation_context/*`, `experiment/*` (phet, runtime, builder, investigation, assessment, guided_runtime), `voice/*`, `math_studio/*`  
**Date:** 2026-09-12

---

## 1. SimulationContext Bridging — Not-Wired (Partial)

| Path | File:Line | Status | Fix |
|---|---|---|---|
| Native runtime | `experiment/presentation/screens/experiment_player_screen.dart:78,181-187` `ExperimentContextBridge(eventBus, world)` → `simulationContextProvider.notifier.update(variables:…)` dispose `248` + `runtime/experiment/application/experiment_context_bridge:11-74` (60Hz throttle missing) | **Wired** but floods UI at 60Hz (`35-36` every `RuntimeEvent`). | Debounce `update` 200ms, throttle `currentState` not `'Active'` hardcoded `:67`. Double-nest `experiment.variables` vs flat `variables` spec mismatch. |
| PhET/WebView | `experiment/phet/presentation/experiment_player_screen.dart:37-46` static `update(experimentId,title,subject,provider,state:'Running')` on init, `InAppWebView:120-163` no `addJavaScriptHandler`/`evaluateJavascript`/`postMessage` | **Display-only** — WebView variable changes never reach Dart. Still open from prior audit. | `controller.addJavaScriptHandler('onSimulationState', callback: (vars)=>notifier.update(...))` + `onLoadStop: evaluateJavascript('window.postState()')`. |
| Chat/LLM | `chat/presentation/chapter_chat_screen:1-193` 0 hits for `simulationContext` | **Never reads** provider | `HybridInferenceService.streamTutorAnswer` needs `ref.watch(simulationContextProvider).toJson()` injected into `buildContextualPrompt` + `ContextCard()` above `ChatMessageList`. |
| Voice | `voice/services/voice_socket_service:151-165` `sendAudioComplete({context})` nests under `payload.simulation_context` vs spec root | **Wrong nesting** + `voice/providers/voice_provider:114,130` caller `mic_button:39` passes no args → never called | `notifier.stopRecording(context: ref.read(simulationContextProvider).toJson())` + send `{'simulation_context': ctx}` at top level, chunked base64 streaming with `sequence++` not always `1:127`. |

---

## 2. Experiment Run Lifecycle

| ID | File:Line | Bug |
|---|---|---|
| | `presentation/controllers/experiment_player_controller:61-77` fuzzy `ExperimentTemplates.allTemplates.firstWhere(sceneId==manifest.id || title==manifest.title || manifest.id.contains(...split('_')[0]))` | Template-copy not manifest-native — builder payloads mismatch title → `templateData={}` → empty scene → generic `failed`. Use `manifest.toJson()` direct via `RuntimeLoader.loadFromManifest(manifest)`. |
| | `application/orchestrator/experiment_execution_orchestrator:17-133` | **Dead orchestrator** — `ExperimentCapabilityAnalyzer`/`ExecutionPlanner` never imported in UI (only lightweight controller). Capability `selectedMode` never enforced. Wire or delete (~2.5k LOC Forge2D etc). |
| | `runtime/runtime_world:42-424` 19 subsystems (`variables, bindings, measurementStore, interactionBus, visualBindings, animationEngine...`) `initialize:177-209` implicit order, `tick:211-225` `animationEngine` before `clock.tick` → ` _visualElapsed` vs `clock.elapsedTime` split, `dispose:304` swallows sensor errors | God-object; split into `RuntimeCoordinator`+`VisualCoordinator`, unify clock. |
| | `presentation/screens/experiment_player_screen:204-239` recovery `FileRuntimeSessionRepository(getTemporaryDirectory)` unencrypted, not per-student, `autosave` not wired to `clock` pause. | Scope to student, encrypt. |

---

## 3. Offline Queue / Sync — In-Memory Lossy

| File:Line | Bug |
|---|---|
| `data/experiment_sync_queue:8-71` `Queue<PendingExperimentSync>` in-memory, `enqueue:16` `_attemptFlush()` even offline → `break` (64) head-of-line block, no `ConnectivityListener`, `print` leak, no persistence despite `pending_experiment_sync:27` `toJson`. | Persist to Drift/SharedPrefs, per-op retry, connectivity guard. |
| `data/experiment_run_sync_adapter_impl:46-68` global `Timer? _batchTimer` shared across `runId`s → timer for run A cancelled by run B, never flush; `completeRun:72` placeholder `experimentId:'', studentId:''` → backend drops analytics. | Per-run `Map<runId,Timer>`, populate from `SessionInfo`. |

---

## 4. Grading / Assessment — Naive & Insecure

| File:Line | Bug |
|---|---|
| `assessment/engine/assessment_engine:19-44` `isAutoGradable() false → correct if answer.trim().isNotEmpty` | **Free credit** for any non-empty free-text; `possible<=0→100.0` (29) auto-pass reflection-only. |
| `assessment/rubrics/rubric_grader:12-20` `observationScore = trials.any Observations?100:0` (1 obs →100), `normalizedWeight` uncapped → grade >100 possible | `clamp(0,100)` + validate weights sum 1. |

---

## 5. Voice STT/TTS — Stubbed Offline, Fragile Online

| File:Line | Bug |
|---|---|
| `services/offline_speech_service:21-36` `transcribeAudioFile` hard `"Explain chapter concept..."` | Not STT — stub. Replace with `speech_to_text` / whisper.cpp via platform channel. |
| `services/audio_recorder_service:21` `wav 16kHz` OK, but `VoiceProvider.stopRecording:127` `File(path).readAsBytes()` sync on main isolate → jank + temp files never deleted | `compute` + size check + `delete` after send. |
| `services/voice_socket_service:133-165` single chunk `sequence=1`, wrong nesting `payload.simulation_context` vs root | Chunk base64 4k slices, `{'type':'audio_complete','simulation_context':ctx}`. |
| `services/voice_stream_player:54-82` `StreamAudioSource` `sourceLength:null` → `just_audio` Android WAV header fail | Set `audio/pcm;codecs=opus` or WAV header; share single `AudioPlayer` vs `audio_player_service:5` double player. |
| `network/application/hybrid_inference_service:398,492` Gemma templating "handled in C++ JNI" hardcoded, `linux_tutor_inference_gateway:74` dev paths `/home/akash/Desktop/IDP/llama.cpp/...` | Test Gemma, make paths configurable via `LlmAdminChannelService`. |

---

## 6. Math Studio — Pseudo-3D + Calc Bugs

| ID | File:Line | Bug |
|---|---|---|
| | `math_studio/presentation/widgets/math_studio_3d_canvas:248-545` `Matrix4.rotateY/X` + manual `project()` 16×16 mesh 256 quads, no z-buffer → pop, `scale!=1` pinch+pan impossible | Use `flutter_gpu`/`model_viewer` or deep-equals `listEquals` + fix `shouldRepaint` `oldDelegate.points != points` identity compare. |
| | `widgets/geogebra_embedded_view:39-41` always `https://www.geogebra.org/3d` online-only, `transparentBackground:true` ~30% FPS drop, no auto-switch on error `271-274` | Offline bundle `assets/geogebra/*`, `transparentBackground:false`, `setState(_useGeoGebra=false)` on `onReceivedError`. |
| | `scientific_calculator_screen:88,92` `_expression.replaceAll('e','${math.e}')` corrupts `exp`; degree regex `sin|cos|tan\(([^)]+)\)` misses `sin(cos(x))` | `RegExp(r'\be\b')` + recursive, persist history (now RAM 20). |
| | `runtime/engine/experiment_flame_game:3-76` `Forge2DGame(gravity:9.8)` 3MB never instantiated | Dead weight — delete or lazy `RuntimeProfile.physics`. |

---

## 7. Duplicate Physics / UI

Forge2D dead vs `BehaviorStack` dart math pendulum drift; 4 visual pipelines `VisualizationRuntimeCoordinator` + `SimulationSceneComposer` + `RuntimeVisualTemplateEngine` + `RuntimeSimulationCanvas` double-create actors; 2 graph stacks `RuntimeMeasurementCollector/line_graph_renderer` vs `visualization_first/graphs/animated_graph_profile`.

**Dead LOC:** ~2.5k (Forge2D 76 + visual presets ~200 + duplicate graphs).

---

## 8. UI Perf — Fullscreen / WebView

| File:Line | Bug |
|---|---|
| `presentation/runtime_workspace/runtime_orientation_lock:6-15` no ref-count, 3 screens lock landscape → first `dispose` unlocks while second visible → portrait snap | Counter `lock()/_n++; unlock()/_n-- if 0 restore`. |
| `experience/lab_v2/widgets/fullscreen_lab_mode:15` fake fullscreen (color only) | `SystemChrome.setEnabledSystemUIMode(immersiveSticky)`. |
| `phet/presentation/experiment_player_screen:133-140` `allowFileAccessFromFileURLs:true` no `cacheEnabled` → re-download 4-8s each load, `placeholder.html` may be missing `pubspec:89` | `cacheMode: LOAD_CACHE_ELSE_NETWORK`, add asset, dispose controller. |

---

*Fix order: PhET JS bridge + chat injection, stub STT, offline queue persist, orientation ref-count, orientation lock race, Forge2D gate, Geogebra offline fallback, grading `clamp`.*
