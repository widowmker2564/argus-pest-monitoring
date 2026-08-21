/* ==============================================================
   BBOX — box extraction/filtering, verify (TP/FP) logic, canvas
   overlay rendering, card overlay painting, and image zoom/pan.
   Split from dashboard_v3_9.html (v4.0 module split).
   Runtime-circular imports with gallery.js / modal.js are safe:
   all cross-calls happen inside functions, never at module eval.
   ============================================================== */
import { state } from './state.js';
import { api } from './api.js';
import { toast, escapeHtml } from './utils.js';
import { renderGalleryGrid } from './gallery.js';
import { refreshOpenModalReview } from './modal.js';

/* v3.6: extract per-target-label detections from labels JSON as the verifiable bbox list.
   Each entry corresponds to one bounding box drawn on the processed image.
   Sorted by confidence desc so bbox_0 = highest-conf detection.

   v4.5: no threshold re-filter here anymore. The processor's hybrid gate (Rekognition
   authoritative at/above camera.min_confidence, the LLM adjudicates below it) already
   decided which boxes survive before writing the record — a box present in `it.labels`
   IS a confirmed detection, even one below the camera's current threshold value, so
   re-filtering by that same threshold here would wrongly hide a legitimate low-confidence
   detection the LLM confirmed. */
export function getVerifiableBoxes(it) {
    let labels = [];
    try { labels = JSON.parse(it.labels || '[]'); } catch {}
    const target = (it.target_label || '').toLowerCase().trim();
    if (!target) return [];

    const matches = labels.filter(l => (l.name || '').toLowerCase().trim() === target);
    matches.sort((a, b) => (b.confidence || 0) - (a.confidence || 0));
    return matches;
}

/* v3.9: geometry-bearing box list for the modal (canvas overlay + review block).
   Source of truth = it.bboxes (structured coords from the processor), NOT it.labels.
   Filtered by the current per-camera threshold and sorted by confidence desc — the
   SAME filter+sort getVerifiableBoxes uses — so for the armyworm custom model the
   index here lines up with the review rows and with verifyMap. Each returned box
   carries normalized geometry (0-1) plus name/confidence for display.
   NOTE: analytics still use getVerifiableBoxes(it.labels) and are untouched. If a
   camera's min_confidence is lowered BELOW the value used at detection time, this
   list (frozen at detection) and the labels-based analytics list can diverge. */
export function getDrawableBoxes(it) {
    let raw = it.bboxes;
    if (typeof raw === 'string') { try { raw = JSON.parse(raw); } catch { raw = []; } }
    if (!Array.isArray(raw)) return [];

    // v4.5: no threshold re-filter — the processor's hybrid gate already decided
    // which boxes survive (see getVerifiableBoxes above for why).
    const boxes = raw.map(b => ({
        name:       b.label ?? b.name ?? (it.target_label || ''),
        confidence: b.confidence,                       // string e.g. "97.3"; kept for sort only
        confVal:    parseFloat(b.confidence) || 0,
        left:       parseFloat(b.left)   || 0,
        top:        parseFloat(b.top)    || 0,
        width:      parseFloat(b.width)  || 0,
        height:     parseFloat(b.height) || 0,
    })).filter(b => b.width > 0 && b.height > 0);        // need real geometry to draw
    boxes.sort((a, b) => b.confVal - a.confVal);
    return boxes;
}

/* v3.9: build the overlay's box DOM from getDrawableBoxes, honoring verifyMap.
   A box flagged FP is dropped (truly disappears); restore it from the review list.
   v4.5: plain box + species name only — no confidence, no verifier badge. Every box
   that reaches here already passed the processor's gate, so there is nothing left
   to annotate; a human can still flag one as a false positive with the ✕ button. */
export function renderOverlayBoxes(it) {
    const boxes = getDrawableBoxes(it);
    const verdicts = state.verifyMap[it.image_id] || {};
    const safeId = escapeHtml(it.image_id);
    return boxes.map((b, i) => {
        if (verdicts[i] === 'FP') return '';            // dismissed → not drawn
        const style = `left:${b.left * 100}%;top:${b.top * 100}%;`
                    + `width:${b.width * 100}%;height:${b.height * 100}%;`;
        return `<div class="bbox-box" style="${style}">
            <span class="bbox-label">${escapeHtml(b.name)} ${b.confVal.toFixed(0)}%</span>
            <button class="bbox-x" title="Flag false positive — removes this box"
                    onclick="verifyClick('${safeId}', ${i}, 'FP')">✕</button>
        </div>`;
    }).join('');
}

/* v3.9: re-fill the open modal's overlay boxes without touching the image element
   or its current zoom/pan transform (we only replace the overlay's innerHTML). */
export function redrawModalOverlay(image_id) {
    const modal = document.getElementById('image-modal');
    if (!modal.classList.contains('visible')) return;
    const overlay = modal.querySelector('.bbox-overlay');
    if (!overlay || overlay.dataset.imageId !== image_id) return;
    const it = state.galleryItems.find(x => x.image_id === image_id);
    if (it) overlay.innerHTML = renderOverlayBoxes(it);
}

// Opt-out counting for Analytics: a detection counts UNLESS a human marked it FP.
// (TP and unreviewed both count — CAG only has to flag false positives, not confirm
// every true one.) Index basis matches getVerifiableBoxes/renderReviewBlock exactly:
// both use the confidence-sorted verifiable-box array, and verifyMap is keyed by that index.
export function getCountedBoxes(it) {
    const boxes = getVerifiableBoxes(it);
    const verdicts = state.verifyMap[it.image_id] || {};
    return boxes.filter((b, i) => verdicts[i] !== 'FP');
}

/* v3.6: roll up per-bbox verdicts into a single corner-badge state for the gallery card. */
export function aggregateVerdict(it) {
    const verdicts = state.verifyMap[it.image_id];
    const total = getVerifiableBoxes(it).length;
    if (!verdicts || total === 0) return null;
    const entries = Object.values(verdicts);
    const reviewed = entries.length;
    if (reviewed === 0) return null;
    const tp = entries.filter(v => v === 'TP').length;
    const fp = entries.filter(v => v === 'FP').length;
    if (reviewed < total) {
        return { cls: 'partial', label: `${reviewed}/${total}`, title: `${reviewed} of ${total} reviewed` };
    }
    if (tp === total) return { cls: 'true',  label: '✓', title: 'All confirmed pest' };
    if (fp === total) return { cls: 'false', label: '✗', title: 'All false positives' };
    return { cls: 'mixed', label: `${tp}✓${fp}✗`, title: `${tp} confirmed, ${fp} false positives` };
}

/* v3.6: hydrate state.verifyMap from a backend detection record.
   Handles both new (it.verifications map) and legacy (it.verified bool) formats.
   Legacy mapping: verified=true → every target bbox gets "TP"; verified=false → every bbox "FP".
   Local in-memory edits (already in verifyMap) win over backend, so unsaved clicks aren't clobbered. */
export function hydrateVerifyMap(it) {
    if (state.verifyMap[it.image_id]) return;  // already have local state, don't overwrite
    // New format
    if (it.verifications) {
        let v = it.verifications;
        if (typeof v === 'string') { try { v = JSON.parse(v); } catch { v = null; } }
        if (v && typeof v === 'object' && !Array.isArray(v)) {
            state.verifyMap[it.image_id] = { ...v };
            return;
        }
    }
    // Legacy format: project per-image verdict onto every bbox
    const legacy = (it.verified === true || it.verified === 'true') ? 'TP'
                 : (it.verified === false || it.verified === 'false') ? 'FP'
                 : null;
    if (!legacy) return;
    const bboxes = getVerifiableBoxes(it);
    if (bboxes.length === 0) return;
    const rec = {};
    bboxes.forEach((_, i) => { rec[i] = legacy; });
    state.verifyMap[it.image_id] = rec;
}

/* v3.6: Toggle a per-bbox verification verdict. Clicking the active button clears it.
   Optimistic update with rollback on backend failure. */
export async function verifyClick(image_id, bbox_index, verdict) {
    const rec = state.verifyMap[image_id] || {};
    const cur = rec[bbox_index];
    const next = cur === verdict ? null : verdict;  // toggle off if same

    // Optimistic update
    const prevRec = { ...rec };
    if (next === null) {
        delete rec[bbox_index];
        if (Object.keys(rec).length === 0) delete state.verifyMap[image_id];
        else state.verifyMap[image_id] = rec;
    } else {
        rec[bbox_index] = next;
        state.verifyMap[image_id] = rec;
    }

    // Re-render UI
    if (state.tab === 'gallery') renderGalleryGrid();
    refreshOpenModalReview(image_id);

    // Persist
    try {
        await api.verifyDetection(image_id, bbox_index, next);
        const lbl = next === 'TP' ? '✓ Confirmed' : next === 'FP' ? '✗ False positive' : 'Cleared';
        toast(lbl, next === 'FP' ? 'info' : 'success', 1800);
    } catch (err) {
        // Revert
        if (Object.keys(prevRec).length === 0) delete state.verifyMap[image_id];
        else state.verifyMap[image_id] = prevRec;
        if (state.tab === 'gallery') renderGalleryGrid();
        refreshOpenModalReview(image_id);
        toast(`Verify failed: ${err.message}`, 'error', 4000);
    }
}

/* v3.9: draw bounding boxes on a gallery card thumbnail. The thumb uses
   object-fit:cover on a 4:3 box, so the original frame is scaled to fill and
   center-cropped. We size the overlay to that cover content rect (which may
   overflow the thumb — the thumb's overflow:hidden clips it) and place boxes as
   % of the full frame. Boxes flagged FP are skipped so the card matches the modal. */
export function paintCardBoxes(cardEl, it) {
    const overlay = cardEl.querySelector('.card-bbox-overlay');
    const img = cardEl.querySelector('img.lazy');
    if (!overlay || !img || !it) return;

    let boxes = getDrawableBoxes(it);
    const verdicts = state.verifyMap[it.image_id] || {};
    boxes = boxes.filter((b, i) => verdicts[i] !== 'FP');
    if (boxes.length === 0) { overlay.innerHTML = ''; return; }

    const tw = img.clientWidth, th = img.clientHeight;
    const nw = img.naturalWidth, nh = img.naturalHeight;
    if (!tw || !th || !nw || !nh) { overlay.innerHTML = ''; return; }

    // object-fit: cover → scale so the frame fills the thumb, overflow center-cropped.
    const scale = Math.max(tw / nw, th / nh);
    const dw = nw * scale, dh = nh * scale;
    overlay.style.left   = ((tw - dw) / 2) + 'px';
    overlay.style.top    = ((th - dh) / 2) + 'px';
    overlay.style.width  = dw + 'px';
    overlay.style.height = dh + 'px';

    overlay.innerHTML = boxes.map(b =>
        `<div class="card-bbox" style="left:${b.left * 100}%;top:${b.top * 100}%;`
        + `width:${b.width * 100}%;height:${b.height * 100}%;"></div>`
    ).join('');
}

/* v3.6.3: zoom + pan controls for the modal image.
   - Wheel: zoom toward cursor (desktop)
   - Pinch: zoom toward pinch center (mobile)
   - Double-click / double-tap: toggle 1× ↔ 2.5×
   - Drag (when zoomed): pan, clamped to image bounds
   Returns a cleanup fn that removes the window listeners. */
export function attachImageZoom(img, pane) {
    const MIN_SCALE = 1, MAX_SCALE = 6;
    let scale = 1, tx = 0, ty = 0;
    let dragging = false;
    let dragStartX = 0, dragStartY = 0, dragStartTx = 0, dragStartTy = 0;
    let pinchStartDist = 0, pinchStartScale = 1;
    let pinchStartTx = 0, pinchStartTy = 0;
    let pinchCenterX = 0, pinchCenterY = 0;

    img.style.cursor = 'zoom-in';
    img.draggable = false;
    img.style.touchAction = 'none';

    // v3.9: the bbox overlay (if present) gets the SAME transform as the image.
    const overlay = pane.querySelector('.bbox-overlay');

    const apply = () => {
        clampPan();
        const t = `translate(${tx}px, ${ty}px) scale(${scale})`;
        img.style.transform = t;
        if (overlay) {
            overlay.style.transform = t;
            // v5.4: the same transform magnifies the overlay's decoration as
            // well as its geometry, so a 2.4px border became 14px at 6x and the
            // flag button swallowed a small worm. Publishing the live scale as
            // --z lets the CSS divide every border, font, padding and offset by
            // it, holding the chrome at a constant on-screen size while the
            // worm keeps growing. Geometry stays in %, so nothing shifts.
            overlay.style.setProperty('--z', scale);
        }
        img.style.cursor = scale > 1 ? (dragging ? 'grabbing' : 'grab') : 'zoom-in';
    };

    const clampPan = () => {
        if (scale <= 1) { tx = 0; ty = 0; return; }
        const r = img.getBoundingClientRect();
        if (r.width === 0) return;
        // r already reflects current scale; overflow = (scaled - base) / 2 = base*(scale-1)/2
        const baseW = r.width / scale;
        const baseH = r.height / scale;
        const maxX = (baseW * (scale - 1)) / 2;
        const maxY = (baseH * (scale - 1)) / 2;
        tx = Math.max(-maxX, Math.min(maxX, tx));
        ty = Math.max(-maxY, Math.min(maxY, ty));
    };

    // Zoom toward a screen point (sx, sy) — keeps that point fixed on screen.
    const zoomToward = (newScale, sx, sy) => {
        newScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, newScale));
        if (newScale === scale) return;
        const r = img.getBoundingClientRect();
        if (r.width === 0) return;
        const cx = sx - (r.left + r.width / 2);
        const cy = sy - (r.top + r.height / 2);
        const ratio = newScale / scale;
        tx = cx - (cx - tx) * ratio;
        ty = cy - (cy - ty) * ratio;
        scale = newScale;
        if (scale === 1) { tx = 0; ty = 0; }
        apply();
    };

    // === Wheel zoom ===
    const onWheel = (e) => {
        e.preventDefault();
        const factor = e.deltaY > 0 ? 0.85 : 1.18;
        zoomToward(scale * factor, e.clientX, e.clientY);
    };
    img.addEventListener('wheel', onWheel, { passive: false });

    // === Double-click toggle ===
    img.addEventListener('dblclick', (e) => {
        e.preventDefault();
        if (scale > 1) { scale = 1; tx = 0; ty = 0; apply(); }
        else zoomToward(2.5, e.clientX, e.clientY);
    });

    // === Mouse pan (when zoomed) ===
    img.addEventListener('mousedown', (e) => {
        if (scale <= 1) return;
        e.preventDefault();
        dragging = true;
        dragStartX = e.clientX; dragStartY = e.clientY;
        dragStartTx = tx; dragStartTy = ty;
        apply();
    });
    const onMouseMove = (e) => {
        if (!dragging) return;
        tx = dragStartTx + (e.clientX - dragStartX);
        ty = dragStartTy + (e.clientY - dragStartY);
        apply();
    };
    const onMouseUp = () => {
        if (!dragging) return;
        dragging = false;
        apply();
    };
    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);

    // === Touch: pinch + single-finger pan ===
    img.addEventListener('touchstart', (e) => {
        if (e.touches.length === 2) {
            e.preventDefault();
            const t1 = e.touches[0], t2 = e.touches[1];
            pinchStartDist = Math.hypot(t2.clientX - t1.clientX, t2.clientY - t1.clientY);
            pinchStartScale = scale;
            pinchStartTx = tx; pinchStartTy = ty;
            pinchCenterX = (t1.clientX + t2.clientX) / 2;
            pinchCenterY = (t1.clientY + t2.clientY) / 2;
        } else if (e.touches.length === 1 && scale > 1) {
            e.preventDefault();
            dragging = true;
            const t = e.touches[0];
            dragStartX = t.clientX; dragStartY = t.clientY;
            dragStartTx = tx; dragStartTy = ty;
        }
    }, { passive: false });

    img.addEventListener('touchmove', (e) => {
        if (e.touches.length === 2 && pinchStartDist > 0) {
            e.preventDefault();
            const t1 = e.touches[0], t2 = e.touches[1];
            const dist = Math.hypot(t2.clientX - t1.clientX, t2.clientY - t1.clientY);
            // Restore to pinch-start state then compute new zoom toward pinch center
            tx = pinchStartTx; ty = pinchStartTy; scale = pinchStartScale;
            zoomToward(pinchStartScale * (dist / pinchStartDist), pinchCenterX, pinchCenterY);
        } else if (e.touches.length === 1 && dragging) {
            e.preventDefault();
            const t = e.touches[0];
            tx = dragStartTx + (t.clientX - dragStartX);
            ty = dragStartTy + (t.clientY - dragStartY);
            apply();
        }
    }, { passive: false });

    img.addEventListener('touchend', () => {
        dragging = false;
        pinchStartDist = 0;
        apply();
    });

    // Cleanup: only window listeners need removal; element listeners die with the element.
    return () => {
        window.removeEventListener('mousemove', onMouseMove);
        window.removeEventListener('mouseup', onMouseUp);
    };
}
