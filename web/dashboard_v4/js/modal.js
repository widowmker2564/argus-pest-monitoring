/* ==============================================================
   IMAGE DETAIL MODAL — open/close, meta pane, per-bbox review
   block, overlay + zoom wiring.
   Split from dashboard_v3_9.html (v4.0 module split).
   ============================================================== */
import { state, imageCache } from './state.js';
import { api } from './api.js';
import { escapeHtml, fmtTime, camDisplayName } from './utils.js';
import { getDrawableBoxes, renderOverlayBoxes, redrawModalOverlay, attachImageZoom } from './bbox.js';
import { deleteGalleryItem } from './gallery.js';

export async function openImg(idx) {
    const it = state.galleryItems[idx];
    if (!it) return;
    const modal = document.getElementById('image-modal');
    document.getElementById('modal-title').textContent = camDisplayName(it.camera_id) + ' · ' + (it.waypoint_id || '');
    document.getElementById('modal-sub').textContent = fmtTime(it.detection_time);
    const pane = document.getElementById('modal-img-pane');
    const meta = document.getElementById('modal-meta-pane');

    pane.innerHTML = '<div class="loading-wrapper" style="color:#ccc;"><span class="spinner"></span> Loading…</div>';
    let labels = [];
    try { labels = JSON.parse(it.labels || '[]'); } catch {}
    const row = (l, v) => `<div class="meta-row"><span class="meta-label">${l}</span><span class="meta-value">${escapeHtml(v ?? '—')}</span></div>`;
    // TESTING AID (not a final feature): which model version produced this
    // record. Parsed from the stored model_arn; legacy records show '—'.
    const modelVersion = (rec) => {
        const m = /\/version\/([^/]+)\//.exec(rec.model_arn || '');
        return m ? m[1] : '—';
    };
    // v5.3: which multimodal model judged the boxes on this frame. Written by
    // the processor on every row, so a model A/B stays readable here later.
    const llmVerifierName = (rec) => {
        const id = rec.llm_verify_model || '';
        if (!id) return '—';
        return id.replace(/^(us|eu|apac|global)\./, '')
                 .replace(/-v\d+:\d+$/, '')
                 .replace(/^(anthropic|amazon|meta|mistral|google|qwen)\./, '');
    };
    // v3.9: Review block + overlay both gate on getDrawableBoxes (the geometry source).
    const drawable = getDrawableBoxes(it);
    const detected = drawable.length > 0;

    // v3.6: tag meta pane so refreshOpenModalReview can find it when re-rendering a single bbox change.
    meta.dataset.imageId = it.image_id;
    meta.innerHTML = `
        ${row('Camera', camDisplayName(it.camera_id))}
        ${row('Zone', it.waypoint_id)}
        ${row('Target', it.target_label)}
        ${row('Detected', it.target_detected ? 'Yes' : 'No')}
        ${row('Model', it.model_type)}
        ${row('Verifier', llmVerifierName(it))}
        ${row('Labels', labels.length)}
        ${row('Time', fmtTime(it.detection_time))}
        ${row('Model version', modelVersion(it))}
        ${detected ? renderReviewBlock(it.image_id) : ''}
        <button class="btn btn-danger btn-sm" style="margin-top:16px;width:100%;"
            onclick="deleteOpenImage()">Delete this capture…</button>
    `;

    // v3.9: if this record has drawable bboxes, show the ORIGINAL frame and draw the
    // canvas overlay (so dismissing a box truly removes it). Legacy records without
    // bboxes (e.g. pre-v4.0 migrated rows) fall back to the baked processed image so
    // they don't lose their box visualization.
    const key = drawable.length > 0
        ? (it.original_image_key || it.image_id)
        : (it.processed_image_key || it.original_image_key || it.image_id);
    // v3.9: if the original frame is missing (legacy moth records whose frame never
    // reached the frames bucket), fall back to the baked processed image, no overlay.
    const modalFallbackKey = drawable.length > 0 ? (it.processed_image_key || null) : null;
    if (state._imgZoomCleanup) { state._imgZoomCleanup(); state._imgZoomCleanup = null; }
    try {
        // Try cache first (instant if S3 CORS is configured)
        const cached = await imageCache.get(key);
        if (cached && cached.blob) {
            pane.innerHTML = `<img src="${URL.createObjectURL(cached.blob)}" alt=""><div class="bbox-overlay" data-image-id="${escapeHtml(it.image_id)}"></div><div class="zoom-hint">scroll · pinch · dbl-click to zoom</div>`;
        } else {
            const r = await api.getPresignedUrl(key);
            // Fetch the blob ONCE (CORS), cache it, then display from that blob.
            // Avoids the no-cors <img> response getting cached as opaque and then
            // blocking the cache fetch — same fix as loadOneThumb.
            let displayUrl;
            try {
                const resp = await fetch(r.url);
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                const blob = await resp.blob();
                imageCache.put(key, blob);
                displayUrl = URL.createObjectURL(blob);
            } catch {
                displayUrl = r.url;   // CORS/fetch failed → raw URL (display only, no cache)
            }
            pane.innerHTML = `<img src="${escapeHtml(displayUrl)}" alt=""><div class="bbox-overlay" data-image-id="${escapeHtml(it.image_id)}"></div><div class="zoom-hint">scroll · pinch · dbl-click to zoom</div>`;
        }
        const imgEl = pane.querySelector('img');
        const overlayEl = pane.querySelector('.bbox-overlay');
        if (imgEl) {
            // v3.9: size/place the overlay to exactly cover the rendered image rect.
            // offset* metrics ignore CSS transforms, so this is the untransformed base
            // box; attachImageZoom then applies the same transform to img + overlay.
            const positionOverlay = () => {
                if (!overlayEl) return;
                overlayEl.style.left   = imgEl.offsetLeft   + 'px';
                overlayEl.style.top    = imgEl.offsetTop    + 'px';
                overlayEl.style.width  = imgEl.offsetWidth  + 'px';
                overlayEl.style.height = imgEl.offsetHeight + 'px';
            };
            // Attach zoom once the image element exists; clamp logic needs natural size
            // which is ready as soon as src is set for cached blobs and shortly after for network.
            let usingFallback = false;
            const init = () => {
                positionOverlay();
                if (overlayEl) overlayEl.innerHTML = usingFallback ? '' : renderOverlayBoxes(it);
                const zoomCleanup = attachImageZoom(imgEl, pane);
                const onResize = () => positionOverlay();
                window.addEventListener('resize', onResize);
                state._imgZoomCleanup = () => {
                    zoomCleanup();
                    window.removeEventListener('resize', onResize);
                };
            };
            if (imgEl.complete) init(); else imgEl.addEventListener('load', init, { once: true });

            // v3.9: original frame missing → swap to the processed image and drop the
            // overlay (processed already has baked boxes). The subsequent 'load' fires
            // init() with usingFallback=true, so no boxes are drawn over the baked image.
            if (modalFallbackKey) {
                const onImgError = async () => {
                    try {
                        const r = await api.getPresignedUrl(modalFallbackKey);
                        usingFallback = true;
                        if (overlayEl) overlayEl.innerHTML = '';
                        imgEl.src = r.url;
                    } catch {}
                };
                imgEl.addEventListener('error', onImgError, { once: true });
                if (imgEl.complete && imgEl.naturalWidth === 0) onImgError();
            }

            // Show the zoom hint briefly when modal opens (first 2.5s), then fade out.
            pane.classList.add('show-hint');
            clearTimeout(state._hintTimer);
            state._hintTimer = setTimeout(() => pane.classList.remove('show-hint'), 2500);
        }
    } catch (err) {
        pane.innerHTML = `<div class="video-overlay" style="position:static;"><div class="title">Failed to load</div><div class="msg">${escapeHtml(err.message)}</div></div>`;
    }
    modal.classList.add('visible');
}

/* v3.6: render the per-bbox Review block. Each row corresponds to one box drawn on
   the canvas overlay (v3.9: getDrawableBoxes, same filter+sort+index as the overlay),
   sorted by confidence desc so row order matches the visual prominence of the boxes. */
export function renderReviewBlock(image_id) {
    const it = state.galleryItems.find(x => x.image_id === image_id);
    if (!it) return '';
    const bboxes = getDrawableBoxes(it);
    if (bboxes.length === 0) return '';
    const verdicts = state.verifyMap[image_id] || {};
    const flagged = Object.values(verdicts).filter(v => v === 'FP').length;
    const safeId = escapeHtml(image_id);
    return `
        <div data-review-block style="margin-top:18px;padding-top:14px;border-top:1px solid var(--border);">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
                <div class="input-label" style="margin:0;">Flag false positives</div>
                <div style="font-size:11px;color:var(--muted);font-family:var(--mono);">${flagged} flagged</div>
            </div>
            <div style="display:flex;flex-direction:column;gap:6px;">
                ${bboxes.map((b, i) => {
                    const v = verdicts[i];
                    return `<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 10px;background:var(--bg);border-radius:var(--r-sm);">
                        <div style="font-family:var(--mono);font-size:11.5px;display:flex;align-items:center;gap:6px;">
                            <span style="color:var(--muted-2);">#${i + 1}</span>
                            <span style="color:var(--ink-2);">${escapeHtml(b.name)}</span>
                            <span style="color:var(--muted);">${b.confVal.toFixed(1)}%</span>
                        </div>
                        <div class="verify-btns">
                            <button class="verify-btn ${v === 'FP' ? 'active false' : ''}" onclick="verifyClick('${safeId}', ${i}, 'FP')" title="Mark false positive — removes it from the count">✗</button>
                        </div>
                    </div>`;
                }).join('')}
            </div>
        </div>`;
}

/* v3.6: re-render just the Review section of the open modal, if the image_id matches.
   Avoids re-fetching the image when a single bbox verdict changes. */
export function refreshOpenModalReview(image_id) {
    const modal = document.getElementById('image-modal');
    if (!modal.classList.contains('visible')) return;
    const meta = document.getElementById('modal-meta-pane');
    if (meta && meta.dataset.imageId === image_id) {
        const block = meta.querySelector('[data-review-block]');
        if (block) block.outerHTML = renderReviewBlock(image_id);
    }
    // v3.9: keep the canvas overlay in sync with the verdict change.
    redrawModalOverlay(image_id);
}

export function closeImageModal() {
    document.getElementById('image-modal').classList.remove('visible');
    // v3.6.3: detach window-level zoom listeners
    if (state._imgZoomCleanup) { state._imgZoomCleanup(); state._imgZoomCleanup = null; }
}

/* v4.2: delete the record currently open in the modal. Routes through
   gallery.deleteGalleryItem (confirm dialog + API call + state cleanup);
   closes the modal only if the user confirmed and the delete succeeded. */
export async function deleteOpenImage() {
    const id = document.getElementById('modal-meta-pane')?.dataset.imageId;
    const idx = state.galleryItems.findIndex(x => x.image_id === id);
    if (idx < 0) return;
    if (await deleteGalleryItem(idx)) closeImageModal();
}

// Module-level wiring (same timing as the old end-of-body script: DOM is parsed
// before deferred modules evaluate).
document.getElementById('image-modal').addEventListener('click', (e) => { if (e.target.id === 'image-modal') closeImageModal(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeImageModal(); });
