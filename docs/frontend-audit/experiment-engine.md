# Experiment Engine — Detailed Breakdown

**Scope:** `Mentora/lib/features/experiment` — 526 Dart files: `runtime/*`, `builder/*`, `investigation/*`, `assessment/*`, `phet/*`, `guided_runtime/*`, `immersive_runtime/*`, `visualization_first/*`, `experience/lab_v2/*`, `platform/*`, `data/*`  
**Date:** 2026-09-12 — `Mentora` vs `IDP/offline_tutor_app` vs `pihub/backend/experiment-service`  
**Routing:** **Not reachable** from `MentoraHomeScreen` 5-tab — `ExperimentHubScreen`, `ExperimentPlayerScreen`, `BuilderScreen`, `LabReportsScreen` are dead routes (see `frontend-audit/README.md` reachable ~35/853 files). Only toy `RuntimeWorld` inline HTML canvas `chapter_learning_workspace_screen:547` is live.

---

## 1. Architecture — God Object vs Orphaned Stack

### 1.1 `runtime/runtime_world.dart:42-424` — God Object

| Subsystem | File | Status |
|---|---|---|
| `VariableStore, ObjectRegistry, RuntimeObjectLifecycleManager, 3 registries (schema/behavior/renderer)` | `runtime/variable_store, object_registry, objects/*` | Lifecycle registers behaviors/renderers **never unregistering** per `objects.initialize()` re-init → leak. `RuntimeLoader.loadFromManifest:20` calls `RuntimeObjectFactory.registerDefaults()` every load. |
| `RuntimeBindingEngine/Registry` | `runtime/bindings/*` | Listens to `VariableStore` but `RuntimeWorld.tick()` never calls `bindingEngine.tick()` — bindings only on variable events. |
| `MeasurementStore/Collector` | `runtime/measurements/*` | Collector `eventBus` filter `message=='VariableUpdated':40` brittle; `VariableStore` also emits `VariableChanged` `:127` → split-brain ignored. Ring-buffer `100` (`line_graph_behavior:9`) truncates export silently. |
| `ObservationStore/Scheduler/Exporter` | `runtime/observations/*` | `Scheduler` uses `clock.elapsedTime` but `tick()` only when `clock.isRunning:222` — manual `recordObservation()` while paused lost. |
| `SensorManager` | `runtime/sensors/runtime_sensor_manager:14` vs legacy `sensor_manager:11` | 2 sensor stacks duplicated (`SensorManager` old `SensorType` vs `RuntimeSensorType`). |
| `Canvas, VisualBindings, AnimationEngine, VisualTemplateEngine, PresetRegistry, SceneComposer, VisualizationRuntimeCoordinator, RuleEngine` | `runtime/simulation/*`, `visual_templates/*`, `visual_presets/*`, `visualization_first/*` | Visual bindings re-added without diff `visual_template_engine:50-66` `originX=110+(index%3)*210` fixed grid overlaps; `sceneComposer.composePresetById:204` after canvas init → actor overwrite. |

* `initialize:177-209` no rollback — half-wired if `visualTemplates.initialize()` throws.
* `tick:211-225` drives 6 subsystems sequentially, **no error isolation** — `variableExecutor` exception halts observations/rules.
* `dispose:304` `unawaited(sensors.dispose())` race on hot-restart.
* **Duplicate registration:** `RuntimeLoader.loadFromManifest:20` every load.

### 1.2 Two Competing Runtime Stacks

| Stack A (Current) | Stack B (Orphaned) |
|---|---|
| `RuntimeWorld + RuntimeLoader` (`runtime/runtime_world`, `runtime/runtime_loader`) — used `presentation/controllers/experiment_player_controller:79` | `BaseExperimentRuntime` hierarchy (`base_experiment_runtime:11`, `simulation_runtime:8`, `sensor_runtime:9`, `hybrid_runtime:4`, `observation_runtime:4`) + `RuntimeFactory:10` + `ExperimentExecutionOrchestrator:24` — **0 UI refs**, dead |
| Event: `RuntimeEventBus` `StreamController.broadcast` | Event: `_eventController` inside `BaseExperimentRuntime` |
| Metrics: `runtime_metrics, runtime_analytics, experiment_state_manager` | Metrics: duplicate `RuntimeMetrics, RuntimeSession` |

`HybridRuntime`/`ObservationRuntime` are empty `class X extends BaseExperimentRuntime { X(super.plan); }` shells.

**Fix:** Delete Stack B or make `RuntimeWorld implements ExperimentRuntime:1-8`; confusion blocks backend `experiment-runs` wiring.

---

## 2. Duplicate Physics Engines (Critical)

| Engine | File | Purpose | Overlap |
|---|---|---|---|
| `ExperimentFlameGame` Forge2D | `runtime/engine/experiment_flame_game:8` `Forge2DGame(gravity:9.8)` | Box2D bodies, walls | Hard-coded `physics_ball Vector2(100,100)` `runtime_object_factory:222` ignores JSON coords |
| `SimulationPlaygroundEngine` | `runtime/playground/engine/simulation_playground_engine:25` | Mock scene, variable updates, `if trigger==eventType.name` rule mock `133-156` | No physics; `SimulationRuntime:9` dead stack uses it |
| `Behavior` (5) | `runtime/behaviors/oscillation, flow, pulse, growth, orbit` | Time-driven `behavior_angle, flow_progress` via `FormulaEvaluator` | `OscillationBehavior:31` unsanitized `formulaStr` substring replace `vars={'var':1,'var_length':2}` wrong order (fixed `formula_evaluator:25-29` but caller collides `g, pi` keys) |
| `Effect` (12) | `runtime/effects/*` `EffectContext:1-40` | Canvas draw only, no collision | — |
| `VisualPreset/SceneComposer` | `visual_presets/5 presets + composer/simulation_scene_composer` | Hard positions duplicate flame positions | — |

**Pendulum has 4 impls:** `OscillationBehavior` + `pendulumSimulation` factory `runtime_object_factory:236` + `PendulumPreset` `visual_presets/presets/pendulum_preset` + `PendulumSceneControls` `experience/lab_v2/interactions`. **Fix:** Keep `RuntimeWorld+Behaviors+Effects` canonical; gate `Forge2D` behind `RuntimeProfile.physics` lazy load (currently 3MB dead).

---

## 3. `runtime/` Focus Bugs

| ID | File:Line | Bug |
|---|---|---|
| | `runtime/visual_templates/runtime_visual_template_engine:50-66` | No diffing — re-adds actors/bindings per `objects` change, stale actors remain. Hard grid `110+(index%3)*210` overflows 600px canvas. |
| | `runtime/variables/runtime_variable_executor:68-92` `_handleRuntimeEvent:127` `scheduleMicrotask(evaluateDirtyComputed)` per `VariableUpdated` | 100 vars → 100 microtasks quadratic. |
| | `measurements/runtime_measurement_collector:74` `_shouldCollect onChange` `latest.value != observedValue` on `dynamic` map → `{'x':1} != {'x':1}` true (identity) → spurious measurements. | Fix value equality. |
| | `immersive_runtime/visual_response_controller:24` `eventBus.stream.listen((_)=>setState)` no unsubscribe | Leak → `setState` on disposed. |
| | `formula/formula_evaluator:31` `replaceAll('e', math.e)` corrupts `var_temperature` → `var_tmath.e...` | Word-boundary `RegExp(r'\be\b')`. |
| | `phet/data/phet_catalog_service:187` `Directory.listSync` sync on UI thread | Block jank; use `Isolate`. |

---

## 4. `builder/` — Over-Engineered, Under-Wired

| File:Line | Finding |
|---|---|
| `builder/controllers/experiment_builder_controller:14-432` 14 responsibilities in one `ChangeNotifier` | `generateManifest:402` ignores `validate` result, `fetchExecutionPackage:192` builds locally `coverage:100` **without** `ExperimentManifestRepository.getExecutionPackage` (backend never consulted). `_referencesId:423` heuristic misses `valueVariable`. `deleteVariable:321` cascade deletes no undo. |
| `builder/storage/builder_draft_manager:100` `Timer.periodic(30s)` | Saves even if unchanged → churn `SharedPreferences` + `compute` jank. |
| `builder/storage/manifest_cache_service:6-43` singleton `Map` never persisted/bounded, `sha256(jsonEncode)` nondeterministic key order → collisions. | In-memory leak. |
| `builder/data/api/experiment_manifest_api_service:8-43` | 4 endpoints `POST /manifest/*` no retry/offline fallback → `isValid:false` generic error. |
| `builder/validation/builder_validator:16-250` | Thorough but misses `valueVariable` in `formula`, strict `manifest_sanitizer:1-54` whitelist `[scene,metadata,version]` strips `experience,mission,investigation` added by `runtime_loader:40-50`. |
| `screens/experiment_builder_screen:500+` | Single `build()` rebuilds all panels on `notifyListeners`. |

---

## 5. `investigation/` — Trial System Partial

`ExperimentTrial: trialId, trialNumber, parameterValues, status` — `trials/experiment_trial_manager:11-152` `startTrial:33` `world.start()`; `stopTrial:58` snapshots `world.observationStore` + `measurementStore` but `pause` without `completedTrial` if `save=false` → lost. `resetTrial:129` `world.stop()` no confirm discards. `storage/trial_repository: InMemoryTrialRepository` — **not persisted** after kill. `comparison/*` 4 files + `conclusions/*` 2 files **dead** (no caller). `analytics/investigation_analytics` counters no persistence.

---

## 6. `assessment/` — Minimal, Detached

`engine/assessment_engine:5-45` `isAutoGradable false → correct if answer.trim().isNotEmpty` → **free credit**; `possible<=0→100.0:29` auto-pass reflection-only; `models/assessment_question:isCorrect` string equality no tolerance. `rubrics/rubric_grader:5-23` weighted sum `observationScore/trialScore/questionScore` no cap → >100 possible, no caller (`ExperimentTrialManager` doesn't call), `reports/lab_report_builder` never invoked. UI `learning_progress_panel, report_panel` not mounted.

---

## 7. `phet/` — Catalog Works, Presentation Drifts

`data/phet_catalog_service:14-211` robust 3-tier fallback: installed pack `_loadInstalledCatalog:45` → gateway `_loadGatewayCatalog:85` (`RuntimeBackendUrl`, 4-8s) → bundled asset `_loadBundledFallback:115`. Good offline support. `models/experiment_descriptor: fromPhetCatalog` parses `slug, launchLocation`. **But** `presentation/experiment_catalog_screen` duplicates feature-wide catalog → user confusion; PhET player `WebView` no offline `File.exists` check → blank if pack missing.

---

## 8. `guided_runtime`/`immersive_runtime`/`visualization_first`/`experience/lab_v2`

* `guided_runtime/engine/guided_experiment_engine:17-225` `completeTask:77` auto-advances `advanceTask:95` → `completeMission:101` when no next task, but `currentTask` getter `null:45` → NPE. `_handleRuntimeEvent:156` validates every `VariableUpdated` via string `task.condition` brittle.
* `immersive_runtime/visual_response_controller:33` hard `if(sceneId.contains('water_cycle'))` etc. 8 variables hard-coded (`var_humidity...var_is_swinging`), leak listener `initState:24` never canceled, `TweenAnimationBuilder` to same value unnecessary.
* `visualization_first/runtime/visualization_runtime_coordinator:14-117` `state` public mutable, `particles` never cleared on re-init → leak.
* `experience/lab_v2/stage/experiment_stage:39` `CauseEffectOverlay` `hidden:true` mounted dead; `SceneDefinitionV3` vs legacy `RuntimeWorld scene` out of sync; `interactions/*` 10 scene controls hard-code `var_heart_rate` not data-driven.
* `platform/experiment_capability_provider_impl:22-150` sensor check 10ms `listen` false positive, `hasCamera=true` lies, cache no TTL.
* `data/experiment_sync_queue:8-71` **in-memory only** `Queue<PendingExperimentSync>` lost on kill, head-of-line blocking `break` (64), shared `Timer` per run missing (`_batchTimer` global for all `runId`s).

---

## 9. Dead Code Inventory

`hybrid_runtime:4`, `observation_runtime:4`, `sensor_manager:11` legacy vs `runtime_sensor_manager`, `ExperimentExecutionOrchestrator:24` dead, `builder/validation/manifest_sanitizer:1`, `manifest_cache_service:6`, `experience/engine/runtime_experience_engine` superseded, `immersive_runtime/*` controllers, `visualization_first/validation`, `investigation/comparison/*` 4, `conclusions/*` 2, `assessment/reports/*` 2, `runtime/tools/measurement_registry` not integrated, `visual_mapping/*`, `math_evaluator_service:16` duplicate.

---

## 10. Priority

**P0:** Unify runtime (`RuntimeWorld implements ExperimentRuntime`), persist `ExperimentSyncQueue` (`SharedPreferences` + `Map<String,Timer>` per `runId`), fix `visual_response_controller` leak + `formula_evaluator` word-boundary.  
**P1:** Wire `RubricGrader` into `stopTrial→completeRun`, make `Builder` `fetchExecutionPackage` call backend with `getCapabilities()`, wire `RuntimeAutosaveManager`.  
**P2:** Dedup `VisualPreset` vs `VisualizationFirst` — one visual system; delete `Hybrid/ObservationRuntime` shells.

*Evidence: `runtime_world:42` 22 subsystems, `behavior_registry:15` never auto-initialized, `ExperimentFlameGame:11` gravity 9.8 double with `9.81/length`, `lab_reports_screen:8` hardcoded 1 report.*
