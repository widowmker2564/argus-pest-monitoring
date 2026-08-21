/* ==============================================================
   CONFIG — the ONE place to change per host/env (see docs/dashboard.md).
   Split from dashboard_v3_9.html (v4.0 module split).
   ============================================================== */
export const CONFIG = {
    HTTP_API: 'https://vzfl7s6z00.execute-api.us-east-1.amazonaws.com',
    // v4.1 Cognito auth (production pool us-east-1_9selFDHpc; client id is a
    // public identifier, not a secret). The ARGUS deployer templates these
    // three values at deploy time, but a hand-run `aws s3 sync web/dashboard_v4`
    // ships this file as-is, so it must carry the live production values.
    COGNITO_REGION: 'us-east-1',
    COGNITO_CLIENT_ID: '6vebotf45bp8u46cnraddiaplv',
    // WS_URL removed in v3.7 — WebSocket disabled (dashboard polls instead)
    SG_OFFSET_HOURS: 8,           // Singapore = UTC+8
    MODEL_POLL_INTERVAL_MS: 5000, // poll model status every 5s while custom models exist
    CACHE_DB: 'pest-monitor-cache',
    CACHE_STORE: 'images',
    // v5.3: bumped 200->400 MB — gallery pagination now fetches 500 records/page
    // set of 50, so a full page-through caches ~5x the old working set.
    CACHE_MAX_BYTES: 400 * 1024 * 1024,  // 400 MB
};
