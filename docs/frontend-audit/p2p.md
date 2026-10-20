# P2P Feature — Exhaustive Audit

**Scope:** `Mentora/lib/features/p2p` (presentation, data, application), `lib/features/network` 55 managers, `android/P2PManager.kt`, `pihub/backend/pihub/distribution/pack_manager.py`  
**Date:** 2026-09-12 — `p2p_screen.dart` truncated at `254-382` syntax-broken, `flutter analyze` `799` includes `allowMultiple` deprecated

---

## 1. P2P Presentation — Corrupted Merge + Duplicate Flows

| ID | File:Line | Severity | Bug |
|---|---|---|---|
| | `p2p/presentation/p2p_screen:254-382` `_handlePendingIncomingTransfers()` nests `Future<void> _shareSelectedPackNative` `255` and `_importReceivedPackNative` `294` **inside** its body + floating `if (!mounted ... ) return; :339` outside any method | **Syntax-broken** — AOT build fails; `dart analyze` unreachable methods; hot-reload masks. |
| | `p2p_screen:468-521` `_exportAndSendPack` vs `524-588` `_exportAndSendChapter` 98% duplicate (trust `478-486` ≡ `534-542`) | DRY: generic `_exportAndSend({Future<String> export, P2PPeer peer})`. |
| | `p2p_screen:478,534` `if (!trusted) trustPeer` auto-trust no dialog | **Auth bypass** — LAN attacker auto-whitelisted `ConflictAlgorithm.replace`. |
| | `p2p/data/p2p_bundle_service:80-161` `payloadType: chapter-rag-bundle` JSON via `RagRepository` vs `p2p_screen:273,489` `ContentPackArchiveService` `.otpack/.zip` | 2 parallel channels (RAG JSON vs otpack) same peer list → confusion, no type indicator. |
| | `p2p_screen:55-56` `_sharedSecretController` never rendered (no TextField) | Dead secret UI; no QR (`standard_port` `p2p_secret_bootstrap` not shown). |
| | `p2p_screen:56` `TabController(length:3) 88` disposed `96` **never used** (`_buildTopStatusBar` custom, no `TabBar`) | Dead code. |
| | `p2p_screen:873-888` `_formatThroughput/_formatEta` 0 callers | Dead helpers; telemetry card uses `toStringAsFixed` `1086` directly. |
| | `p2p_screen:694` `lowerPath.endsWith('.otpack')` import via `importPackArchive` **without HMAC** vs `709` RAG path with `verificationSecrets` | RAG verified, otpack not — malicious otpack bypass. |
| | `p2p_screen:255` `Share.shareXFiles` fallback `Share.share(summaryText):282` no size check >50MB `P2PManager:44` → native share succeeds, P2P `sendBundle` later fails silent. | Size guard + shared `p2p_exports` leak `321` never deleted. |
| | `p2p_screen:81-82` `_exportProgress/_importProgress` vs telemetry `P2PTransferTelemetry` polled `Timer 1s:109` | Poll waste when idle (`SizedBox.shrink 1043`); throughput dead. |
| | `p2p_screen:891-943` `_buildTopStatusBar` teal `0ms` latency when `currentClassroom` null; `964-966` fake `Explore/Resources/Collaborate` buttons `onPressed:{}` | Dead nav, wrong latency. |
| | `p2p_screen:1094` radar fixed `400` height, `radius=100+index%2*40`, angles `index*2π/length` overlap >8 peers; horizontal `Active Study Rooms 180` hard 3 dots `1308`. | Clutter, fake avatars. |

---

## 2. Discovery — 3 Systems Competing

| System | File:Line | Finding |
|---|---|---|
| 1 P2P UDP | `P2PManager.kt:737-791` `discoverPeersOnLan()` `45889 DISCOVER_OFFLINE_TUTOR` | Consts `TCP 45888 UDP 45889:39` |
| 2 PiHub subnet scan | `PiHubDiscoveryCoordinator:252-279` TCP 254 IPs port 80 `283-293` + `GET /health` `297-355` | 30+ prints per scan `122-356` no `kDebugMode` guard |
| 3 BackendDiscovery | `BackendDiscoveryService:111-153` `discovery.discover()` → `AppEnvironment.backendBaseUrl` health probe | No shared `port/healthPath` constants; divergence hard. |
| Hardcode | `p2p_screen:413` `OfflineTutor_PeerHub` `192.168.43.1:45888` hardcoded; Android 10+ IP varies `192.168.49.x` | No runtime detection. |
| Polling | `p2p_screen:109` `Timer.periodic 1s` + PiHub `30+` logs + `BackendDiscovery _startHealthChecks 30s` | 3 timers, wake lock not released. |

**Fix:** Centralize `app_environment.dart` `p2pTcpPort, p2pUdpPort, healthPath`, single `DiscoveryCoordinator` fan-in.

---

## 3. Pack Transfer — Incompatibility & Failures

| Frontend | Backend `distribution/pack_manager:25` | Finding |
|---|---|---|
| `p2p_bundle_service:134` `version:2 int` + `PackVersionManager version:int` | `version: str` semver `"1.2.3"` | `needsUpdate >` fails; type divergence. |
| Atomic `Socket write 293` `soTimeout 15000:286` | `update_transfer_chunk:140` chunked `bytes_transferred/chunks_received[]` resume | Atomic can't resume 50MB drop → restart; backend metrics `get_distribution_metrics:238` not wired. |
| `payloadHash sha256(payloadJson):122` + `Hmac SHA256 PIN:137` | `validate_pack_integrity:228` `==` no HMAC, `hashlib` unused | Two validations unintelligible; otpack bypass. |
| `PendingIncomingTransfer:134` in-memory `ConcurrentHashMap` + `RECEIVE_APPROVAL_TIMEOUT 45s:46` | `store.sync_queue` persisted | App kill loses pending; binder empty. |

**No resume/persist:** `P2PManager:793` `inboxDir filesDir/p2p_inbox` never evicts → fill; `fileName` sanitized `Reg("[^a-zA-Z0-9._-]")` but `..` preserved → `timestamp_..evil` still risky; `readUTF:400` filename unchecked; raw TCP no TLS/HMAC sender unauth (`isTrustedSenderAddress:85` not consulted in `receiveBundle`).

---

## 4. Offline Handling — 4 Queues, Memory Fake

| File | Finding |
|---|---|
| `network/application/offline_state_persistence:1` `Map _state` memory only despite name | Lost on restart; real `PendingSyncQueueRepository` sqlite not used by `OfflineRecoveryCoordinator:10`. |
| `connectivity_persistence_manager:52` `SharedPreferences` vs `connectivity_controller:27` etc. | 5 connectivity single sources (`connectivity_controller`, `connectivity_service`, `connectivity_state_coordinator`, `connectivity_diagnostics`...) |
| `PersistentSyncRecoveryManager` **duplicate class name** `persistent_sync_recovery_manager:1` vs `..._v2:47` same class different file | Import collision, `deferred_synchronization_coordinator:1` imports v1, `deferred_sync_manager` uses other. |
| `DeferredSynchronizationCoordinator` vs `DeferredSyncManager` vs `PendingSyncQueueRepository` vs `OfflineRecoveryCoordinator` vs `sync_queue_balancer` etc. | 4 layers queue overlapping, no single `drain` on connectivity restore; `flushPendingItems` early `AppEnvironment.enableBackend` check `62` silently drops offline. |

---

## 5. Duplication — 55 Managers

`pack_recovery_manager:7` ≡ `incremental_pack_recovery_manager:6` identical; `classroom_recovery_coordinator:6` (54L) vs `persistent_classroom_recovery_manager:31` (181L) 85% overlap; `multi_device_recovery` 3 variants; 5 connectivity, 5 metrics shells returning empty `Map`, 5 state persisters — bundle bloat, no owner for offline/online.

---

## 6. Security — XOR Crowbar

| File:Line | Bug |
|---|---|
| `p2p/application/p2p_secret_bootstrap_service:115-127` `sha256('$passcode::$nonce')` → `xorBytes` repeating-key 32B | Not encryption; known JSON plaintext break; low-entropy PIN no PBKDF2, nonce reuse; cipher length leaks secret. → `AES-GCM + PBKDF2 100k`. |
| `p2p/data/local/p2p_security_settings_repository:26` hex secret plaintext SQLite `p2p_settings` | No `flutter_secure_storage`/Keystore; rooted leak. |
| `P2PManager:284` raw `Socket` no HMAC/TLS, `receiveBundle:399` no `isTrustedSenderAddress` | Any LAN attacker 50MB bundle; `MAX_BUNDLE_BYTES 50MB` after header → holds `decisionLatch 45s` per IP `432` flood; `/proc/net/arp` MAC spoof `699-717`. |
| `p2p_bundle_service:220` `existsSync` blocking main isolate, `readAsString:233` 50MB JSON `compute(_decodeJson)` may OOM. | Async + chunked. |

---

## 7. UI/Dead

`_sharedSecret` no QR, `TabController` unused, `Permissions` `enabled` logic inverted `p2p_screen:1004`, `Android hasWifiDirectRuntimePermissions` gated but `NEARBY_WIFI_DEVICES` not requested → silently empty discovery.

**Fix order:** Fix syntax-broken merge `254-382`, replace XOR→AES-GCM + secure storage, enforce `isTrusted` in `receiveBundle`, rate-limit UDP, make transfer chunked+resume, centralize discovery, dedupe 55 managers → 1 `SyncQueueRepository`, wire `DiscoverySyncBridge` to both `distribution` and P2P.

*Evidence: `P2PManager.kt:39` consts, `MAX_BUNDLE 50MB:44`, `discoverPeers 800+1200ms:743`, `p2p_bundle:321` leak.*
