/* ==============================================================
   STATE + IMAGE CACHE (IndexedDB)
   Split from dashboard_v3_9.html (v4.0 module split).
   `state` is the single shared mutable store; modules import and
   mutate it directly (same object identity as the old global).
   ============================================================== */
import { CONFIG } from './config.js';

export const state = {
    tab: 'live',
    settingsSub: 'cameras',
    settings: null,
    cameras: {},
    schedules: {},              // v3.4: {camera_id: {enabled, start_cron, stop_cron, ...}}
    cameraOps: {},              // v3.4: {camera_id: {last_detection_time, today_count}}
    modelStatuses: {},          // {camera_id: {status, arn, message}}
    streamStatuses: {},         // {camera_id: {stream_enabled, status}}
    history: [],
    galleryFilters: { camera: '', zone: '', model: '', detected: '', date_from: '', date_to: '', limit: 500 },
    galleryItems: [],
    galleryPage: 0,             // v5.3: client-side gallery pagination (50/page)
    costData: null,
    identities: [],
    scheduleLogs: [],
    notifs: [],
    notifMax: 50,
    hls: null,
    liveCam: null,
    hourlyChart: null,          // legacy ref — not used in v3.4 (chart removed)
    dailyChart: null,
    byZoneChart: null,          // v3.4
    costChart: null,
    modelPollTimer: null,       // poll handle for custom model status
    db: null,                   // IndexedDB instance
    pendingThumbReq: 0,         // throttle counter for presigned-url calls
    uploadFile: null,           // v3.4: currently selected file in test-upload tab
    uploadCam: 'armyworm_go2_a8mini',  // v3.4: target camera/model selection
    verifyMap: {},              // v3.6: {image_id: {bbox_index: 'TP'|'FP'}} — per-bbox verdicts, persisted to backend.
                                //        Legacy {image_id: 'true'|'false'} on load is migrated to all-TP/all-FP.
};

/* Persistent thumbnail cache so refresh doesn't re-fetch every URL.
   Key = S3 image_id (path). Value = {blob, mime, fetched_at, size}.
   LRU evict when total size exceeds CACHE_MAX_BYTES. */
export const imageCache = {
    async open() {
        if (state.db) return state.db;
        return new Promise((resolve, reject) => {
            const req = indexedDB.open(CONFIG.CACHE_DB, 1);
            req.onupgradeneeded = (e) => {
                const db = e.target.result;
                if (!db.objectStoreNames.contains(CONFIG.CACHE_STORE)) {
                    const store = db.createObjectStore(CONFIG.CACHE_STORE, { keyPath: 'key' });
                    store.createIndex('fetched_at', 'fetched_at', { unique: false });
                }
            };
            req.onsuccess = () => { state.db = req.result; resolve(state.db); };
            req.onerror = () => reject(req.error);
        });
    },

    async get(key) {
        try {
            const db = await this.open();
            return new Promise((resolve) => {
                const tx = db.transaction(CONFIG.CACHE_STORE, 'readonly');
                const req = tx.objectStore(CONFIG.CACHE_STORE).get(key);
                req.onsuccess = () => resolve(req.result || null);
                req.onerror = () => resolve(null);
            });
        } catch { return null; }
    },

    async put(key, blob) {
        try {
            const db = await this.open();
            return new Promise((resolve, reject) => {
                const tx = db.transaction(CONFIG.CACHE_STORE, 'readwrite');
                const entry = { key, blob, mime: blob.type, fetched_at: Date.now(), size: blob.size };
                const req = tx.objectStore(CONFIG.CACHE_STORE).put(entry);
                req.onsuccess = () => resolve(true);
                req.onerror = () => reject(req.error);
            });
        } catch { return false; }
    },

    async remove(key) {
        try {
            const db = await this.open();
            return new Promise((resolve) => {
                const tx = db.transaction(CONFIG.CACHE_STORE, 'readwrite');
                const req = tx.objectStore(CONFIG.CACHE_STORE).delete(key);
                req.onsuccess = () => resolve(true);
                req.onerror = () => resolve(false);
            });
        } catch { return false; }
    },

    async fetchAndCache(key, presignedUrl) {
        const cached = await this.get(key);
        if (cached) return URL.createObjectURL(cached.blob);
        try {
            const r = await fetch(presignedUrl);
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const blob = await r.blob();
            await this.put(key, blob);
            return URL.createObjectURL(blob);
        } catch (err) {
            console.warn('[Cache] fetch failed for', key, err);
            return presignedUrl;  // fall back to direct URL
        }
    },

    async evictIfFull() {
        try {
            const db = await this.open();
            const items = await new Promise((resolve) => {
                const tx = db.transaction(CONFIG.CACHE_STORE, 'readonly');
                const req = tx.objectStore(CONFIG.CACHE_STORE).getAll();
                req.onsuccess = () => resolve(req.result || []);
                req.onerror = () => resolve([]);
            });
            const total = items.reduce((s, i) => s + (i.size || 0), 0);
            if (total < CONFIG.CACHE_MAX_BYTES) return;
            // LRU: oldest first
            items.sort((a, b) => a.fetched_at - b.fetched_at);
            let freed = 0;
            const target = total - CONFIG.CACHE_MAX_BYTES * 0.7;
            for (const it of items) {
                if (freed > target) break;
                const tx = db.transaction(CONFIG.CACHE_STORE, 'readwrite');
                tx.objectStore(CONFIG.CACHE_STORE).delete(it.key);
                freed += it.size || 0;
            }
            console.log(`[Cache] Evicted ${(freed/1e6).toFixed(1)} MB`);
        } catch (err) { console.warn('[Cache] evict failed', err); }
    },

    async size() {
        try {
            const db = await this.open();
            const items = await new Promise((resolve) => {
                const tx = db.transaction(CONFIG.CACHE_STORE, 'readonly');
                const req = tx.objectStore(CONFIG.CACHE_STORE).getAll();
                req.onsuccess = () => resolve(req.result || []);
                req.onerror = () => resolve([]);
            });
            const bytes = items.reduce((s, i) => s + (i.size || 0), 0);
            return { count: items.length, bytes };
        } catch { return { count: 0, bytes: 0 }; }
    },

    async clear() {
        try {
            const db = await this.open();
            return new Promise((resolve) => {
                const tx = db.transaction(CONFIG.CACHE_STORE, 'readwrite');
                tx.objectStore(CONFIG.CACHE_STORE).clear();
                tx.oncomplete = () => resolve(true);
                tx.onerror = () => resolve(false);
            });
        } catch { return false; }
    },
};
