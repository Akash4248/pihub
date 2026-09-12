# Math Studio — Detailed Audit

**Scope:** `Mentora/lib/features/math_studio` — 10 screens + 10 widgets + `application/` + `domain/` vs legacy (none — greenfield) vs `docs/math_studio_architecture.md`  
**Date:** 2026-09-12 — `flutter analyze` `withOpacity` deprecated, `math_studio_3d_canvas:248` pseudo-3D

---

## 1. 3D Rendering — Pseudo-3D, Repaint Storm

| File:Line | Finding | Fix |
|---|---|---|
| `widgets/math_studio_3d_canvas:287` `Matrix4.rotateY/X` + `project()` orthographic | Not `perspective+lookAt` as doc `1-48` promised. No perspective divide. | Perspective matrix. |
| `math_studio_3d_canvas:391-450` 16×16 256 quads fixed `i/j` order, no depth sort | Far over near pop. `fillPaint` lerp `avgZ` pseudo, not depth. | Painter's algorithm. |
| `math_studio_3d_canvas:549` `shouldRepaint: old.points != points` identity | Mutate `add()` in-place → no repaint; new list same values → storm. Flagged. | `listEquals` |
| `math_studio_3d_canvas:44` `_azimuth+=0.015` no `mod 2π` | Precision drift hours. | `% 2π` |
| `math_studio_3d_canvas:128` `onScaleUpdate if(scale!=1.0) zoom else pan` jitter ~1.02 spurious zoom | Threshold `abs(scale-1)>0.05` + `pointerCount`. |
| `math_studio_3d_canvas:82` inverse project hand-rolled `cosA/sinA/sinE` ignores `scale/center` → taps behind camera | `Matrix4.inverted` ray-cast. |
| `math_studio_3d_canvas:316` `if geometry else surface` points inside cube visible | Hidden-surface. |
| `math_studio_3d_canvas:284` `scale = min(w,h)*0.18*zoom` magic 0.18 | Auto-fit viewport. |
| `math_studio_3d_lab_screen:48` + `scientific_calculator:31` landscape lock `SystemChrome` no ref-count | First `dispose` unlocks while second visible → snap. | Counter `lock()/unlock()`. |

---

## 2. GeoGebra — Online-Only No Bridge

| File:Line | Finding |
|---|---|
| `widgets/geogebra_embedded_view:27-43` `https://www.geogebra.org/3d` always online | No `assets/geogebra/*` bundle — offline error. Arch promised offline bundle. |
| `geogebra_embedded_view:114` `transparentBackground:true` extra GPU layer | ~30% FPS drop; set `false`. `cacheMode: LOAD_CACHE_ELSE_NETWORK` missing → 4-8s reload. |
| `geogebra_embedded_view:120-140` no `addJavaScriptHandler/evaluateJavascript` | Variable changes never reach `simulationContextProvider`. Bridge stub. |
| `geogebra_embedded_view:134` `onReceivedError` sets `_hasError=true` but not `setState(_useGeoGebra=false)` | Manual toggle required — auto-fallback. |
| `math_studio_3d_lab_screen:178` `_useGeoGebra=false` default → GeoGebra dead code unless toggled, state loss `_points/_vectors` discarded on switch | Sync state. |

---

## 3. Calculator Logic — `e` Corruption, No High-Precision

| File:Line | Bug | Fix |
|---|---|---|
| `scientific_calculator_screen:88` `replaceAll('e','${math.e}')` | Corrupts `exp` → `2.71828xp`. | `RegExp(r'\be\b')` |
| `scientific_calculator_screen:92` `RegExp(r'(sin\|cos\|tan)\(([^)]+)\)')` nested `sin(cos(30))` missed | Recursive. |
| `scientific_calculator_screen:289` `x²` `^2` vs `^` ambiguous `2^2^2` | Parentheses guard. |
| `scientific_calculator_screen:102` `Parser().parse` each `=` + `decimal` promised doc `decimal` not used → `0.1+0.2≠0.3` | `decimal` lib. |
| `scientific_calculator_screen:26` `_history` RAM 20 not persisted | `SharedPreferences`. |
| `scientific_calculator_screen:66` `MC/MR/M+/M-` `M+` parses `"Error"`→0, `MR` concatenates `2`+`3`→`23` | Guard + operator. |

---

## 4. Workspace Duplication — 60% Copy-Paste

| File | Finding |
|---|---|
| `geometry_workspace:109-141` vs `geometry_playground:121-138` shoelace/perimeter identical | Shared `GeometryMath` mixin. |
| `geometry_workspace:453-686` `_GeometryPainter` vs `geometry_playground:347-473` `_PlaygroundPainter` identical `_paintSegmentMeasurement` | DRY. |
| `algebra_workspace:17-368` vs `algebra_balance_workspace:15-270` hard `xValue=2.0` unsynced, `onRemoveUnit` no UI button | Unified. |
| `math_studio_home:179` hero sine vs `functions_workspace:354` separate painters same math | Shared. |
| `widgets/formula_visualizers:6-233` 5 painters `Circle:12-22` clamp `min(max(radius,5),maxRadius)` lies, `Interest:130` `maxVal = max(total,1)` hides small principals | Base `FormulaPainter`. |
| `math_studio_workspace_shell:6-100` vs direct `Scaffold` in home/3d/calc | Shell `pushAndRemoveUntil` nukes stack → lost notes. |

---

## 5. Dead / Mocked Features

| File:Line | Finding |
|---|---|
| `widgets/socratic_math_solver_sheet:39-73` `_solveEquation` hard `_SolutionStep(x=2)` after 600ms | Mock — not Socratic, no LLM/math_expressions. |
| `geometry_playground:65` `_colorIndex` repeats colors overflow | Check length. |
| `domain/challenge:14-70` 6 challenges 3 routed (`func_downward_parabola` etc.), `geom_perimeter_100` unreachable, `stat_mean_20==20.0` floating flake | `±0.01` tolerance, catalog links. |
| `saved_explorations_screen:178` `onTap` always `AlgebraWorkspace(a=2,b=3,c=11)` ignores `saved data` | Restore `data`. |
| `exploration_repository:1-8` `sqflite` no `updatedAt DESC` index, no `onUpgrade` | Add migration. |

---

## 6. UI/UX + Performance

| File:Line | Bug |
|---|---|
| `math_studio_home:267` `GridView.count(crossAxisCount: floor(maxWidth/260).clamp(1,4))` `NeverScrollablePhysics` inside `SingleChildScrollView` forces full layout per frame | `SliverGrid` |
| `math_studio_workspace_shell:39` `pushAndRemoveUntil` destroys history | `popUntil` |
| `geometry_workspace:382` `Area: ${(area/100).toStringAsFixed(1)} cm²` magic `100` pixel→cm varies by density | Calibrate. |
| `functions_workspace:412` `exp.evaluate` per pixel `360` × `bind` per slider drag → jank + `addPostFrameCallback` mutating state during `paint` anti-pattern | Debounce `ValueNotifier` + isolate. |
| `math_studio_3d_canvas:392` `Path+Paint` per quad 256× per frame GC pressure | Reuse. |
| `algebra_balance_workspace:98` `Wrap` unbounded `xCount+unitCount` DoS spam `+` | Cap. |

---

## 7. Offline

| File:Line | Finding |
|---|---|
| `geogebra_embedded_view:41` needs connectivity, no `connectivity_plus` guard, no cached HTML | Bundle + `LOAD_CACHE_ELSE_NETWORK`. |
| `exploration_repository` correct `AppDatabase.instance` but no cloud sync queue → `Queue<PendingExperimentSync>` analogue missing. | — |
| `Mentora/docs/math_studio_architecture:15` promises `syncfusion_charts, flame_forge2d` not in `pubspec:26` | Drift. |

**Priority:** Calculator `RegExp \be\b` + persist history, 3D `shouldRepaint listEquals` + depth sort, ref-count orientation, GeoGebra offline bundle + auto-fallback, dedupe geometry painters into `core/math/geometry_math.dart`, replace mock solver with `math_expressions`, restore `saved data` on navigation, wire `challenge` catalogLinks.

*Evidence: `math_studio_3d_canvas:248-545` pseudo-3D, `geogebra:27` online-only, `scientific_calculator:88` e-corruption.*
