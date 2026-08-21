# Dashboard

**v5.5 (2026-08-16, deployed): Analytics page repaired.** The page had been
half-dead and nobody noticed, because the failure was swallowed. `renderDailyChart`
read a variable `sub` that was never declared — a leftover from a refactor that
deleted the "Daily trend" card subtitle but not the line writing to it. ES modules
are strict mode, so the read threw `ReferenceError: sub is not defined`. That throw
happened inside `loadAnalytics`'s `try`, which turned the whole page into one red
toast and, critically, **skipped the two render calls that came after it**: Camera
health and By camera never ran at all. Stats, By zone and the heatmap had already
rendered, which is why the page looked merely "partly empty" instead of broken.
Fix: restore `<div class="card-sub" id="daily-sub">` in the card head and declare
`const sub = document.getElementById('daily-sub')`.
Second bug in the same pass: the By-zone chart sets `indexAxis: 'y'`, which makes
the bar horizontal, so x carries the counts and y carries the zone names — but the
titles were passed as `chartOpts('Zone', 'Detections')`, exactly backwards. The
printed chart had "Zone" under a 0-40 count axis. Now `chartOpts('Detections', 'Zone')`.
Verified live in the browser: zero toasts, `daily-sub` reads "30 days · 38 pests",
Camera health and By camera both populated.
NOTE for anyone testing this: the browser caches the ES modules hard. A plain
reload kept serving the old `analytics.js` even though CloudFront was already
serving the new one. Force a refill first —
`for (const u of ['/js/analytics.js']) await fetch(u, {cache:'reload'})` — then reload.

**v5.4 (2026-08-15, deployed): zoom-aware box chrome.** The modal overlay carries the
same CSS transform as the image, so at 6x zoom a 4px border rendered as 24px and the
per-box flag button grew six-fold — on a small worm the chrome covered the animal the
operator was trying to inspect. `attachImageZoom` (in `js/bbox.js`) now publishes the
live scale onto the overlay as the custom property `--z`, and every decoration in
`styles.css` divides by it: `border-width: calc(2.4px / var(--z))`, the label font and
padding, and the flag button's size and corner offset. Effect: the outline and the
button hold a CONSTANT on-screen size at any zoom while the worm keeps growing.
Geometry stays in percentages, so boxes still track the image exactly. Base border was
also thinned 4px -> 2.4px. Verified live: `curl` on the deployed `styles.css` shows 13
`var(--z)` sites and the deployed `bbox.js` sets the property.

**config.js repointed (2026-08-15).** The repo copy carried the retired dev account's
API and Cognito values while `docs/dashboard.md` documented `aws s3 sync` as the
redeploy path — a hand-run sync would have pointed the production dashboard at a dead
backend. It now holds `vzfl7s6z00`, `us-east-1_9selFDHpc`,
`6vebotf45bp8u46cnraddiaplv`. The deployer's writeback stage still templates these at
deploy time; the repo values matter only for a hand-run sync.

## Current file
**Rebrand to ARGUS (2026-07-10):** name ratified by Runzhe. Page `<title>`, topbar
brand, and login-card brand all changed from "Pest Monitor" to "ARGUS"; the "P"
letter-mark logo replaced with the nine-line aperture SVG (same mark as the
deployer wordmark) in both header locations. Styling-only — `.brand-logo` base
rule switched from a lettered square (`border-radius:7px`, centered text) to a
circular icon frame (`border-radius:50%`, centered SVG); the v5.2 theme's color
override on `.brand-logo` needed no change. Deployed + verified live (title tag
confirmed via curl).

**v5.2 (2026-07-10): full liquid-glass pass.** v5.1 read as flat ("看不出用了
liquid glass") because the base was near-white — glass needs a visible backdrop
to transmit. v5.2: clearly visible pastel color field behind everything; panels
at 0.22 alpha + blur(10px) saturate(190%); 45° specular glints on every surface;
and buttons / tabs (glass pills, underline removed) / chips / badges / inputs are
ALL glass now (per the reference's transparent "Get started" button). Tuning
knobs in `:root`: `--lg-bg`, `--lg-blur`, `--lg-rims` (panels) and the `-ctl`
variants (controls).

Predecessor of the reskin:
**v5.1 (2026-07-10): `web/dashboard_v4/` — ARGUS liquid-glass reskin, LIGHT.**
Same 13 js modules + Cognito login; STYLING + layout only, zero behavior change.
- **Theme:** WHITE base (Runzhe rejected the first dark pass same day). Liquid-glass
  optics taken from his reference (yzrt pure-CSS demo): LOW blur (6px) + inner white
  highlight rims (inset shadows simulating glass thickness) + hair-thin dark inner
  line + 45° specular edge on display surfaces (login card, KPI cards). Accent
  moved to iOS blue #0a84ff. Faint ambient color tints on the white base (gradients
  only — full-viewport `filter: blur()` was removed for render performance). All
  rules live in the "ARGUS THEME (v5.1)" block at the end of `styles.css`; the
  original light theme above is untouched, so revert = delete that block.
- **Gallery:** images now render FIRST; the filter bar moved BELOW the grid as a
  compact `.filter-bar.compact` section (same 5 controls, same ids, same
  apply/clear handlers — pure layout move).
- **Analytics:** added a zone×day heatmap (`renderZoneHeatmap`, last 14 days, bbox
  counts via getCountedBoxes — same counting rules as every other widget). Chart
  colors retinted for dark.
- Pre-redesign backup: `web/_archive/dashboard_v4_v42_pre-argus_2026-07-10/`.
- Deployed to S3/CloudFront 2026-07-10. Verified: login → gallery (grid-first,
  filter-below) → analytics heatmap, delete/verify handlers intact, zero console
  errors.
- OPEN: product wordmark still says "Pest Monitor" in `index.html` + login; rename
  to ARGUS is a separate call (gated by Runzhe ratifying the name).

## Predecessor
**v4.1 (2026-07-06): v4.0 module split + Cognito login.** index.html + styles.css +
13 js modules. Same behavior, plus the bbox X-dismiss fix (see below).
Serve any static way: Live Server (open `web/dashboard_v4/index.html`), or
`python -m http.server 5501 --directory web/dashboard_v4`. Verified working against the
live API (modules load clean, gallery hydrates saved verdicts).

`dashboard_v3_9.html` — the single-file predecessor, kept untouched as fallback (still
has the X-dismiss bug). Served via Live Server at http://localhost:5500/.

## What it does
Operator dashboard for pest detections. Tabs: cameras (live KVS streams), gallery (detection
images), analytics. Talks to the Lambda API `pest-monitoring-api` via API Gateway (see
`docs/aws.md`).

## Gallery pagination (v5.3, deployed 2026-07-21)
Gallery now pages **50 images/page** client-side with prev/next, windowed page
numbers, and jump-to-page. The backend `handle_get_history` already scans + sorts
all matching records and returns the top `limit`; the frontend fetch bumped
**100 → 500** (`state.galleryFilters.limit`, `applyGalleryFilters`,
`resetGalleryFilters`), and `renderGalleryGrid` slices `state.galleryItems` by
`state.galleryPage` × `PAGE_SIZE(50)`. Cards keep their GLOBAL index (`start + i`)
so `openImg`/`deleteGalleryItem`/thumb-load still resolve against
`state.galleryItems`; only the current page's 50 thumbnails lazy-load
(`loadThumbsForIndices`, replaced `loadThumbsThrottled`). `gotoGalleryPage(p)`
clamps + re-renders + scrolls; exposed on window via main.js. Records older than
the most-recent 500 (table had ~697 on 2026-07-21) are reached by narrowing the
date filter (server-side). CSS `.gallery-pagination`/`.pager-*` (token-based, both
themes) appended to styles.css. Files: `js/{gallery,state,main}.js` + styles.css,
deployed to S3 (CloudFront CachingDisabled = instant; hard-refresh for cached JS).

## Gallery delete (v4.2, deployed 2026-07-07)
Every gallery card has a Delete button (also inside the image modal); two-step
confirm (button click → confirm dialog). Flow: `api.deleteDetection` →
`DELETE /detection?image_id=` (JWT-gated route `glwyqo0`) → the Lambda deletes
ALL DynamoDB rows for that image_id plus the S3 frame (and legacy processed
image); frontend then drops every matching card, evicts the IndexedDB cache
entries, and recomputes the summary bar. Safety design (reviewed + E2E-tested):
- No record → 404, nothing touched. The endpoint cannot delete arbitrary S3
  keys; frame deletes are gated to `frames/` in code AND in IAM.
- Any S3 delete failure → 500 with rows kept (retry-safe; the record is the
  only index to the object, so it must outlive it).
- Stale-closure guard in loadOneThumb: in-flight thumbnail retries verify the
  record at their index still owns their key before painting (a delete
  re-render shifts indices).
E2E verified 2026-07-07: no token → 401; training-asset keys → 404 + object
intact; real capture → 200, 0 rows left, S3 object gone; browser cancel path
leaves everything untouched.

## bbox rendering (v3.9)
Processor no longer bakes boxes into a "processed" image. Frontend fetches the original frame +
the `bboxes` coords from the DynamoDB record and draws boxes on a canvas overlay locally.
- Per-bbox verify: clicking TP/FP calls `verifyClick` → `api.verifyDetection` → persists to the
  DDB `verifications` map (keyed by bbox index). Optimistic update with rollback on failure.
- `hydrateVerifyMap` reads `verifications` back on load. Local edits win over backend.
- Graceful fallback to the legacy processed image for old records that have no `bboxes`.

## X-dismiss bug — FIXED in v4.0 (still present in v3_9)
Root cause found 2026-07-02: the draw paths (renderOverlayBoxes, paintCardBoxes) already
skipped FP boxes. The broken half was the READ: only analytics/settings called
`hydrateVerifyMap`; the gallery load path never did. A fresh load straight into Gallery
had an empty verifyMap, so every FP-dismissed box reappeared. Fix (js/gallery.js,
loadGallery): hydrate verifyMap from each fetched record before drawing. Verified live:
gallery load now hydrates the saved verdicts (92 records on the current data).

## Module layout (DONE 2026-07-02 — this was the refactor plan, now shipped in web/dashboard_v4/)
ES modules (`<script type="module">` + import/export, no bundler, works over any static
http). Only the inline-onclick handler surface is exposed on window (see js/main.js);
everything else is module-private. Actual layout (matches the plan, plus live.js /
modal split into its own file and costs.js kept as a dormant tab):
- `index.html` — markup + mount points
- `styles.css` — the CSS block
- `js/config.js` — API base URL + Cognito region/client id (single point per host/env)
- `js/auth.js` — Cognito sign-in (v4.1): login overlay wiring, token store, auto-refresh
- `js/api.js` — all `api.*` fetch calls; attaches the Bearer token, 401 → login overlay
- `js/state.js` — global `state`, verifyMap, imageCache
- `js/utils.js` — escapeHtml, toast, formatters
- `js/bbox.js` — canvas draw + attachImageZoom
- `js/gallery.js`, `js/cameras.js`, `js/analytics.js`, `js/modal.js` — per-tab render
- `js/main.js` — bootstrap, tab routing, event wiring

Do NOT use multiple flat `<script src>` tags — that stays in global scope, which is not a real
split.

## Live deployment (v4.1, 2026-07-06 — HTTPS + Cognito login)
**URL to hand out: https://d1dtoxef7qmugl.cloudfront.net**
Stack: CloudFront `E1YADURLSAVNFA` (CachingDisabled managed policy — every request hits
S3, so a plain `s3 sync` is a full redeploy, no invalidation step) → S3 static website
`argus-dashboard-506868652945` (prod, us-east-1, public-read, `index.html` as index +
error document). The raw S3 URL
http://argus-dashboard-506868652945.s3-website-us-east-1.amazonaws.com still works but is
HTTP-only — do not hand it out; passwords must not travel over plain HTTP.

Auth (v4.1):
- Cognito User Pool `us-east-1_9selFDHpc` (`pest-dashboard-users`); app client
  `4husu6afr835e235eu9dqp8av6` (`dashboard-web`, no secret, USER_PASSWORD_AUTH +
  refresh, 12 h tokens / 30-day refresh). Sign-in = email. Self-signup DISABLED —
  accounts are admin-created only.
- API GW `vzfl7s6z00`: JWT authorizer `cognito-dashboard` (id `enxa26`) attached to all
  routes EXCEPT `GET /stream/status` (route `gcc7355`) — the Orin and mini-PC
  `kvs_controller.py` poll that route unauthenticated; it is read-only, exempting it
  keeps both devices untouched. CORS AllowHeaders now includes `authorization`
  (mirrored in `lambda/cors.json`).
- Frontend: `js/auth.js` — raw InitiateAuth fetch to the Cognito IDP endpoint (no SDK,
  fits the bundler-free module setup), tokens in localStorage, auto-refresh 5 min
  before expiry. `api.js` attaches `Authorization: Bearer <IdToken>` on every call and
  re-opens the login overlay on any 401. Enforcement lives in API GW, not in the JS.

Verified end-to-end 2026-07-06 (throwaway smoketest account, deleted after): wrong
password shows the Cognito error inline; correct login boots the app (Live tab +
Gallery with 100 cards over the authed API); Sign out clears tokens and re-gates;
no-token API call → 401; no-token `GET /stream/status` → 200; REFRESH_TOKEN_AUTH flow
works; CloudFront serves the same build over HTTPS with correct MIME types.

Manage users — CLI (console: Cognito → User pools → pest-dashboard-users → Users):
- create:
  `aws cognito-idp admin-create-user --user-pool-id us-east-1_9selFDHpc --username EMAIL --message-action SUPPRESS --profile prod`
- set permanent password (policy since 2026-07-06: ≥8 chars, lowercase + number
  required; uppercase/symbols optional):
  `aws cognito-idp admin-set-user-password --user-pool-id us-east-1_9selFDHpc --username EMAIL --password PASSWORD --permanent --profile prod`
  (`--permanent` matters — without it the account is stuck in FORCE_CHANGE_PASSWORD,
  which the login UI intentionally does not handle)
- delete:
  `aws cognito-idp admin-delete-user --user-pool-id us-east-1_9selFDHpc --username EMAIL --profile prod`

Redeploy after any edit (the `--cache-control` flag is REQUIRED — without it the
uploaded files carry no Cache-Control header and browsers heuristically cache the
JS, so users keep seeing the old build until a hard refresh; added 2026-07-14
after exactly that bite):
`aws s3 sync web/dashboard_v4 s3://argus-dashboard-506868652945 --delete --cache-control "no-cache, must-revalidate" --exclude "*.md" --exclude ".claude/*" --profile prod`

Test-upload gotcha (hit + fixed 2026-07-07): the browser POSTs/PUTs the test image
straight to S3 with a presigned URL, so the FRAMES bucket
(`argus-frames-506868652945`) must list the dashboard origin in its CORS
AllowedOrigins. Now lists: CloudFront + S3 website endpoint + localhost 5500/5501.
"Failed to fetch" on Test upload from a new origin = add that origin there
(S3 → frames bucket → Permissions → CORS, or `aws s3api put-bucket-cors`).

EMERGENCY ROLLBACK (if login misbehaves during a demo), two levers:
1. Detach the authorizer — API instantly open again (PowerShell). NOTE: route
   `glwyqo0` (DELETE /detection, destructive) is deliberately NOT in this list —
   leave it JWT-gated even during a rollback; nothing in the no-auth fallback
   uses it:
   `foreach ($r in @(**route ids from `aws apigatewayv2 get-routes --api-id vzfl7s6z00 --profile prod`**) { aws apigatewayv2 update-route --api-id vzfl7s6z00 --route-id $r --authorization-type NONE --profile prod }`
   Re-attach later with the same loop using `--authorization-type JWT --authorizer-id enxa26`.
2. If the login UI itself is the problem: `web/dashboard_v3_9.html` has no auth — open
   it via Live Server locally (only works after lever 1, since it sends no token).

## Deploy + auth roadmap — SHIPPED 2026-07-06
The whole chain from the original plan is done: bbox fix → module split → config
constant → cloud serve (S3+CloudFront instead of EC2+nginx; static files made the EC2
box pointless) → Cognito auth on the deployed structure. Frontend layering remains
host-agnostic; the only host-sensitive bit is the API base URL in `js/config.js`.
