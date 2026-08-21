# Chapter 4 — ARGUS dashboard (web frontend)

This chapter documents the ARGUS web dashboard: its module architecture, every JS module, the liquid-glass design system, the deploy runbook, and Cognito user management.
_As of 2026-08-15._

The dashboard runs on the NP production account `506868652945`. The ARGUS deployer stood up the full stack there on 2026-08-10 (all 15 stages, 103 seconds), both detection models were retrained on that account (moth 2026-08-11, armyworm 2026-08-12), and the handover snapshot `argus-repo-snapshot-20260813.zip` was published 2026-08-13. Account `366356442579` is the retired development account where the system was built and validated; it stays reachable only as the historical record behind the report's evidence. Both deployments run the same frontend code from `web/dashboard_v4/`; only `js/config.js` differs, and the deployer templates that file at deploy time (section 4.4).

## 4.1 Role in the system

ARGUS is the operator dashboard for the Smart Pest Monitoring System. It is the surface CAG staff and NP assessors see. It shows live camera streams (Kinesis Video Streams HLS), the detection gallery (frames captured by the SIYI A8 Mini on the Unitree Go2, or by fixed cameras, and processed by AWS Rekognition Custom Labels), analytics (zone and trend views), and settings (camera thresholds, model start/stop, schedules, alert subscribers, test uploads).

The dashboard is a pure static frontend. It holds no business logic authority. Every data operation goes over HTTPS to the `pest-monitoring-api` Lambda behind API Gateway HTTP API `vzfl7s6z00` (Chapter 2), except live-video playback URLs, which come from the `kvs-hls-handler` Lambda. Authentication is Amazon Cognito. The JWT authorizer on API Gateway is the real enforcement boundary; the login screen in the browser is UX only.

It is served as plain files from an S3 website bucket behind CloudFront.

- **Production account `506868652945`** — the live deployment. Stood up by the ARGUS deployer 2026-08-10, verified serving `<title>ARGUS</title>` over HTTP 200 on 2026-08-11: bucket `argus-dashboard-506868652945`, CloudFront `E1YADURLSAVNFA`, URL **https://d1dtoxef7qmugl.cloudfront.net**, API `vzfl7s6z00`, Cognito pool `us-east-1_9selFDHpc`.
- **Retired development account `366356442579`** — where the system was built and validated; historical record only: bucket `pest-dashboard-366356442579`, CloudFront `E1423RGLAXWNSI`, URL **https://d1twcdquexdgj8.cloudfront.net**, API `zwpcbivmsj`, Cognito pool `us-east-1_ea0aJdusl`.

The raw S3 website URLs still work but are HTTP-only. Do not hand them out; passwords must not travel over plain HTTP. Where this chapter cites a concrete resource id without naming the account, it is the production account's; development-era ids appear only in passages clearly marked as history.

## 4.2 Inventory

All paths are relative to the project repository root, under `web/dashboard_v4/` unless noted. Every file below was read in full for this chapter; all described behavior is verified in code except where an inline OPEN ITEM note says otherwise.

| Name | Location | Purpose |
|---|---|---|
| `index.html` | `web/dashboard_v4/index.html` | Markup and mount points: topbar, tab bar, page content, image modal, login overlay, toast stack. Loads Chart.js and hls.js from CDN, then `js/main.js` as the single ES-module entry. |
| `styles.css` | `web/dashboard_v4/styles.css` | Base light theme (design tokens) plus the ARGUS v5.2 liquid-glass theme block appended at the end, plus v5.3 pagination CSS. 1736 lines. |
| `js/config.js` | `web/dashboard_v4/js/config.js` | The one per-host/env constants file: API base URL, Cognito region and client id, cache sizing, poll interval. |
| `js/auth.js` | `web/dashboard_v4/js/auth.js` | Cognito sign-in via raw `InitiateAuth` fetch. Token storage, refresh, sign-out, login overlay wiring. |
| `js/api.js` | `web/dashboard_v4/js/api.js` | Every backend call. Attaches `Authorization: Bearer <IdToken>`; any 401 reopens the login overlay. |
| `js/state.js` | `web/dashboard_v4/js/state.js` | The single shared mutable `state` object and the IndexedDB image cache (`imageCache`). |
| `js/utils.js` | `web/dashboard_v4/js/utils.js` | Toasts, time formatting (UTC to Singapore), `escapeHtml`, `camDisplayName`, camera classifiers, `convertToJpg`, Chart.js option factory. |
| `js/bbox.js` | `web/dashboard_v4/js/bbox.js` | Bounding-box extraction, overlay rendering, coordinate scaling, per-box verify (TP/FP) logic, image zoom/pan. |
| `js/gallery.js` | `web/dashboard_v4/js/gallery.js` | Gallery tab: filters, card grid, client-side pagination (50/page), throttled thumbnail loading, delete flow. |
| `js/modal.js` | `web/dashboard_v4/js/modal.js` | Image detail modal: meta pane (including the LLM Verifier row), review block, overlay + zoom wiring, delete button. |
| `js/live.js` | `web/dashboard_v4/js/live.js` | Live tab: camera cards, KVS stream on/off toggles, HLS playback via hls.js. |
| `js/analytics.js` | `web/dashboard_v4/js/analytics.js` | Analytics tab: stat cards, by-zone chart, zone x day heatmap, daily trend, camera health, by-camera split. |
| `js/settings.js` | `web/dashboard_v4/js/settings.js` | Settings tab and its four sub-tabs: Cameras, Test upload, Schedules, Alerts. Model status polling lives here. |
| `js/costs.js` | `web/dashboard_v4/js/costs.js` | Dormant Costs tab. Not in the tab list (the shared IAM user has no Cost Explorer access). Kept so it can be re-enabled by adding it back to `TABS` in `main.js`. |
| `js/main.js` | `web/dashboard_v4/js/main.js` | Bootstrap, tab router, topbar notifications, and the window bridge for inline onclick handlers. |
| Local fallback | `web/_archive/dashboard_v3_9.html` | Single-file pre-auth predecessor, kept untouched for emergencies. NOTE: doc lag. `docs/dashboard.md` says `web/dashboard_v3_9.html`; the file has since moved into `web/_archive/` (location verified on disk). |
| Pre-reskin backup | `web/_archive/dashboard_v4_v42_pre-argus_2026-07-10/` | Full copy of the dashboard before the ARGUS liquid-glass reskin. |

Cloud resources this chapter touches, production account `506868652945` (all created by the ARGUS deployer, Chapter 7): S3 `argus-dashboard-506868652945` (static website, us-east-1), CloudFront `E1YADURLSAVNFA`, Cognito User Pool `us-east-1_9selFDHpc` (pool name `pest-dashboard-users`) with app client `6vebotf45bp8u46cnraddiaplv` (`dashboard-web`), API Gateway HTTP API `vzfl7s6z00` (21 routes, JWT on all but `GET /stream/status`), and the frames bucket `argus-frames-506868652945` (the deployer writes the CORS rule for the website and CloudFront origins automatically — Test upload needs it).

Development-era equivalents on the retired account `366356442579` (history; kept because older records and the report's evidence reference them): S3 `pest-dashboard-366356442579`, CloudFront `E1423RGLAXWNSI`, Cognito pool `us-east-1_ea0aJdusl` with app client `4husu6afr835e235eu9dqp8av6`, API Gateway HTTP API `zwpcbivmsj` with JWT authorizer `cognito-dashboard` (id `enxa26`), frames bucket `frames-armyworm-366356442579`.

## 4.3 Architecture: vanilla JS ES modules, no framework

The app is deliberately framework-free. It is plain ES modules (`<script type="module">` with import/export), no bundler, no build step. Any static HTTP server can serve it, which is what makes the S3 + CloudFront hosting trivially reproducible. This is a design decision, not a shortcut: the whole deploy is one `aws s3 sync`.

Rendering model: each tab render function writes an HTML string into `#page-content` via `innerHTML`. Interactive elements use inline handlers (`onclick="switchTab('live')"`). Inline handlers resolve names on `window`, and module scope is not window scope, so `main.js` ends with one `Object.assign(window, {...})` block that exposes exactly the inline-handler surface. Everything else stays module-private. If a new inline onclick is added anywhere, its function must be added to that block too, or the click throws "X is not defined". This is the module split's contract; do not regress to flat `<script src>` tags, which would put everything back in global scope.

Two CDN globals load before the module graph: `window.Chart` (chart.js 4.4.1) and `window.Hls` (hls.js 1.4.12), both from jsdelivr in `index.html`.

Boot sequence (`main.js` `init()`, verified in code):
1. `renderTabs()` paints the tab bar.
2. `imageCache.open()` opens IndexedDB early so the first gallery render is instant.
3. `initAuthUi(startApp)` wires the login form.
4. If `isLoggedIn()`, `startApp()` runs immediately; otherwise `requireLogin()` opens the login overlay.
5. `startApp()` fetches `/settings`, `/model/status`, and `/stream/status`, then `switchTab(state.tab)` (default tab: `live`).

`switchTab(id)` also does teardown: destroys the HLS player when leaving Live, destroys Chart.js instances when leaving Analytics, and stops model-status polling when leaving Settings.

NOTE: doc lag. The module list in `docs/dashboard.md` names a `js/cameras.js`. No such file exists. Camera cards live in `js/settings.js` (Settings → Cameras sub-tab) and the live-view camera cards live in `js/live.js`.

## 4.4 config.js

One exported object, `CONFIG`. This is the only file to touch when moving hosts or accounts. The repository copy still carries the development-era values; the deployer templates the production values in at deploy time (explained below the table). Verified in code:

| Key | Value | Meaning |
|---|---|---|
| `HTTP_API` | `https://zwpcbivmsj.execute-api.us-east-1.amazonaws.com` | API Gateway HTTP API base URL. |
| `COGNITO_REGION` | `us-east-1` | Region of the user pool. |
| `COGNITO_CLIENT_ID` | `4husu6afr835e235eu9dqp8av6` | App client id. This is a public identifier, not a secret. |
| `SG_OFFSET_HOURS` | `8` | Singapore is UTC+8. Backend stores UTC; all UI shows SGT. |
| `MODEL_POLL_INTERVAL_MS` | `5000` | Custom-model status poll period while Settings → Cameras is open. |
| `CACHE_DB` / `CACHE_STORE` | `pest-monitor-cache` / `images` | IndexedDB database and object store names. |
| `CACHE_MAX_BYTES` | 400 MB | Image cache ceiling (bumped from 200 MB in v5.3 when the gallery fetch went to 500 records). |

A `WS_URL` used to exist; WebSocket was removed in v3.7 and the dashboard polls instead.

To reproduce on a new AWS account: change `HTTP_API`, `COGNITO_REGION`, and `COGNITO_CLIENT_ID` here and nothing else in the frontend.

The values in the table above are the development account's; the repository copy of `config.js` keeps them. On the production account the ARGUS deployer templates the file at deploy time: `deployer/deploy.py` (stage "writeback") regex-replaces `HTTP_API`, `COGNITO_REGION`, and `COGNITO_CLIENT_ID` in memory and uploads the rewritten copy to `argus-dashboard-506868652945`, leaving the repo file untouched. Verified through the CDN 2026-08-11: the deployed production `config.js` carries `HTTP_API: 'https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com'` and `COGNITO_CLIENT_ID: '6vebotf45bp8u46cnraddiaplv'`. Cosmetic stale comment: the templating rewrites only the values, so the comment above those lines still names the old pool id `us-east-1_ea0aJdusl`. The values are correct; do not let the comment mislead you about which pool the production deployment uses (`us-east-1_9selFDHpc`).

## 4.5 auth.js — Cognito sign-in

Purpose: sign-in against the Cognito IDP endpoint with raw `fetch`, no AWS SDK. This fits the bundler-free setup. Tokens live in `localStorage` under the key `pest-monitor-auth`. Verified in code end to end.

Key functions:

- `idpCall(target, body)`: POST to `https://cognito-idp.us-east-1.amazonaws.com/` with header `X-Amz-Target: AWSCognitoIdentityProviderService.<target>` and content type `application/x-amz-json-1.1`. Non-OK responses throw an `Error` carrying the Cognito `__type` as `err.code`.
- `login(email, password)`: `InitiateAuth` with `AuthFlow: USER_PASSWORD_AUTH`. If Cognito returns any `ChallengeName`, the function throws "Account needs a password reset (`<ChallengeName>`) — ask the admin.", with the actual challenge name interpolated. This is deliberate: accounts are admin-created with permanent passwords (`admin-set-user-password --permanent`), so the login UI intentionally does not implement the FORCE_CHANGE_PASSWORD challenge flow.
- `storeAuthResult(r, email)`: saves `{idToken, refreshToken, expiresAt, email}`. Cognito omits `RefreshToken` on refresh responses, so the previous refresh token is kept.
- `refreshSession()`: `InitiateAuth` with `AuthFlow: REFRESH_TOKEN_AUTH`. Returns false on any failure.
- `isLoggedIn()`: true if a refresh token exists, or an unexpired ID token exists.
- `getIdToken()`: returns a valid ID token or null. It refreshes proactively when the token is within 5 minutes of expiry. Every API call goes through this, so refresh is automatic and invisible.
- `logout()`: clears the stored tokens (removes only the `pest-monitor-auth` key; the IndexedDB image cache survives) and reopens the login overlay. Wired to the topbar "Sign out" button as `logoutClick`.
- `requireLogin()` / `hideLogin()`: toggle the `#login-screen` overlay. `requireLogin` is also called by `api.js` on any 401, so an expired or revoked session re-gates the app mid-use.
- `initAuthUi(onLogin)`: wires the login form submit. On success it blanks the password field, hides the overlay, and calls `onLogin` (which is `startApp`). Cognito error messages show inline in `#login-error`.

Token facts, production pool `us-east-1_9selFDHpc` (created by the ARGUS deployer; pool name `pest-dashboard-users`, the same name as the retired development pool it reproduces): 12-hour ID tokens, 30-day refresh tokens, sign-in by email, self-signup disabled, admin-create-only, app client has no secret. Verified in `deployer/deploy.py` stage 7: `IdTokenValidity=12` hours, `RefreshTokenValidity=30` days, `AllowAdminCreateUserOnly`, `GenerateSecret=False`, auth flows `USER_PASSWORD_AUTH` + refresh. Passwords are set permanent via `admin-set-user-password --permanent`; the first production sign-in user was created and confirmed 2026-08-11.

Security note: no password, secret key, or token appears anywhere in the repository for this flow. The client id and pool id are public identifiers by design.

## 4.6 api.js — the API client

Every backend call goes through `api._fetch(path, opts)`. Verified in code:

1. Builds headers with `Content-Type: application/json`.
2. `await getIdToken()`; if a token comes back, adds `Authorization: Bearer <IdToken>`.
3. Fetches `CONFIG.HTTP_API + path`. Parses the body as JSON, falling back to `{raw: text}`.
4. If status is 401: calls `requireLogin()` and throws "Signed out — please sign in again".
5. Any other non-OK status throws `data.error` or `HTTP <status>`.

The full endpoint surface (all verified in code):

| Method call | HTTP request |
|---|---|
| `getSettings()` | `GET /settings` |
| `postSettings(body)` | `POST /settings` |
| `getHistory(filters)` | `GET /history?...` (filters become query params; empty values dropped) |
| `getPresignedUrl(key)` | `GET /presigned-url?key=...&method=GET` |
| `getPresignedUploadUrl(key)` | `GET /presigned-url?key=...&method=PUT` (backend actually returns a presigned POST; see 4.11 Test upload) |
| `getModelStatus()` | `GET /model/status` |
| `startModel(camera_id)` / `stopModel(camera_id)` | `POST /model/start` / `POST /model/stop` |
| `getCost(days)` | `GET /cost?days=N` (dormant Costs tab) |
| `getIdentities()` / `addIdentity(email)` / `removeIdentity(email)` | `GET` / `POST` / `DELETE /identities` |
| `getSchedule()` / `postSchedule(body)` / `deleteSchedule(camera)` | `GET` / `POST` / `DELETE /schedule` |
| `getScheduleLogs(limit)` | `GET /schedule-logs?limit=N` |
| `getVideoPlayback(stream)` | `GET /video-playback?stream=<kvs_stream_name>` |
| `getStreamStatus(camera)` | `GET /stream/status` (the one route with no JWT authorizer; the Jetson Orin and mini-PC `kvs_controller.py` poll it unauthenticated) |
| `startStream(camera_id)` / `stopStream(camera_id)` | `POST /stream/start` / `POST /stream/stop` |
| `verifyDetection(image_id, bbox_index, verdict)` | `POST /detection/verify` with `verdict` of `"TP"`, `"FP"`, or `null` (null clears that box's verdict) |
| `deleteDetection(image_id)` | `DELETE /detection?image_id=...` (route `glwyqo0`, JWT-gated, stays gated even during the emergency rollback) |

## 4.7 state.js — shared state and the IndexedDB image cache

`state` is one exported mutable object; modules import and mutate it directly. Notable fields (all verified in code): `tab`, `settingsSub`, `cameras`, `schedules`, `cameraOps`, `modelStatuses`, `streamStatuses`, `galleryFilters` (default limit 500), `galleryItems`, `galleryPage`, `notifs` (capped at 50), `hls`, `liveCam`, chart handles, `modelPollTimer`, `db`, `uploadFile`, `uploadCam`, and `verifyMap` (`{image_id: {bbox_index: 'TP'|'FP'}}`, the per-box verdict store).

`imageCache` is a persistent thumbnail cache in IndexedDB so a refresh does not re-fetch every presigned URL. Key = the S3 object key. Value = `{key, blob, mime, fetched_at, size}`. Verified per function:

- `open()`: opens database `pest-monitor-cache` version 1. On first open (`onupgradeneeded`) it creates the `images` object store keyed by `key` with a `fetched_at` index. The handle is memoized on `state.db`, so repeat calls cost nothing.
- `get(key)` / `put(key, blob)` / `remove(key)` / `clear()`: one-transaction wrappers. Each wraps the IndexedDB work in try/catch and resolves `null` or `false` on failure instead of rejecting, so a broken cache (private browsing, quota exhaustion) can never take the UI down.
- `fetchAndCache(key, presignedUrl)`: cache-first. On a hit it returns an object URL for the stored blob. On a miss it fetches the presigned URL, stores the blob, and returns an object URL for it. On any fetch failure it returns the presigned URL itself, so the image still displays, just uncached.
- `evictIfFull()`: reads all entries and sums `size`. Under `CACHE_MAX_BYTES` (400 MB) it returns. Over the cap it sorts by `fetched_at` ascending (LRU) and deletes oldest-first until the freed bytes exceed `total - 0.7 * cap`, which lands usage at about 70% of the cap. It runs once per gallery page, after that page's thumbnails finish loading (end of `loadThumbsForIndices`), not on every put.
- `size()`: returns `{count, bytes}`; feeds the summary bar's cache line.

All cache operations are best-effort; every failure path falls back to direct presigned URLs.

## 4.8 utils.js — helpers

All verified in code:

- `toast(msg, kind, ms)` and `refreshToastFan()`: see 4.14.
- Time helpers: `_parseUtc` treats DynamoDB's `"YYYY-MM-DD HH:MM:SS"` strings as UTC (appends `Z`). `fmtTime`, `fmtTimeShort`, `fmtDate` all render with `timeZone: 'Asia/Singapore'`. `todayYMD` and `daysAgoYMD` compute Singapore dates for filter defaults.
- `escapeHtml(s)`: used on every interpolated string in every module's HTML templates.
- `convertToJpg(file, quality=0.92)`: canvas-based conversion of any browser-decodable image (PNG, HEIC, WebP, BMP, GIF) to JPG, transparency flattened onto white, filename rewritten to `.jpg`. Pass-through for JPEGs. Used by Test upload.
- Camera classifiers: `isManualCamera` (`manual_upload`), `isTestCamera` (`person_cam`), `needsKvs`, `needsSchedule` (only custom-model cameras; their Rekognition Custom Labels endpoints cost about $4/hour), `isWormCam` (id starts with `worm` or `armyworm`), `camAvatarClass`, `camInitial`.
- `statusBadge(status)`: maps model states to badges. `RUNNING` shows "Live", `STOPPED` shows "Standby", `TRAINING_COMPLETED` shows "Ready", `NOT_CONFIGURED` shows "Free mode", `NOT_FOUND` shows "Setup incomplete".
- `camDisplayName(id)`: the static map that hides internal camera ids from users. Verified mapping: `worm_cam` → "Worm Cam", `moth_cam` → "Moth Cam", `armyworm_go2_a8mini` → "Worm Cam" (pre-migration id), `moth_cam_01` → "Moth Cam" (pre-migration id), `wilbur-fyp-project` → "Moth Cam" (Wilbur-era legacy records), `manual_upload` → "Test upload". Unknown ids fall back to the raw id. The map is static on purpose: it must cover legacy record ids that no longer exist in the cameras table (the camera_id migration rewrote camera rows but left old S3 keys and legacy rows in place).
- `cleanLabel(label)`: strips a trailing parenthetical from camera labels for display only.
- `chartOpts(xLabel, yLabel)`: the shared Chart.js options factory used by analytics and costs.

## 4.9 Gallery (gallery.js + modal.js)

### Page layout and filters

`renderGalleryPage()` writes: summary bar, card grid, pagination bar, then the filter bar below the grid (v5.1 layout decision: images first). Filters: date range preset (Today / 3 / 7 / 14 / 30 days / All time), pest type (from each camera's `target_label`), free-text zone, camera, and detected-only/clear-only. `applyGalleryFilters()` converts the range preset into `date_from`/`date_to` (Singapore dates) and reloads. All filtering is server-side via `GET /history`; the fetch limit is 500 records.

### Load path

`loadGallery()` fetches, stores `resp.items` in `state.galleryItems`, resets to page 1, then hydrates `state.verifyMap` from every fetched record before drawing. That last step is the fix for the historical X-dismiss bug: v3_9 only hydrated verdicts on the analytics/settings paths, so a fresh load straight into Gallery had an empty verifyMap and every dismissed false positive reappeared. Verified present in code (`for (const it of state.galleryItems) hydrateVerifyMap(it);`).

`updateGallerySummary(resp)` builds the summary bar. Per-pest chips count verifiable boxes, not photos: `getVerifiableBoxes` per record, summed by `target_label` (the v3.6.2 counting rule). The record count carries a "most recent 500 — narrow the date range for older" hint whenever the fetch limit was hit. The cache line shows `imageCache.size()` as count + MB with a Clear button; `clearImageCache()` asks for a native confirm, clears the store, and reloads the gallery. After a delete, the bar is recomputed locally from the module-level `lastScanned` counter instead of re-fetching.

### Pagination (v5.3)

Client-side, 50 cards per page (`PAGE_SIZE = 50`). The backend returns up to 500 most-recent matching records already sorted; `renderGalleryGrid()` slices `state.galleryItems` by `state.galleryPage`. Cards keep their global index (`start + i`) so `openImg`, `deleteGalleryItem`, and thumbnail loaders always resolve against `state.galleryItems` no matter which page shows. `renderPagination()` builds prev/next, windowed page numbers (first, last, current's neighbors, with ellipsis gaps), and a jump-to-page input. `gotoGalleryPage(p)` clamps, re-renders, and scrolls the grid into view. Records older than the most-recent 500 are reached by narrowing the date filter (server-side).

### Thumbnail loading

`loadThumbsForIndices(indices)` loads only the current page's thumbnails, 4 at a time (a worker pool over a shared cursor). Per record, key selection: if the record has drawable boxes, load the original frame (`original_image_key || image_id`) and draw boxes client-side; legacy records without `bboxes` fall back to the baked `processed_image_key`. `loadOneThumb` order of attempts, verified in code:

1. IndexedDB cache hit: instant object URL.
2. Cache miss: `GET /presigned-url`, then fetch the blob once over CORS, put it in the cache, and display from that same blob. A single CORS fetch (not `<img src>` plus a second fetch) avoids the browser caching an opaque no-cors response that the cache fetch then cannot read.
3. 403/404 on the object: switch to the processed-image fallback with box drawing disabled (boxes are already baked into that image; drawing again would double them).
4. Other failures: up to 3 attempts with escalating 800 ms backoff (800 ms x attempt number, so 800 ms then 1600 ms), then a "Retry" button on the card (`showRetryButton` / `retryThumb`).

Stale-closure guard (v4.2, verified): a delete re-renders the grid and shifts indices, so an in-flight retry's index can point at a different record. `loadOneThumb` proceeds only if the record at its index still owns the key it was started for.

### Gallery cards and badges

`galleryCard(it, idx)` shows the thumb, a bbox overlay div, a pest badge ("Armyworm-larva · 3" style, count appended when more than one box), zone + time footer, the camera display name (via `camDisplayName`), and a Delete button. Cards show no confidence percentage: every surviving box already passed the processor's server-side gate (v4.5 decision).

Verification badges: `aggregateVerdict(it)` rolls per-box verdicts into one corner badge. States: `n/total` (partially reviewed), `✓` (all confirmed), `✗` (all false positives), `n✓m✗` (mixed). Unreviewed records show no badge.

LLM verifier display (v5.3, verified in code): the detection modal's meta pane has a "Verifier" row showing which multimodal model judged the boxes on that frame. `modal.js` `llmVerifierName(rec)` reads the record field `llm_verify_model` (written by `pest-detection-processor` on every row so a model A/B stays readable later), then strips the region prefix (`us.` / `eu.` / `apac.` / `global.`), the trailing `-vN:M` version suffix, and the vendor prefix (`anthropic.` / `amazon.` / `meta.` / `mistral.` / `google.` / `qwen.`) for display. Records without the field (legacy, or cameras with LLM verify off) show a dash. That row is the whole LLM display surface: there are no per-box LLM verdict badges in the gallery, overlay, or review list (the v4.5 UI simplification removed them; a box present in the record is already a confirmed detection). No field named `verified_by_llm` is read anywhere in the frontend. The backend does write more: `pest-detection-processor` adds per-box `verified_by_llm` (`"true"`/`"false"`) and `llm_reason` fields on boxes the LLM verifier judged, alongside the per-record `llm_verify_model`. The UI ignores both per-box fields; only `llm_verify_model` is read.

### The detection modal (modal.js)

`openImg(idx)` opens `#image-modal` with two panes. The meta pane shows Camera (display name), Zone, Target, Detected, Model (model_type), Verifier (see above), Labels count, Time, and Model version (a testing aid, not a final feature: the version segment regex-parsed out of the stored `model_arn`; legacy records show a dash). Below that: the review block and a Delete button.

Image pane: same cache-first, blob-once load path as thumbnails, then an overlay div (`.bbox-overlay`) tagged with the record's `image_id`. `positionOverlay()` sizes the overlay to exactly the rendered image rect using `offsetLeft/Top/Width/Height` (these ignore CSS transforms, so it is the untransformed base box); a window `resize` listener re-runs it, and `closeImageModal` removes that listener together with the zoom listeners through the stored `state._imgZoomCleanup`. If the original frame is missing, an error handler swaps to the processed image and blanks the overlay (`usingFallback`), since that image has baked boxes; the handler also fires immediately when a cached `<img>` has already failed (`imgEl.complete && imgEl.naturalWidth === 0`). A zoom hint ("scroll · pinch · dbl-click to zoom") shows for 2.5 s.

Review block (`renderReviewBlock`): one row per drawable box, sorted by confidence descending, so row order matches the visual prominence of the boxes and the index base matches `verifyMap`. Each row shows `#n`, species name, confidence to one decimal, and one `✗` flag button; a "flagged" counter sits in the block header. The counting model is opt-out: a detection counts unless a human marks it FP. CAG staff only flag false positives; they never have to confirm true ones. `refreshOpenModalReview(image_id)` re-renders just the review block and the overlay on a verdict change, without re-fetching the image.

Close paths: the ✕ button, clicking the backdrop, or Escape. All route through `closeImageModal()`, which also detaches the window-level zoom listeners.

### Two-step delete flow (v4.2)

Step one is the Delete button (on the card, with `event.stopPropagation()`, or in the modal). Step two is a native `confirm()` dialog that states exactly what happens: removes the DynamoDB record, removes the stored S3 image, cannot be undone. Only then does `deleteGalleryItem(idx)` call `api.deleteDetection(it.image_id)` (`DELETE /detection?image_id=`).

On success, the frontend evicts every cached image variant from IndexedDB, drops every card sharing that `image_id` (the API deletes all rows for the id, and duplicates share the PK), re-renders the grid, and recomputes the summary bar locally from `lastScanned`. The modal closes only if the delete succeeded. Server-side safety (from `docs/aws.md` and `docs/dashboard.md`, E2E-tested 2026-07-07): 404 with nothing touched if no record exists; frame deletes gated to the `frames/` prefix in code and IAM; any S3 failure returns 500 with rows kept, so the operation is retry-safe.

## 4.10 Bounding boxes and verification (bbox.js)

NOTE: doc lag. `docs/dashboard.md` calls this "drawing boxes on a canvas overlay". The implementation is not a `<canvas>`; it is absolutely-positioned `<div>` elements inside an overlay container, with box geometry expressed as percentages. The only real canvases are Chart.js charts and the `convertToJpg` helper. The behavior described in the docs is otherwise accurate.

### Box extraction (two sources, verified)

- `getVerifiableBoxes(it)`: parses `it.labels` (JSON), keeps entries whose name equals the record's `target_label`, sorts by confidence descending. This is the counting basis for the summary chips, analytics, and verifyMap indices (bbox_0 = highest confidence).
- `getDrawableBoxes(it)`: parses `it.bboxes` (the structured coordinates the processor writes: `left`, `top`, `width`, `height`, all normalized 0-1), drops zero-area boxes, sorts by confidence descending. This is the geometry source for the modal overlay, card overlays, and the review block.

Standing rule (introduced in v4.5, still true under the v6.3 processor): neither function re-filters by any camera threshold. The processor's server-side gate already decided which boxes survive before the record was written — in the current pipeline, Rekognition proposes candidates at a low floor (`min_confidence` 10), the LLM verifier judges every box, and only boxes that pass verification and the post-verify floor (`post_verify_floor`; live production value 33, verified against the production camera row 2026-08-14) are written. A box present in the record is a confirmed detection. Re-filtering client-side would wrongly hide a legitimate detection the gate confirmed, and raising the threshold later would retroactively hide old confirmed records.

### Coordinate scaling

Modal overlay (`renderOverlayBoxes` + `modal.js` positioning): the overlay div is sized to the rendered image's untransformed rect. Each box is a child div at `left: left*100%; top: top*100%; width: width*100%; height: height*100%`. Because the geometry is percentage-based, boxes stay correct at any modal size. During zoom/pan, `attachImageZoom` applies the identical CSS transform string to the image and the overlay, so boxes track the image exactly. Since v5.4 it also writes the live scale onto the overlay as the custom property `--z` (`overlay.style.setProperty('--z', scale)`), which is what keeps the box chrome from growing with the zoom — see below.

Card overlay (`paintCardBoxes`): the thumb uses `object-fit: cover` on a 4:3 box, so the frame is scaled to fill and center-cropped. The algorithm reconstructs that cover rect: `scale = max(thumbW/naturalW, thumbH/naturalH)`, display size `dw = naturalW*scale`, `dh = naturalH*scale`, overlay offset `((thumbW-dw)/2, (thumbH-dh)/2)`. The overlay may overflow the thumb; the thumb's `overflow: hidden` clips it. Boxes are again percentage-positioned children, with no labels on cards. Boxes flagged FP are skipped so the card matches the modal.

Each modal overlay box carries a label with the species name and the confidence rounded to a whole percent, plus a `✕` button ("Flag false positive — removes this box"). NOTE: code-comment lag. The v4.5 comment above `renderOverlayBoxes` says "plain box + species name only — no confidence"; the rendered template actually includes `${b.confVal.toFixed(0)}%`. The rendered output is the ground truth here.

### Verification logic

- `verifyClick(image_id, bbox_index, verdict)`: toggle semantics (clicking the active verdict clears it, sending `null`). Optimistic update of `state.verifyMap`, immediate re-render of grid and modal review block, then `POST /detection/verify`. On backend failure the previous verdict state is restored and an error toast shows. Verified in code.
- `hydrateVerifyMap(it)`: reads the record's `verifications` map (object or stringified JSON) into `verifyMap`. Legacy records with a per-image `verified` boolean are migrated: true projects TP onto every box, false projects FP. Local in-memory edits always win; hydrate never overwrites an existing entry, so unsaved clicks are not clobbered by a refetch.
- `getCountedBoxes(it)`: the opt-out counting rule used by every analytics widget: a box counts unless flagged FP. TP and unreviewed both count.
- `redrawModalOverlay(image_id)`: swaps only the overlay's innerHTML, leaving the image element and its zoom transform untouched.

### Image zoom (attachImageZoom)

Scale range 1x to 6x. Wheel zooms toward the cursor (factors 0.85 out, 1.18 in); `zoomToward` keeps the screen point under the cursor fixed by adjusting the translation by the scale ratio. Double-click toggles 1x and 2.5x. Drag pans when zoomed, clamped to image bounds (`max offset = baseSize*(scale-1)/2`). Touch: two-finger pinch toward the pinch center, one-finger pan. Returns a cleanup function that removes the window-level mousemove/mouseup listeners; element listeners die with the element.

**Zoom-aware box chrome (v5.4, 2026-08-15).** The overlay carries the same CSS transform as the image, so before this change every decoration scaled with the zoom: a 4 px border rendered as 24 px at 6x, and the per-box flag button grew six-fold. On a small larva the chrome covered the animal the operator had zoomed in to inspect. The fix keeps the geometry in percentages, as before, but divides every decoration by the published scale. In `styles.css`:

```css
.bbox-box   { border: calc(2.4px / var(--z)) solid rgba(255, 60, 60, 0.95); }
.bbox-label { font-size: calc(15px / var(--z)); padding: calc(3px / var(--z)) calc(7px / var(--z)); }
.bbox-x     { width: calc(20px / var(--z)); height: calc(20px / var(--z));
              top: calc(-11px / var(--z)); right: calc(-11px / var(--z)); }
```

`.bbox-overlay` declares `--z: 1` as the fallback, so an overlay that is never zoomed still renders correctly. The result is that the outline and the flag button hold a constant on-screen size at any zoom while the target keeps growing. The base border was also thinned from 4 px to 2.4 px in the same change.

## 4.11 Cameras and settings (settings.js)

`renderSettingsPage()` loads settings, model statuses, schedules, and the last 500 history records (to compute per-camera "last detection" and "today count", counted in boxes, not photos), then renders four sub-tabs: Cameras, Test upload, Schedules, Alerts. The former Global sub-tab was removed; a remembered `state.settingsSub` of `'global'` redirects to Alerts (verified in code). `saveGlobal` (email alerts on/off plus primary recipient) is still exported but no rendered markup produces its input elements any more; it is vestigial.

### Cameras sub-tab

One card per deployed camera (`manual_upload` is excluded; it has its own Test upload sub-tab). Card order: custom-model worm cameras first, other custom cameras, then free-mode cameras. Each card (all verified in code):

- Header: avatar, display label, status badge and Start/Stop buttons (`cameraStatusActionsHtml`). Start/Stop only render for custom-model cameras whose `custom_model_arn` is set and not a `REPLACE_` placeholder. Buttons disable during STARTING/STOPPING.
- Status hint line (`cameraStatusHintHtml`): "Starting up · ready in ~5 min", "Detection live", "Idle", etc.
- Operational tier: Last detection time, Today's box count, and an Auto-schedule quick toggle. The toggle locks during model state transitions and is only enabled for cameras where `needsSchedule` is true (custom model, since endpoints cost about $4/hour).
- Detection settings (collapsible):
  - AI filter threshold: a number input (0-100, step 1) bound to the camera field `post_verify_floor` — the confidence floor applied after the LLM verification step decides which boxes are written. It auto-saves through `debouncedSaveCamera` (500 ms debounce) into `POST /settings`; after saving, the code re-fetches settings and verifies each field actually changed (the verify loop compares `post_verify_floor` numerically), showing a mismatch toast if not. The input's markup fallback is 33 (`cam.post_verify_floor ?? 33`). Live production value: **33**. History: 49 was the value decided and flipped live 2026-08-10; the retrained production model scores the same images lower, so the floor was refitted per-build to 33 on 2026-08-13 (camera row + Lambda env together). The floor is a per-build fit, not a universal constant. The low Rekognition candidate floor (`min_confidence`, 10 in production) is no longer exposed in this UI; the user-facing threshold is the post-verify floor.
  - AI model: a per-camera selector bound to `llm_model_id` — Claude Sonnet 4.6 ($20 / 1M tokens) or Claude Haiku 4.5 ($5 / 1M tokens) — choosing which model verifies every detection on this camera (a v6.3 processor feature). Rendered only for custom-model cameras; saved through the same debounced path.
  - Zoom scan toggle: only rendered for custom-model cameras. The UI label is "Zoom scan — Split & zoom in to catch small pests"; the persisted field is `tiling_enabled` (live production value: true, alongside `llm_verify_enabled` true on the same row). `toggleTiling(id, enabled)` saves it via `POST /settings` and updates local state; on failure the checkbox reverts. Cloud-side, the flag makes `pest-detection-processor` tile that camera's frames (grid split + per-tile upscale + per-tile detect + NMS). It is the same camera, just a switch, not a separate camera. Verified in code.

The write path behind those inputs (`saveCameraSettings(id)`, verified in code): it harvests whichever inputs exist in the DOM by id — `cam-{id}-label`, `-target`, `-conf` (parsed to int as `post_verify_floor`), `-llm`, `-waypoint`, `-kvs`. An absent input leaves that field out of the POST entirely, so the one function serves every card variant. It then sends `POST /settings` with `{camera_id, fields}`, re-fetches `GET /settings`, and compares each sent field against the fresh copy: numeric compare for `min_confidence` / `post_verify_floor`, string compare for the rest; empty and null sent values are skipped. On a full match the save indicator flashes "ok" for 2 s and a toast counts the saved fields. On a mismatch the indicator shows the error state and a toast names the mismatched fields. Any thrown error (network, 401) lands in the same error state. The debounce wrapper keeps one timer per camera id (`_saveDebounceTimers`), so edits on two cameras cannot cancel each other's saves.

Model start/stop: `modelStart(id)` first shows a confirm dialog stating the roughly $4/hour cost and the 5-10 minute STARTING window, then `POST /model/start`, optimistic status patch, and polling resumes. `modelStop(id)` mirrors it.

Model status polling: `startModelPolling()` runs only while Settings → Cameras is open and at least one camera has a real custom model ARN. Every 5 s it fetches `/model/status`; on change it calls `patchCameraStatusUI()`, a surgical DOM update that only touches the status badge, buttons, hint, and schedule-toggle lock. User-edited inputs are never re-rendered mid-edit. Transition toasts fire on RUNNING, STOPPED, and FAILED. `stopModelPolling()` is called on any tab or sub-tab change.

### Test upload sub-tab

This is the panel that demonstrates live detection at the final presentation (there are no worms at Jewel to stage). Drag-drop or browse an image, pick which camera config (and so which detection model) to run it with, pick which AI verification model judges the boxes for this run (Claude Sonnet 4.6 or Haiku 4.5, with per-model cost shown), then submit. `uploadSubMarkup` self-heals a stale remembered camera: if `state.uploadCam` no longer exists (for example after a camera_id migration), it falls back to the first deployed camera. The pipeline, verified in code:

1. Non-JPEG files convert to JPG in the browser (`convertToJpg`). 20 MB pre-conversion cap; non-image MIME types rejected.
2. The Run button gates on model readiness (`uploadModelReady`): custom-model cameras require RUNNING status; free-detection cameras are always ready. The hint line re-fetches status so the gate is accurate even if the Cameras tab was never opened this session.
3. Key construction: `frames/{camera_id}/{waypoint}/{timestamp}_{safeName}`, where the waypoint segment is `manual_test__conf10__llm-{model}`. The extension comes from the (possibly converted) filename: `convertToJpg` rewrites non-JPEG names to `.jpg`, while pass-through JPEGs keep their original extension. `conf10` pins the Rekognition candidate floor at 10 (the validated production candidate floor) for this run; `__llm-sonnet46` or `__llm-haiku45` picks the verification model for this run only. The S3-triggered processor Lambda parses those suffixes and applies the overrides for this one detection run, with no DynamoDB mutation. The earlier user-facing min-confidence override input is gone; the per-run choice the operator makes now is the verification model. NOTE: code-comment lag. The comment in `settings.js` calls that Lambda `image-detection-handler`; the deployed function is `pest-detection-processor` (`docs/aws.md`).
4. `GET /presigned-url?method=PUT` returns a presigned POST (`{url, fields, method:"POST"}`); the file uploads direct to S3 via FormData with the file as the last field. A legacy presigned PUT path is kept as fallback.
5. `waitForDetection(key)` polls `GET /history?limit=10` every 3 s (first poll after 2 s) for up to 60 s until a record with that exact `image_id` appears, then rejects with a timeout error pointing at CloudWatch. On a match, `renderUploadResult` draws the result inline: the original frame with drawn boxes (minus any boxes already flagged FP in `verifyMap`; the baked processed image for legacy records without box geometry), a detected/clear badge, the raw confidence, and the returned label count.

Gotcha (hit and fixed 2026-07-07, on the development stack): the browser posts the file straight to S3, so the frames bucket must list the dashboard origin in its CORS AllowedOrigins. On production the deployer's writeback stage already set the rule on `argus-frames-506868652945` to the website and CloudFront origins. "Failed to fetch" on Test upload from any new origin (for example a localhost dev server) means: add that origin. Console: S3 → Buckets → `argus-frames-506868652945` → Permissions → Cross-origin resource sharing (CORS) → Edit. CLI: `aws s3api put-bucket-cors --bucket argus-frames-506868652945 --cors-configuration file://`**cors.json** ` --profile prod`.

### Schedules sub-tab

One schedule card per custom-model camera: enable toggle, start time, active-day chips with "Every day" / "Weekdays" presets, Save and Delete. Data-source nuance, verified in code: the schedule card reads `cam.schedule` (the copy embedded in the camera row by `GET /settings`), while the quick toggle on the camera card reads `state.schedules` from `GET /schedule`; both write through `POST /schedule`. There is no stop time any more: the card's helper text states the contract — the schedule starts the model, one detection round runs, and the model is shut down automatically. The auto-close is not the schedule's doing: the `pest-model-watchdog` Lambda (the v6.2 per-camera build, which reads each camera's `max_runtime_min` — 45 for `worm_cam` — instead of one global cap) runs on a 15-minute EventBridge schedule and stops any endpoint past its budget. The ARGUS deployer installed exactly this build on production. `saveSchedule` posts `{camera_id, enabled, start_time, days}` to `POST /schedule`. Below the cards, an execution log table shows the last 30 automated start/stop events from `GET /schedule-logs`. The quick toggle (`toggleScheduleQuick`) reuses an existing schedule's values or defaults to 05:40 every day (matching the production camera's schedule, stored at 05:40 daily but currently disabled); on a failed POST it flips the checkbox back.

### Alerts sub-tab

Email subscriber management over SES identities: list with verification status badges, Add (sends the SES verification email), Resend for pending addresses (same idempotent backend call), Remove (blocked for the primary address), and a Refresh status button. An info panel explains the SES sandbox verification flow: production SES access removes the recipient-verification requirement. SES on the production account `506868652945` is in sandbox mode, so every recipient must be individually verified. The primary alert address was verified on 2026-08-11, so alerts reach that one address; any further subscriber must verify through this same sub-tab. (The retired development account was sandboxed too — not a migration regression.)

### Removed: the Global sub-tab

Earlier builds had a fifth Global sub-tab (email alerts on/off, primary recipient, auto-capture, capture interval). It was removed; its `state.settingsSub` value now redirects to Alerts. The exported `saveGlobal` function survives in `settings.js` but nothing renders its inputs, so it is dead UI code. Global settings still exist backend-side (`POST /settings` with a `global` body); they are just no longer editable from this dashboard build.

## 4.12 Live view (live.js)

`renderLivePage()` lists every camera that has a `kvs_stream_name`, defaulting the selection to the worm camera (its KVS stream `armyworm-cam-stream` is the primary one; `moth-cam-stream` is parked — stream names are unchanged across the account migration). The stream-status fetch inside it is best-effort: a failure only logs a warning and the page still renders. `selectLiveCam(id, ev)` ignores clicks that originate on the KVS toggle (`ev.target.closest('.toggle')`), sets `state.liveCam`, and re-renders the page. Each camera card has a KVS push toggle: `toggleStream` calls `POST /stream/start` or `/stream/stop`, which sets the desired state; the GStreamer producer on the device (Jetson Orin or mini PC) picks it up on its next poll of `GET /stream/status`. On success `toggleStream` patches `state.streamStatuses` and re-renders; on failure it toasts and re-renders too, which redraws the checkbox from the last known state — the failed flip reverts itself. The status route is the only unauthenticated route in the API, precisely so the devices need no tokens.

Playback path, verified in code: `loadStream()` calls `api.getVideoPlayback(kvs_stream_name)` (`GET /video-playback?stream=...`), which returns `{hls_url}`, a Kinesis Video Streams HLS session URL. That route is served by the `kvs-hls-handler` Lambda (see Chapter 2; its existence is confirmed by the deployer audit `deployer/audit/lambda__kvs-hls-handler.json`). `playHls` destroys any prior player, then either attaches hls.js (`new Hls({lowLatencyMode: true})`) or, on Safari-class browsers with native HLS, sets `video.src` directly. Errors render an overlay telling the operator to toggle the stream on and check the producer. NOTE: doc lag. The Lambda list in `docs/aws.md` does not mention `kvs-hls-handler`.

Production-account status: the 2026-08-10 deploy ran without `--live-view`, so the deployer created no KVS stream resources. The production `worm_cam` row does carry `kvs_stream_name` `armyworm-cam-stream` (verified against the production DynamoDB row 2026-08-14) with `stream_enabled` false, so the Live tab lists the camera with its toggle off. Live playback stays dark until the stream resource exists on the production account and the device-side producers run with production credentials: the repo mirrors of `robot/kvs_controller.py` and `minipc/kvs_controller.py` already default to the `vzfl7s6z00` API (repointed 2026-08-13), but the Jetson Orin and mini PC still carry development-account keys, and the on-device sync is pending the hardware's return. Known open item of the migration, not a defect.

## 4.13 Analytics (analytics.js)

All widgets count bounding boxes, not photos (v3.6.2 decision: one photo with 5 worms contributes 5, matching what CAG wants: "where are the pests"). All use `getCountedBoxes`, so the opt-out FP rule applies uniformly. Data source: one `GET /history` fetch for the chosen date range (default last 30 days, limit 500), with `hydrateVerifyMap` run over every record first. Verified in code:

- Stat cards: Today, Range total (with photo count), Flagged FP rate over the last 7 days (denominator is all detections in the window, not just reviewed ones), Peak day, Avg/day.
- By zone: horizontal bar chart of the top 5 waypoints, last 7 days. Note that this widget is scoped to 7 days while the rest of the tab follows the date picker, so on a quiet site it can read "No detections in the last 7 days" beside a non-zero Range total. That is correct behaviour, not a fault.
- Zone heatmap (`renderZoneHeatmap`, added in the v5.1 ARGUS reskin, per `docs/dashboard.md`; the code comment in `analytics.js` labels it v5.0 — same change, inconsistent label): zone x day grid, last 14 days, top 6 zones ranked by total. Cell shade is `rgba(10,132,255, 0.16 + 0.72*(n/max))`; zero cells get a faint neutral. Tooltip per cell: zone, date, count. A legend shows the shade ramp and the peak per-day count. Built as a CSS grid of divs, columns `auto repeat(14, 1fr)`; the wrap scrolls horizontally on narrow screens.
- Daily trend: combined bar + line chart over the selected range (both datasets carry the same daily-bucket numbers; the line is a styled duplicate for readability, not a computed trend).
- Camera health: days since last detection per camera. Green up to 3 days, warning to 7, red "stale" beyond 7, "No data" otherwise. This answers "is the camera alive", not "are there pests".
- By camera: box counts with percentage bars, using `camDisplayName`.

Counting helpers, verified in code: `zoneBuckets7d(items)` keeps only records whose Singapore-converted `detection_time` falls in the last 7 x 24 h and sums `getCountedBoxes` per `waypoint_id`; `dailyBuckets(items)` does the same keyed by Singapore date over the whole fetched range. `renderStats` derives everything from `dailyBuckets`: Today = the bucket for today's Singapore date, Range total = sum of all buckets, Peak day = the max bucket, and Avg/day divides by active days (days with at least one counted box), not calendar days. The FP-rate algorithm: over the last 7 days, denominator = every verifiable box in the window, numerator = every `FP` verdict in `verifyMap` for those records; a window with no detections renders a dash.

FIXED 2026-08-16 (v5.5, deployed and verified live). This section previously carried the defect as open; it is recorded here because the failure mode is worth knowing and because the same shape can recur.

`renderDailyChart` in `web/dashboard_v4/js/analytics.js` read an undeclared variable — `if (sub) sub.textContent = ...` — left dangling when the card's subtitle line was removed in the 2026-08-05 small-caption cleanup. ES modules run in strict mode, so that read threw a `ReferenceError`, and the throw landed in `loadAnalytics`'s catch. The damage was larger than one chart: `loadAnalytics` calls its six render functions in sequence, so **the two calls after the throw, `renderCamHealth` and `renderByCam`, never executed at all**. Stat cards, By zone and the heatmap had already rendered, which is why the page read as "partly empty" rather than broken, and why the fault survived so long. A red toast showed "Analytics: sub is not defined".

The fix reinstates the subtitle element (`<div class="card-sub" id="daily-sub">` in the Daily trend card head) and declares `const sub = document.getElementById('daily-sub')`. Deleting the line would also have worked, but the subtitle carries useful information ("30 days · 38 pests"), so it was restored rather than dropped.

A second defect was fixed in the same pass. The By-zone chart sets `indexAxis: 'y'`, which makes the bar horizontal: the x axis then carries the counts and the y axis carries the zone names. The titles were passed as `chartOpts('Zone', 'Detections')`, exactly reversed, so the rendered chart printed "Zone" beneath a 0-40 count axis. Now `chartOpts('Detections', 'Zone')`.

Testing note. The browser caches these ES modules hard. After deploying, a plain reload kept executing the old `analytics.js` even though CloudFront was already serving the new file. Force a cache refill first, then reload:

```javascript
for (const u of ['/js/analytics.js', '/js/bbox.js', '/js/config.js']) {
    await fetch(u, { cache: 'reload' });
}
```

Chart instances are stored on `state` and destroyed on tab exit and re-render (Chart.js requirement). A tab-switch guard (`if (state.tab !== 'analytics') return`) prevents a slow fetch from painting over another tab.

## 4.14 Notifications and toasts

Topbar notifications (`main.js`): a bell button with a count badge and a dropdown. `addNotif(e)` (defined and exported in `main.js`) is written to push detection events (`kind: 'detected'`) and schedule events (`kind: 'schedule'`) onto `state.notifs`, capped at 50, and re-render the dropdown. In the current code, however, the feed is dormant: `addNotif` has zero callers anywhere in `web/dashboard_v4` and is not exposed on the window bridge, and no polling loop feeds it. Its producer was the WebSocket removed in v3.7 and was never re-wired to polling. The bell, count badge, and dropdown all render, but no event ever populates them. Clicking outside the dropdown closes it; a Clear button empties the list.

Toasts (`utils.js`): `toast(msg, kind, ms)` prepends a toast into `#toast-stack` (newest on top), auto-dismisses after `ms` (default 3000) with a slide-out animation, and supports a manual ✕. `refreshToastFan()` recomputes each toast's `--fan-offset` (cumulative heights + 8 px gaps) so stacked toasts fan out without overlapping on hover. The ✕ handler is inline HTML, which is why `refreshToastFan` must be on `window`.

## 4.15 Dormant module: costs.js

A complete Costs tab (range pills 7 to 365 days, spend hero, month-to-date, per-service breakdown with usage units, per-day table, spend chart) that is not registered in `TABS`. Reason, verified in the code comment: the shared nbk2 IAM user has no Cost Explorer access; `ce:GetCostAndUsage` requires the account root to enable IAM access to billing, which is not available on shared nbk2. To re-enable: add the entry back to `TABS` in `main.js` and grant the API's Lambda role `ce:GetCostAndUsage`. The window bridge already exposes `renderCostsPage` and `changeCostRange`.

## 4.16 Design system: liquid glass (styles.css v5.2)

`styles.css` is layered: the original warm-neutral light theme (design tokens at the top: cream `--bg: #faf8f2`, teal accent, DM Sans + JetBrains Mono) stays untouched, and the "ARGUS THEME (v5.2)" block appended at the end overrides it. Revert = delete that block. Verified in the file (the block starts at line 1402).

Why v5.2 exists: v5.1's base was near-white, so the glass had nothing to transmit and read as flat. v5.2 adds a visible pastel color field behind everything, then makes every surface actually translucent.

The exact tokens (v5.2 `:root` overrides, verified):

- Base: `--bg: #eef1f5`, `--surface: rgba(255,255,255,0.35)`, ink `#1d1d1f`, accent moved to iOS blue `--teal: #0a84ff` (the variable keeps its old name).
- Pastel color field: `body::before` is a fixed, `inset: -12%` layer of five radial gradients (blue 0.32, cyan 0.30, purple 0.22, green 0.18, yellow 0.10) that drifts slowly (`argus-drift`, 48 s alternate) unless the user prefers reduced motion. The login screen has its own four-gradient variant.
- Glass recipe, panels: `--lg-bg: rgba(255,255,255,0.22)` + `--lg-blur: blur(10px) saturate(190%)` applied as `backdrop-filter`, a `1px double rgba(30,40,60,0.10)` border, and `--lg-rims`.
- Rims (the inset shadow stack that simulates glass thickness): two 2 px inner white highlights at opposite corners at full opacity, two softer 8 px white spreads, and a hair-thin dark inner line (`inset 0 0 2px rgba(30,40,60,0.22)`).
- Specular: every panel (`.card`, `.stat-card`, `.filter-bar`, `.modal-content`, `.login-card`, `.image-card`) gets an `::after` overlay: a 45° linear gradient with white at 0.65 alpha at both extremes, transparent through the middle, blurred 3 px. Content sits above it via explicit `z-index: 1` rules.
- Controls variant: `--lg-bg-ctl: rgba(255,255,255,0.30)`, `--lg-blur-ctl: blur(6px) saturate(180%)`, `--lg-rims-ctl` (1.5 px highlights + 1.5 px dark line). Buttons, tabs, chips, badges, and inputs are all glass. Tabs became glass pills (`border-radius: 980px`) and the old active-tab underline is removed. `.btn-primary` is translucent blue (0.55) with a text shadow and a blue glow; inputs are "glass wells" with an inner-shadow stack and a blue focus ring.
- Brand: the nine-line aperture SVG (same mark as the deployer wordmark) inside a circular `.brand-logo` with a blue gradient. Rebranded from "Pest Monitor" to ARGUS 2026-07-10.
- Tuning knobs: adjust `--lg-bg`, `--lg-blur`, `--lg-rims` (panels) and the `-ctl` variants (controls) in `:root`. Nothing else needs touching to retune the glass.

The v5.3 pagination CSS (`.gallery-pagination`, `.pager-*`) is appended after the theme block and is token-based, so it works in both themes.

## 4.17 Operations / reproduction

### Serve locally (development)

Any static server works. From the repository root:

```
python -m http.server 5501 --directory web/dashboard_v4
```

Then open http://localhost:5501/. VS Code Live Server on `web/dashboard_v4/index.html` also works. The app talks to the live API, so a Cognito sign-in is still required.

### Deploy (the runbook, development account)

Console path: S3 → Buckets → `argus-dashboard-506868652945` → Upload (drag the contents of `web/dashboard_v4/`); then verify the bucket's Properties → Static website hosting shows `index.html` as both index and error document. But the CLI is the intended path:

```
aws s3 sync web/dashboard_v4 s3://argus-dashboard-506868652945 --delete --cache-control "no-cache, must-revalidate" --exclude "*.md" --exclude ".claude/*" --profile prod
```

Replace **nbk2** with your own CLI profile, and the bucket name with **your bucket** on a fresh account.

The `--cache-control` flag is required. Without it the uploaded files carry no Cache-Control header, browsers heuristically cache the JS, and users keep seeing the old build until a hard refresh. This bit the project on 2026-07-14; the flag was added the same day.

CloudFront behavior: distribution `E1YADURLSAVNFA` uses the managed CachingDisabled policy, so every request hits S3. A plain `s3 sync` is therefore a full redeploy with no invalidation step. Console check: CloudFront → Distributions → `E1YADURLSAVNFA` → Behaviors → default behavior → Cache policy = CachingDisabled. CLI check:

```
aws cloudfront get-distribution-config --id E1YADURLSAVNFA --profile prod --query "DistributionConfig.DefaultCacheBehavior.CachePolicyId"
```

(CachingDisabled's managed policy id is `4135ea2d-6df8-44a3-9df3-4b5a84be39ad`.)

Verify a deploy: `curl -s https://d1dtoxef7qmugl.cloudfront.net | findstr "<title>"` should show `ARGUS`, and a hard-refreshed browser session should log in and load the gallery with zero console errors.

### Deploy on the production account

The production dashboard was deployed by the ARGUS deployer (Chapter 7), not by a manual sync: `deployer/deploy.py` uploads every file of `web/dashboard_v4/` to `argus-dashboard-506868652945`, templating `js/config.js` in flight (section 4.4), and its CloudFront distribution `E1YADURLSAVNFA` also uses the CachingDisabled policy.

Note on frontend-only updates. The repository copy of `config.js` carries the live production values (API `vzfl7s6z00`, Cognito pool `us-east-1_9selFDHpc`, client `6vebotf45bp8u46cnraddiaplv`), corrected on 2026-08-15, so a plain `aws s3 sync web/dashboard_v4 s3://argus-dashboard-506868652945 --profile prod` is safe on this deployment. It was NOT safe before that date: the file still held the development account's values, and a hand-run sync would have overwritten the deployed, correctly templated `config.js` and silently pointed the production dashboard at a retired backend. The rule to keep: `config.js` is the one host-specific file, so before any hand-run sync confirm its three values match the account you are deploying to, or let the deployer's writeback stage template them (7.4).

Verify: `curl -s https://d1dtoxef7qmugl.cloudfront.net | findstr "<title>"` shows `ARGUS`, and the served `js/config.js` must contain `vzfl7s6z00` and `6vebotf45bp8u46cnraddiaplv` (checked good 2026-08-11).

### Cognito user management

Console path: Amazon Cognito → User pools → `pest-dashboard-users` (`us-east-1_ea0aJdusl`) → Users → Create user / select user → Delete. Self-signup is disabled; accounts are admin-created only. The commands below target the development pool with profile `nbk2`; for the production account substitute pool id `us-east-1_9selFDHpc` and profile `prod` — the flow is identical, and the first production user was created this way on 2026-08-11.

Create a user (replace **EMAIL** and **nbk2**):

```
aws cognito-idp admin-create-user --user-pool-id us-east-1_9selFDHpc --username EMAIL --message-action SUPPRESS --profile prod
```

Set a permanent password (policy: at least 8 characters, lowercase and number required; uppercase and symbols optional). Replace **EMAIL** and **PASSWORD**:

```
aws cognito-idp admin-set-user-password --user-pool-id us-east-1_9selFDHpc --username EMAIL --password PASSWORD --permanent --profile prod
```

`--permanent` matters. Without it the account is stuck in FORCE_CHANGE_PASSWORD, and the login UI intentionally does not handle that challenge (verified in `auth.js`: any `ChallengeName` shows "ask the admin").

Remove a user (replace **EMAIL**):

```
aws cognito-idp admin-delete-user --user-pool-id us-east-1_9selFDHpc --username EMAIL --profile prod
```

Never write any password into a committed file or chat log.

### Emergency rollback: authorizer detach

If login misbehaves during a demo, two levers, in order.

Lever 1: detach the JWT authorizer so the API is instantly open again. Console path: API Gateway → APIs → `zwpcbivmsj` → Routes → select each route → Attach authorization → set to None. In practice use the PowerShell loop (route ids are stable):

```
foreach ($r in @("1xu5zhs","1zwy2vl","2dlsspt","33iv13c","58gsnz6","7wgkuzm","ao22ksu","b3kl8si","i8pcllp","jedpy5t","krzufjf","nqy131o","odwxz00","t4w6czh","tos63cu","ttif38k","w0ivphi","wqh3mvj","zbh9lli") { aws apigatewayv2 update-route --api-id vzfl7s6z00 --route-id $r --authorization-type NONE --profile prod }
```

Route `glwyqo0` (`DELETE /detection`, destructive) is deliberately not in this list. Leave it JWT-gated even during a rollback; nothing in the no-auth fallback uses it.

Re-attach later with the same loop using `--authorization-type JWT --authorizer-id enxa26`.

These route ids belong to the development API `vzfl7s6z00`. The production API `vzfl7s6z00` has its own route ids; list them first with `aws apigatewayv2 get-routes --api-id vzfl7s6z00 --profile prod` and apply the same pattern (leave `DELETE /detection` gated).

Lever 2: if the login UI itself is broken, open the local fallback `web/_archive/dashboard_v3_9.html` via a local static server. It has no auth and sends no token, so it only works after lever 1. It still has the pre-v4.0 X-dismiss bug; that is accepted for an emergency fallback.

### Reproducing on a different AWS account (for NP staff)

This is no longer theory: the ARGUS deployer (Chapter 7) performed exactly this reproduction on the NP production account `506868652945` on 2026-08-10 — all 15 stages in 103 seconds, dashboard, Cognito, API, and frames-bucket CORS included — so the automated path is `python deployer/deploy.py` and the steps below are the manual equivalent (and the explanation of what the deployer does).

1. Create the S3 website bucket, enable static website hosting (index and error document `index.html`), allow public read.
2. Create the CloudFront distribution with the S3 website endpoint as origin and the CachingDisabled cache policy.
3. Create a Cognito user pool (email sign-in, self-signup off) and an app client with no secret, `USER_PASSWORD_AUTH` and refresh flows enabled.
4. Deploy the backend (Chapter 2; full fresh-account order in Chapter 8), then attach a JWT authorizer for that pool to every API route except `GET /stream/status`.
5. Edit `js/config.js`: **HTTP_API**, **COGNITO_REGION**, **COGNITO_CLIENT_ID**. This is the only frontend change needed.
6. Add the new CloudFront origin to the frames bucket's CORS AllowedOrigins (needed for Test upload).
7. Run the deploy sync, create users, sign in.

Remember the account-bound caveat: Rekognition Custom Labels models cannot migrate across accounts. The dashboard works immediately, but detection needs the models retrained on the new account. This was done during the 2026-08-10/11 migration: the training data was copied server-side to `argus-frames-506868652945`, the armyworm model (the v9r data-augmentation build) was submitted for training as `v9r-prod-20260810` in project `argus-detection`, and the moth model was rebuilt from the surviving labelled data as `moth-prod-20260811` in project `argus-moth-detection` (F1 = 0.991). Details in Chapter 3 and the migration scripts under `migration/`.

Security caution while reproducing: one historical script, `datasets/archive/experiments/pre_v3_abandoned/download.py`, contains an inline Roboflow API key (credential stored there, not reproduced here). Do not copy that file, or any file containing a credential, into anything that gets published or synced to the dashboard bucket. The deploy command's `--exclude` flags only cover `*.md` and `.claude/*`; the real protection is that the sync source is `web/dashboard_v4` only.

## 4.18 Cross-references

- Chapter 1: system overview; where the dashboard sits in the end-to-end chain.
- Chapter 2: the cloud stack this frontend calls — `pest-monitoring-api` routes, `kvs-hls-handler`, `pest-detection-processor` and the v4.5 hybrid Rekognition/LLM gate whose output the gallery trusts, DynamoDB record shape, S3 buckets, IAM.
- Chapter 3: detection models — Rekognition Custom Labels version history, `min_confidence` semantics, tiling, holdout methodology.
- Chapter 5: Unitree Go2 + Jetson Orin — patrol, SIYI A8 Mini capture, the capture-upload gate that produces gallery records.
- Chapter 6: mini PC — the KVS controller service behind the Live tab's stream toggles.
- Chapter 7: ARGUS deployer, which recreates this whole stack from the manifest.
- Chapter 8: full fresh-account reproduction, superset of section 4.17's frontend-only steps.
