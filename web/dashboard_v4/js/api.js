/* ==============================================================
   API CLIENT — every backend call goes through here (the frontend
   /backend seam). Split from dashboard_v3_9.html (v4.0 module split).
   ============================================================== */
import { CONFIG } from './config.js';
import { getIdToken, requireLogin } from './auth.js';

export const api = {
    async _fetch(path, opts = {}) {
        const headers = { 'Content-Type': 'application/json' };
        const token = await getIdToken();
        if (token) headers['Authorization'] = 'Bearer ' + token;
        const res = await fetch(CONFIG.HTTP_API + path, { headers, ...opts });
        const text = await res.text();
        let data;
        try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
        if (res.status === 401) {
            requireLogin();
            throw new Error('Signed out — please sign in again');
        }
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        return data;
    },
    getSettings() { return this._fetch('/settings'); },
    postSettings(body) { return this._fetch('/settings', { method: 'POST', body: JSON.stringify(body) }); },
    getHistory(f = {}) {
        const q = new URLSearchParams();
        for (const [k, v] of Object.entries(f)) if (v !== '' && v != null) q.set(k, v);
        return this._fetch('/history?' + q);
    },
    getPresignedUrl(key) {
        return this._fetch('/presigned-url?key=' + encodeURIComponent(key) + '&method=GET');
    },
    getModelStatus() { return this._fetch('/model/status'); },
    startModel(camera_id) { return this._fetch('/model/start', { method: 'POST', body: JSON.stringify({ camera_id }) }); },
    stopModel(camera_id)  { return this._fetch('/model/stop',  { method: 'POST', body: JSON.stringify({ camera_id }) }); },
    getCost(days = 30) { return this._fetch('/cost?days=' + days); },
    getIdentities() { return this._fetch('/identities'); },
    addIdentity(email) { return this._fetch('/identities', { method: 'POST', body: JSON.stringify({ email }) }); },
    removeIdentity(email) { return this._fetch('/identities?email=' + encodeURIComponent(email), { method: 'DELETE' }); },
    getSchedule() { return this._fetch('/schedule'); },
    postSchedule(body) { return this._fetch('/schedule', { method: 'POST', body: JSON.stringify(body) }); },
    deleteSchedule(camera) { return this._fetch('/schedule?camera=' + encodeURIComponent(camera), { method: 'DELETE' }); },
    getScheduleLogs(limit = 30) { return this._fetch('/schedule-logs?limit=' + limit); },
    getVideoPlayback(stream) {
        return this._fetch('/video-playback?stream=' + encodeURIComponent(stream));
    },
    // NEW v3.2 — KVS stream control
    getStreamStatus(camera) {
        return this._fetch('/stream/status' + (camera ? '?camera=' + encodeURIComponent(camera) : ''));
    },
    startStream(camera_id) { return this._fetch('/stream/start', { method: 'POST', body: JSON.stringify({ camera_id }) }); },
    stopStream(camera_id)  { return this._fetch('/stream/stop',  { method: 'POST', body: JSON.stringify({ camera_id }) }); },

    // v3.4: presigned PUT for browser-direct upload (backend already supports method=PUT)
    getPresignedUploadUrl(key) {
        return this._fetch('/presigned-url?key=' + encodeURIComponent(key) + '&method=PUT');
    },
    // v3.6: per-bbox detection verification.
    //   verdict: "TP" | "FP" | null (null clears that bbox's verdict)
    //   bbox_index: 0-based index of the bbox in target-label detections, sorted by confidence desc
    verifyDetection(image_id, bbox_index, verdict) {
        return this._fetch('/detection/verify', {
            method: 'POST',
            body: JSON.stringify({ image_id, bbox_index, verdict }),
        });
    },
    // v4.2: permanent delete of one detection (DDB record + S3 objects)
    deleteDetection(image_id) {
        return this._fetch('/detection?image_id=' + encodeURIComponent(image_id), { method: 'DELETE' });
    },
};
