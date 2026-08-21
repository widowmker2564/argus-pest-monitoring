/* ==============================================================
   PAGE: GALLERY — filters, grid, throttled thumb loading with
   IndexedDB cache, card bbox overlays.
   Split from dashboard_v3_9.html (v4.0 module split).
   v4.0 BUGFIX in loadGallery(): hydrate verifyMap from the fetched
   records. v3_9 only hydrated on the analytics/settings paths, so a
   fresh load straight into Gallery had an empty verifyMap and every
   FP-dismissed box reappeared after reload (the X-dismiss bug in
   docs/dashboard.md). The draw paths already skip FP boxes.
   ============================================================== */
import { state, imageCache } from './state.js';
import { api } from './api.js';
import { toast, escapeHtml, pestClass, fmtTimeShort, todayYMD, daysAgoYMD, cleanLabel, camDisplayName } from './utils.js';
import { getVerifiableBoxes, getDrawableBoxes, aggregateVerdict, hydrateVerifyMap, paintCardBoxes } from './bbox.js';

export async function renderGalleryPage() {
    const content = document.getElementById('page-content');
    const cameras = Object.keys(state.cameras);
    const pestOptions = [...new Set(Object.values(state.cameras).map(c => c.target_label).filter(Boolean))];

    // v5.0 (ARGUS reskin): images FIRST — the grid opens the page; the filter
    // bar moved BELOW the grid as a compact section (same controls, same ids,
    // same apply/clear handlers — layout change only).
    content.innerHTML = `
        <div class="gallery-summary" id="gallery-summary"></div>
        <div id="gallery-grid"></div>
        <div class="gallery-pagination" id="gallery-pagination"></div>

        <div class="filter-bar compact">
            <div class="filter-title">Filters</div>
            <div>
                <div class="input-label">Date range</div>
                <select class="select" id="flt-range">
                    <option value="7">Last 7 days</option>
                    <option value="1">Today</option>
                    <option value="3">Last 3 days</option>
                    <option value="14">Last 14 days</option>
                    <option value="30">Last 30 days</option>
                    <option value="all">All time</option>
                </select>
            </div>
            <div>
                <div class="input-label">Pest type</div>
                <select class="select" id="flt-model">
                    <option value="">All pests</option>
                    ${pestOptions.map(p => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join('')}
                </select>
            </div>
            <div>
                <div class="input-label">Location</div>
                <input type="text" class="input" id="flt-zone" placeholder="Any zone">
            </div>
            <div>
                <div class="input-label">Camera</div>
                <select class="select" id="flt-camera">
                    <option value="">All cameras</option>
                    ${cameras.map(id => `<option value="${escapeHtml(id)}">${escapeHtml(cleanLabel(state.cameras[id].label) || id)}</option>`).join('')}
                </select>
            </div>
            <div>
                <div class="input-label">Detected</div>
                <select class="select" id="flt-detected">
                    <option value="">All</option>
                    <option value="true">Detected only</option>
                    <option value="false">Clear only</option>
                </select>
            </div>
            <div style="display:flex;gap:8px;">
                <button class="btn btn-primary btn-sm" onclick="applyGalleryFilters()">Apply</button>
                <button class="btn btn-outline btn-sm" onclick="resetGalleryFilters()">Clear</button>
            </div>
        </div>
    `;
    await loadGallery();
}

// v4.2: last server-side scan count, so the summary bar can be recomputed
// locally after a delete without re-fetching.
let lastScanned = 0;

// v5.3: client-side pagination. The backend returns up to `limit` (500) most-recent
// matching records already sorted; we render PAGE_SIZE per page and flip locally
// (instant, and only 50 thumbnails load per page). Older-than-500 records are
// reachable by narrowing the date filter (that filtering is server-side).
const PAGE_SIZE = 50;

export async function loadGallery() {
    const grid = document.getElementById('gallery-grid');
    grid.innerHTML = '<div class="loading-wrapper"><span class="spinner"></span> Loading detections…</div>';
    try {
        const resp = await api.getHistory(state.galleryFilters);
        state.galleryItems = resp.items || [];
        state.galleryPage = 0;   // fresh fetch always starts on page 1
        lastScanned = resp.scanned || state.galleryItems.length;
        // v4.0 BUGFIX (X-dismiss persistence): pull saved per-bbox verdicts out of the
        // fetched records BEFORE drawing, so boxes flagged FP stay hidden after a
        // reload that lands straight on the Gallery tab. Local unsaved edits still
        // win — hydrateVerifyMap never overwrites an existing local entry.
        for (const it of state.galleryItems) hydrateVerifyMap(it);
        renderGalleryGrid();
        updateGallerySummary(resp);
    } catch (err) {
        grid.innerHTML = `<div class="empty-state"><h3>Failed to load</h3><p>${escapeHtml(err.message)}</p><p style="margin-top:8px;"><code>Make sure pest-history-query Lambda has CORS headers (v3).</code></p></div>`;
    }
}

async function updateGallerySummary(resp) {
    // v3.6.2: chip counts roll up bboxes per pest type, not photos.
    // "Armyworm-larva · 12" now means "12 worm sightings", not "12 photos containing any worm".
    const byPest = {};
    for (const it of state.galleryItems) {
        const n = getVerifiableBoxes(it).length;
        if (n === 0) continue;
        const p = (it.target_label || 'unknown').toLowerCase();
        byPest[p] = (byPest[p] || 0) + n;
    }
    const chips = Object.entries(byPest).map(([label, count]) => {
        const cls = pestClass(label);
        return `<span class="pest-chip ${cls}">${escapeHtml(label.charAt(0).toUpperCase() + label.slice(1))} · ${count}</span>`;
    }).join('');

    // Cache size info
    const cacheInfo = await imageCache.size();
    const cacheMB = (cacheInfo.bytes / 1e6).toFixed(1);

    const summary = document.getElementById('gallery-summary');
    if (summary) {
        summary.innerHTML = `
            <div class="gallery-summary-text">
                ${state.galleryItems.length} detection${state.galleryItems.length === 1 ? '' : 's'}${state.galleryItems.length >= (state.galleryFilters.limit || 500) ? ' <span style="color:var(--muted);font-weight:400;">(most recent ' + (state.galleryFilters.limit || 500) + ' — narrow the date range for older)</span>' : ''}
                <span style="color:var(--muted);font-weight:400;font-size:12px;margin-left:10px;">
                    · cache: ${cacheInfo.count} images (${cacheMB} MB)
                    <button class="btn-ghost btn-sm" style="font-size:11px;padding:2px 8px;margin-left:4px;" onclick="clearImageCache()">Clear</button>
                </span>
            </div>
            <div class="gallery-summary-chips">${chips}</div>`;
    }
}

export async function clearImageCache() {
    if (!confirm('Clear image cache? Next view will re-fetch all thumbnails from S3.')) return;
    await imageCache.clear();
    toast('Image cache cleared', 'success');
    loadGallery();
}

export function applyGalleryFilters() {
    const range = document.getElementById('flt-range').value;
    const filters = {
        camera: document.getElementById('flt-camera').value,
        zone: document.getElementById('flt-zone').value.trim(),
        model: document.getElementById('flt-model').value,
        detected: document.getElementById('flt-detected').value,
        date_from: '', date_to: '',
        limit: 500,
    };
    if (range !== 'all') {
        filters.date_from = daysAgoYMD(parseInt(range, 10) - 1);
        filters.date_to = todayYMD();
    }
    state.galleryFilters = filters;
    loadGallery();
}

export function resetGalleryFilters() {
    state.galleryFilters = { camera: '', zone: '', model: '', detected: '', date_from: daysAgoYMD(6), date_to: todayYMD(), limit: 500 };
    renderGalleryPage();
}

export function renderGalleryGrid() {
    const grid = document.getElementById('gallery-grid');
    const pager = document.getElementById('gallery-pagination');
    if (state.galleryItems.length === 0) {
        grid.className = '';
        grid.innerHTML = `<div class="empty-state"><h3>No detections</h3><p>Try widening your filters or wait for new captures.</p></div>`;
        if (pager) pager.innerHTML = '';
        return;
    }
    const total = state.galleryItems.length;
    const totalPages = Math.ceil(total / PAGE_SIZE);
    // clamp (e.g. after a delete emptied the last page)
    if (state.galleryPage >= totalPages) state.galleryPage = totalPages - 1;
    if (state.galleryPage < 0) state.galleryPage = 0;

    const start = state.galleryPage * PAGE_SIZE;
    const pageItems = state.galleryItems.slice(start, start + PAGE_SIZE);

    grid.className = 'gallery-grid';
    // cards keep their GLOBAL index (start + i) so openImg/delete/thumb all resolve
    // against state.galleryItems regardless of which page is showing.
    grid.innerHTML = pageItems.map((it, i) => galleryCard(it, start + i)).join('');
    renderPagination(start, pageItems.length, total, totalPages);

    // lazy-load only THIS page's thumbnails (by global index)
    loadThumbsForIndices(pageItems.map((_, i) => start + i));
}

// v5.3: jump to a page (0-indexed), clamp, re-render, scroll grid into view.
export function gotoGalleryPage(p) {
    p = parseInt(p, 10);
    if (isNaN(p)) return;
    const totalPages = Math.ceil(state.galleryItems.length / PAGE_SIZE);
    state.galleryPage = Math.max(0, Math.min(p, totalPages - 1));
    renderGalleryGrid();
    const grid = document.getElementById('gallery-grid');
    if (grid) grid.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderPagination(start, shown, total, totalPages) {
    const pager = document.getElementById('gallery-pagination');
    if (!pager) return;
    if (totalPages <= 1) {
        pager.innerHTML = `<div class="pager-info">${total} shown</div>`;
        return;
    }
    const cur = state.galleryPage;   // 0-indexed
    // windowed page numbers: always show first + last + the neighbours of current,
    // with "…" gaps between non-adjacent runs.
    const shownPages = new Set([0, totalPages - 1]);
    for (let p = cur - 1; p <= cur + 1; p++) if (p >= 0 && p < totalPages) shownPages.add(p);
    const sorted = [...shownPages].sort((a, b) => a - b);
    let prev = -1;
    const numHtml = sorted.map(p => {
        let h = '';
        if (prev !== -1 && p - prev > 1) h += `<span class="pager-ellipsis">…</span>`;
        prev = p;
        h += `<button class="pager-btn${p === cur ? ' active' : ''}" onclick="gotoGalleryPage(${p})">${p + 1}</button>`;
        return h;
    }).join('');

    pager.innerHTML = `
        <div class="pager-info">${start + 1}–${start + shown} of ${total}</div>
        <div class="pager-controls">
            <button class="pager-btn" ${cur === 0 ? 'disabled' : ''} onclick="gotoGalleryPage(${cur - 1})">‹ Prev</button>
            ${numHtml}
            <button class="pager-btn" ${cur >= totalPages - 1 ? 'disabled' : ''} onclick="gotoGalleryPage(${cur + 1})">Next ›</button>
        </div>
        <div class="pager-jump">
            <span>Page</span>
            <input type="number" class="pager-input" id="pager-jump-input" min="1" max="${totalPages}"
                   value="${cur + 1}"
                   onkeydown="if(event.key==='Enter'){gotoGalleryPage(this.value-1);}">
            <span>of ${totalPages}</span>
            <button class="pager-btn" onclick="gotoGalleryPage(document.getElementById('pager-jump-input').value-1)">Go</button>
        </div>`;
}

/* Load thumbnails 4 at a time. Primary path: presigned URL → <img src>
   (always works, no CORS issues with img tags). Cache is a best-effort
   enhancement: if S3 has CORS configured, we additionally fetch the blob
   into IndexedDB for instant load on next visit. If S3 CORS isn't set,
   no cache is built but images still display correctly. */
// v5.3: load thumbnails for a set of GLOBAL indices (the current page only), so
// data-idx / openImg / delete all keep resolving against state.galleryItems.
async function loadThumbsForIndices(indices) {
    const CONCURRENCY = 4;
    let cursor = 0;

    async function worker() {
        while (cursor < indices.length) {
            const idx = indices[cursor++];
            const it = state.galleryItems[idx];
            if (!it) continue;
            // v3.9: records with bboxes load the ORIGINAL frame (card draws its own boxes);
            // legacy records without bboxes fall back to the baked processed image.
            const hasBoxes = getDrawableBoxes(it).length > 0;
            const key = hasBoxes
                ? (it.original_image_key || it.image_id)
                : (it.processed_image_key || it.original_image_key || it.image_id);
            // If the original is missing (legacy moth), fall back to the processed image.
            const fallbackKey = hasBoxes ? (it.processed_image_key || null) : null;
            if (!key) continue;
            await loadOneThumb(idx, key, fallbackKey);
        }
    }

    const workers = Array(Math.min(CONCURRENCY, indices.length)).fill(0).map(() => worker());
    await Promise.all(workers);
    imageCache.evictIfFull();
}

async function loadOneThumb(idx, key, fallbackKey = null, attempt = 1, drawBoxes = true) {
    const cardEl = document.querySelector(`[data-idx="${idx}"]`);
    if (!cardEl) return;
    const img = cardEl.querySelector('img.lazy');
    if (!img) return;
    const it = state.galleryItems[idx];   // v3.9: needed to paint card bbox overlay
    // v4.2: stale-closure guard — a delete re-renders the grid and shifts
    // indices, so an in-flight retry's idx can now point at a DIFFERENT
    // record. Proceed only if the record at idx still owns the key this
    // loader was started for; otherwise the fresh loader owns the card.
    if (!it) return;
    if (key !== (it.original_image_key || it.image_id) && key !== it.processed_image_key) return;

    // v3.9: on success, draw boxes — UNLESS this is a processed-image fallback, in
    // which case the boxes are already baked in (drawing would double them up).
    const onLoaded = () => {
        img.classList.add('loaded');
        if (drawBoxes) {
            paintCardBoxes(cardEl, it);
        } else {
            const ov = cardEl.querySelector('.card-bbox-overlay');
            if (ov) ov.innerHTML = '';
        }
    };
    // v3.9: original frame missing (e.g. legacy moth records whose frame never
    // reached the frames bucket) → switch to the baked processed image, no boxes.
    const tryFallback = () => {
        if (fallbackKey && fallbackKey !== key) {
            loadOneThumb(idx, fallbackKey, null, 1, false);
            return true;
        }
        return false;
    };

    // 1. Cache hit → instant blob (S3 CORS was configured, prior visit cached it)
    try {
        const cached = await imageCache.get(key);
        if (cached && cached.blob) {
            img.src = URL.createObjectURL(cached.blob);
            img.onload = onLoaded;
            return;
        }
    } catch {}

    // 2. Cache miss → fetch the blob ONCE (CORS), cache it, display from that blob.
    //    Using a single CORS fetch (not <img src=url> + a separate fetch on the same
    //    url) avoids the browser caching a no-cors "opaque" img response that the
    //    fetch then can't read — which silently broke persistence.
    try {
        const r = await api.getPresignedUrl(key);
        if (!r.url) throw new Error('No presigned URL');

        let displayUrl, missing = false;
        try {
            const resp = await fetch(r.url);
            if (resp.status === 403 || resp.status === 404) { missing = true; throw new Error('missing'); }
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            const blob = await resp.blob();
            imageCache.put(key, blob);                // persist for next visit
            displayUrl = URL.createObjectURL(blob);   // display from the same blob
        } catch (e) {
            // Definitely-missing object → switch straight to the processed fallback.
            if (missing && tryFallback()) return;
            displayUrl = r.url;                       // transient/CORS → raw URL; img.onerror retries
        }

        img.src = displayUrl;
        img.onload = onLoaded;
        img.onerror = () => {
            if (attempt < 3) {
                setTimeout(() => loadOneThumb(idx, key, fallbackKey, attempt + 1, drawBoxes), 800 * attempt);
            } else if (!tryFallback()) {
                showRetryButton(cardEl, idx);
            }
        };

    } catch (err) {
        if (attempt < 3) {
            await new Promise(r => setTimeout(r, 800 * attempt));
            return loadOneThumb(idx, key, fallbackKey, attempt + 1, drawBoxes);
        }
        if (!tryFallback()) showRetryButton(cardEl, idx);
    }
}

function showRetryButton(cardEl, idx) {
    const thumb = cardEl.querySelector('.image-card-thumb');
    if (thumb && !thumb.querySelector('.retry-btn')) {
        thumb.insertAdjacentHTML('beforeend',
            `<button class="retry-btn" onclick="event.stopPropagation();retryThumb(${idx})" title="Retry loading">↻ Retry</button>`);
    }
}

export function retryThumb(idx) {
    const it = state.galleryItems[idx];
    if (!it) return;
    const cardEl = document.querySelector(`[data-idx="${idx}"]`);
    if (cardEl) {
        const oldRetry = cardEl.querySelector('.retry-btn');
        if (oldRetry) oldRetry.remove();
        const img = cardEl.querySelector('img.lazy');
        if (img) img.classList.remove('loaded');
    }
    const hasBoxes = getDrawableBoxes(it).length > 0;
    const key = hasBoxes
        ? (it.original_image_key || it.image_id)
        : (it.processed_image_key || it.original_image_key || it.image_id);
    const fallbackKey = hasBoxes ? (it.processed_image_key || null) : null;
    loadOneThumb(idx, key, fallbackKey, 1);
}

/* v4.2: permanent delete — the API removes the DDB record + S3 objects; both
   the card button and the modal route here. Confirm dialog = the second step
   of the two-step confirmation (button click is the first). Returns true on
   success so the modal knows to close. */
export async function deleteGalleryItem(idx) {
    const it = state.galleryItems[idx];
    if (!it) return false;
    const when = fmtTimeShort(it.detection_time);
    if (!confirm(`Permanently delete the ${when} capture from ${camDisplayName(it.camera_id)}?\n\n` +
        `• Removes the detection record (DynamoDB)\n` +
        `• Removes the stored image (S3)\n\nThis cannot be undone.`)) return false;
    try {
        await api.deleteDetection(it.image_id);
        // Evict every cached variant of this record's imagery
        try { await imageCache.remove(it.original_image_key || it.image_id); } catch {}
        try { if (it.processed_image_key) await imageCache.remove(it.processed_image_key); } catch {}
        // The API deletes EVERY row for this image_id (duplicates share the PK),
        // so drop every matching card, not just the clicked index.
        const removed = state.galleryItems.filter(x => x.image_id === it.image_id).length;
        state.galleryItems = state.galleryItems.filter(x => x.image_id !== it.image_id);
        renderGalleryGrid();
        lastScanned = Math.max(0, lastScanned - removed);
        updateGallerySummary({ scanned: lastScanned });
        toast('Capture deleted', 'success');
        return true;
    } catch (err) {
        toast('Delete failed: ' + err.message, 'error', 6000);
        return false;
    }
}

function galleryCard(it, idx) {
    // v4.5: "detected" = at least one box survived the processor's hybrid gate at
    // write time (getVerifiableBoxes no longer re-filters by the camera's current
    // min_confidence — see bbox.js). Raising Min confidence later does not
    // retroactively hide an old record's confirmed low-confidence detections.
    const bboxes = getVerifiableBoxes(it);
    const detected = bboxes.length > 0;
    const pcls = pestClass(it.target_label);
    const label = it.target_label || 'Unknown';
    const title = label.charAt(0).toUpperCase() + label.slice(1);
    // v4.5: no confidence in the badge — every surviving bbox already passed the
    // processor's gate. When >1 bbox, append count e.g. "Armyworm-larva · 3"
    const countSuffix = bboxes.length > 1 ? ` · ${bboxes.length}` : '';
    const badgeHtml = detected
        ? `<span class="pest-badge ${pcls}">${escapeHtml(title)}${countSuffix}</span>`
        : `<span class="pest-badge none">Clear</span>`;
    const zone = it.waypoint_id || '—';
    const time = fmtTimeShort(it.detection_time);

    // v3.6: aggregate per-bbox verdicts into a single small corner badge.
    // Verify happens in modal now (per-bbox), so no inline buttons on card.
    const agg = detected ? aggregateVerdict(it) : null;
    const verifiedBadge = agg
        ? `<div class="verified-badge ${agg.cls}" title="${escapeHtml(agg.title)}">${agg.label}</div>`
        : '';

    return `<div class="image-card" data-idx="${idx}" onclick="openImg(${idx})">
        <div class="image-card-thumb">
            <img class="lazy" alt="" src="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 4 3'/>">
            <div class="card-bbox-overlay"></div>
            <div class="image-card-badge-row">${badgeHtml}</div>
            ${verifiedBadge}
        </div>
        <div class="image-card-body">
            <div class="image-card-footer">${escapeHtml(zone)} · ${time}</div>
            <div class="image-card-meta">
                <span class="badge badge-muted">${escapeHtml(camDisplayName(it.camera_id))}</span>
                <button class="btn-ghost btn-sm card-delete-btn" title="Delete capture (permanent)"
                    onclick="event.stopPropagation();deleteGalleryItem(${idx})">Delete</button>
            </div>
        </div>
    </div>`;
}
