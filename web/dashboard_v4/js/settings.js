/* ==============================================================
   PAGE: SETTINGS — cameras, test upload, schedules, alerts, global;
   plus the custom-model status polling lifecycle.
   Split from dashboard_v3_9.html (v4.0 module split).
   ============================================================== */
import { CONFIG } from './config.js';
import { state } from './state.js';
import { api } from './api.js';
import {
    toast, escapeHtml, fmtTime, fmtTimeShort, todayYMD, utcToSgDate,
    cleanLabel, isManualCamera, isWormCam, needsKvs, needsSchedule, camAvatarClass,
    camInitial, statusBadge, intVal, valToSec, convertToJpg,
} from './utils.js';
import { getVerifiableBoxes, getDrawableBoxes, hydrateVerifyMap } from './bbox.js';

export async function renderSettingsPage() {
    const content = document.getElementById('page-content');
    content.innerHTML = '<div class="loading-wrapper"><span class="spinner"></span> Loading settings…</div>';

    state.settings = await api.getSettings();
    state.cameras = state.settings.cameras || {};
    try { state.modelStatuses = (await api.getModelStatus()).cameras || {}; } catch { state.modelStatuses = {}; }
    // v3.4: load schedules + camera operational stats (last detection, today count) in parallel
    try {
        const sch = await api.getSchedule();
        // Backend returns { schedules: { camera_id: { enabled, start_time, end_time, days, ... } } }
        state.schedules = sch.schedules || {};
    } catch { state.schedules = {}; }
    try {
        // Last 24h of detections is enough to compute "last detection" + "today count" per camera
        const hist = await api.getHistory({ limit: 500 });
        const items = hist.items || [];
        const ops = {};
        const todayYmd = todayYMD();
        for (const it of items) {
            const cid = it.camera_id;
            if (!cid) continue;
            const sg = utcToSgDate(it.detection_time);
            const isToday = sg && sg.toISOString().slice(0, 10) === todayYmd;
            // v3.6.2: today_count rolls up bboxes (pests sighted), not photos.
            // last_detection_time stays photo-based — it's a liveness signal, not a pest count.
            const bboxN = getVerifiableBoxes(it).length;
            const hasPests = bboxN > 0;
            if (!ops[cid]) ops[cid] = { last_detection_time: null, today_count: 0 };
            if (hasPests && isToday) ops[cid].today_count += bboxN;
            if (hasPests && (!ops[cid].last_detection_time || it.detection_time > ops[cid].last_detection_time)) {
                ops[cid].last_detection_time = it.detection_time;
            }
            // v3.6: also harvest per-bbox verify state into local cache
            hydrateVerifyMap(it);
        }
        state.cameraOps = ops;
    } catch { state.cameraOps = {}; }

    const subs = [
        { id: 'cameras',   label: 'Cameras' },
        { id: 'upload',    label: 'Test upload' },
        { id: 'schedules', label: 'Schedules' },
        { id: 'alerts',    label: 'Alerts' },
    ];
    if (state.settingsSub === 'global') state.settingsSub = 'alerts';
    content.innerHTML = `
        <div class="sub-tabs">
            ${subs.map(s => `<button class="sub-tab ${state.settingsSub === s.id ? 'active' : ''}" onclick="switchSettingsSub('${s.id}')">${s.label}</button>`).join('')}
        </div>
        <div id="settings-body"></div>
    `;
    renderSettingsSub();
}

export function switchSettingsSub(id) {
    state.settingsSub = id;
    document.querySelectorAll('.sub-tab').forEach(t => t.classList.remove('active'));
    event.currentTarget.classList.add('active');
    // Polling lifecycle: only on Cameras sub-tab
    if (id === 'cameras') startModelPolling();
    else stopModelPolling();
    renderSettingsSub();
}

async function renderSettingsSub() {
    const body = document.getElementById('settings-body');
    if (!body) return;
    if (state.settingsSub === 'cameras') {
        body.innerHTML = camerasSubMarkup();
        startModelPolling();  // ensure polling is active when cameras tab is rendered
    }
    else if (state.settingsSub === 'upload') {
        body.innerHTML = uploadSubMarkup();
        attachUploadHandlers();
    }
    else if (state.settingsSub === 'schedules') {
        body.innerHTML = '<div class="loading-wrapper"><span class="spinner"></span> Loading schedules…</div>';
        try { state.scheduleLogs = (await api.getScheduleLogs(30)).logs || []; } catch {}
        body.innerHTML = schedulesSubMarkup();
    }
    else if (state.settingsSub === 'alerts') {
        body.innerHTML = '<div class="loading-wrapper"><span class="spinner"></span> Loading subscribers…</div>';
        try { state.identities = (await api.getIdentities()).emails || []; } catch {}
        body.innerHTML = alertsSubMarkup();
    }
}

function camerasSubMarkup() {
    // Manual upload moved to its own "Test upload" sub-tab — exclude from deployed cameras grid
    const entries = Object.entries(state.cameras).filter(([id]) => !isManualCamera(id));
    if (entries.length === 0) {
        return '<div class="empty-state"><h3>No deployed cameras</h3><p>Add a camera in your AWS DynamoDB system-config to see it here.</p></div>';
    }
    // Card order: primary project cameras first, free-mode/general cameras last.
    const cameraRank = ([id, c]) =>
        c.model_type === 'custom' ? (isWormCam(id) ? 0 : 1) : 2;
    entries.sort((a, b) => cameraRank(a) - cameraRank(b));
    return `<div class="cam-grid">${entries.map(([id, c]) => cameraCardMarkup(id, c)).join('')}</div>`;
}

/* ==============================================================
   v3.4 SUB-TAB: TEST UPLOAD
   Lets the user drag-drop or browse a JPG, pick which detection model to invoke,
   override min confidence, and run it through the full S3 → Lambda → DynamoDB
   pipeline. Result is shown inline (annotated image + label list).

   Routing: the file is uploaded to frames/{chosen_camera_id}/manual_test/{ts}_{name}.jpg
   so the existing image-detection-handler picks up the right camera config (model
   type, target label, ARN). No backend changes required for this feature.
   ============================================================== */
function uploadSubMarkup() {
    // Self-heal a stale selection (e.g. after a camera_id migration): if the
    // remembered camera no longer exists, fall back to the first deployed one.
    if (!state.cameras[state.uploadCam]) {
        const first = Object.keys(state.cameras).find(id => !isManualCamera(id));
        if (first) state.uploadCam = first;
    }
    // Build camera options from deployed cameras (exclude manual_upload itself)
    const camOpts = Object.entries(state.cameras)
        .filter(([id]) => !isManualCamera(id))
        .map(([id, c]) => {
            const friendly = cleanLabel(c.label) || id;
            const modeNote = c.model_type === 'custom' ? 'custom model' : 'free general detection';
            return `<option value="${escapeHtml(id)}" ${state.uploadCam === id ? 'selected' : ''}>${escapeHtml(friendly)} — ${modeNote}</option>`;
        })
        .join('');

    return `<div style="max-width:760px;">
        <p style="color:var(--muted);margin-bottom:18px;font-size:13.5px;">
            Runs the image through the live detection pipeline.
        </p>

        <div style="display:flex;gap:14px;margin-bottom:18px;">
            <div style="flex:1;">
                <div class="input-label" style="margin-bottom:6px;">Run with</div>
                <select class="select" id="upload-cam-select" onchange="state.uploadCam = this.value; refreshUploadModelHint();">
                    ${camOpts || '<option value="" disabled>No cameras configured</option>'}
                </select>
                <div class="input-sub" id="upload-model-hint" style="margin-top:6px;font-size:12px;color:var(--muted);"></div>
            </div>
            <div style="width:250px;">
                <div class="input-label" style="margin-bottom:6px;">AI model</div>
                <select class="select" id="upload-llm" onchange="state.uploadModel = this.value;">
                    <option value="sonnet46" ${(state.uploadModel || 'sonnet46') === 'sonnet46' ? 'selected' : ''}>Claude Sonnet 4.6 &mdash; $20 / 1M tokens</option>
                    <option value="haiku45" ${state.uploadModel === 'haiku45' ? 'selected' : ''}>Claude Haiku 4.5 &mdash; $5 / 1M tokens</option>
                </select>
                <div class="input-sub" style="margin-top:6px;font-size:12px;color:var(--muted);">3 photos/day &asymp; $5.40 Sonnet &middot; $1.35 Haiku</div>
            </div>
        </div>

        <div class="drop-zone" id="upload-drop-zone">
            <div class="drop-zone-icon">📁</div>
            <div class="drop-zone-title" id="upload-drop-title">Drop an image here, or click to browse</div>
            <div class="drop-zone-sub" id="upload-drop-sub">JPG, PNG, HEIC, WebP — max 20 MB</div>
            <input type="file" id="upload-file-input" accept="image/*" style="display:none;">
        </div>
        <div id="upload-error" class="drop-zone-error"></div>

        <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;">
            <button class="btn btn-outline btn-sm" id="upload-clear-btn" onclick="resetUpload()" style="display:none;">Clear</button>
            <button class="btn btn-primary btn-sm" id="upload-go-btn" onclick="submitUpload()" disabled>Run detection</button>
        </div>

        <div id="upload-result"></div>
    </div>`;
}

function uploadModelReady() {
    const cam = state.cameras[state.uploadCam];
    if (!cam) return false;
    if (cam.model_type !== 'custom') return true;   // general detection — no model to start
    return state.modelStatuses[state.uploadCam]?.status === 'RUNNING';
}

export async function refreshUploadModelHint() {
    const cam = state.cameras[state.uploadCam];
    const hint = document.getElementById('upload-model-hint');
    if (!hint || !cam) return;
    // For custom cameras pull a fresh status so the gate is accurate even when
    // the user hasn't opened the Cameras tab this session.
    if (cam.model_type === 'custom') {
        try { state.modelStatuses = (await api.getModelStatus()).cameras || state.modelStatuses; } catch {}
    }
    const status = state.modelStatuses[state.uploadCam]?.status;
    if (cam.model_type === 'custom') {
        if (status === 'RUNNING') hint.innerHTML = '✓ Custom model is live — ready to run';
        else hint.innerHTML = `<span style="color:var(--danger);">⚠ Custom model is not live (${status || 'unknown'}). Start it from Cameras tab first, or pick a free-detection camera.</span>`;
    } else {
        hint.textContent = 'Uses free general-purpose detection (always available)';
    }
    // Gate the Run button: enable only when a file is staged AND the model is ready.
    const goBtn = document.getElementById('upload-go-btn');
    if (goBtn) goBtn.disabled = !(state.uploadFile && uploadModelReady());
}

function attachUploadHandlers() {
    refreshUploadModelHint();
    const dz = document.getElementById('upload-drop-zone');
    const fileInput = document.getElementById('upload-file-input');
    if (!dz || !fileInput) return;

    dz.onclick = () => fileInput.click();
    fileInput.onchange = (e) => handleUploadFile(e.target.files[0]);

    ['dragenter', 'dragover'].forEach(ev => dz.addEventListener(ev, (e) => {
        e.preventDefault(); e.stopPropagation(); dz.classList.add('is-drag');
    }));
    ['dragleave', 'drop'].forEach(ev => dz.addEventListener(ev, (e) => {
        e.preventDefault(); e.stopPropagation(); dz.classList.remove('is-drag');
    }));
    dz.addEventListener('drop', (e) => {
        const f = e.dataTransfer?.files?.[0];
        if (f) handleUploadFile(f);
    });
}

function handleUploadFile(file) {
    const errEl = document.getElementById('upload-error');
    const titleEl = document.getElementById('upload-drop-title');
    const subEl = document.getElementById('upload-drop-sub');
    const goBtn = document.getElementById('upload-go-btn');
    const clearBtn = document.getElementById('upload-clear-btn');
    if (errEl) errEl.textContent = '';

    if (!file) return;
    // Accept any image MIME type — convertToJpg() will normalise later.
    // Catch obvious non-images early (PDFs, videos, archives).
    if (!file.type.startsWith('image/')) {
        if (errEl) errEl.textContent = `✗ "${file.name}" is not an image file (got type: ${file.type || 'unknown'}).`;
        state.uploadFile = null;
        if (goBtn) goBtn.disabled = true;
        return;
    }
    // Pre-conversion size cap — generous because PNG/HEIC compress hard to JPG.
    if (file.size > 20 * 1024 * 1024) {
        if (errEl) errEl.textContent = `✗ File is ${(file.size / 1024 / 1024).toFixed(1)} MB, exceeds 20 MB limit.`;
        state.uploadFile = null;
        if (goBtn) goBtn.disabled = true;
        return;
    }
    state.uploadFile = file;
    const isJpeg = file.type === 'image/jpeg' || file.type === 'image/jpg';
    const willConvert = !isJpeg;
    if (titleEl) titleEl.textContent = `📎 ${file.name}`;
    if (subEl) {
        const sizeKB = (file.size / 1024).toFixed(1);
        subEl.textContent = willConvert
            ? `${sizeKB} KB · will convert to JPG · ready to run`
            : `${sizeKB} KB · ready to run`;
    }
    if (goBtn) goBtn.disabled = !uploadModelReady();
    if (clearBtn) clearBtn.style.display = 'inline-flex';
}

export function resetUpload() {
    state.uploadFile = null;
    document.getElementById('upload-file-input').value = '';
    document.getElementById('upload-drop-title').textContent = 'Drop an image here, or click to browse';
    document.getElementById('upload-drop-sub').textContent = 'JPG, PNG, HEIC, WebP — max 20 MB';
    document.getElementById('upload-error').textContent = '';
    document.getElementById('upload-go-btn').disabled = true;
    document.getElementById('upload-clear-btn').style.display = 'none';
    document.getElementById('upload-result').innerHTML = '';
}

export async function submitUpload() {
    let file = state.uploadFile;
    const cam = state.uploadCam;
    if (!file || !cam) return;
    if (!uploadModelReady()) {
        toast('Start the model first (Cameras tab), or pick a free-detection camera.', 'error');
        return;
    }

    const goBtn = document.getElementById('upload-go-btn');
    const resultEl = document.getElementById('upload-result');
    goBtn.disabled = true;

    // 0. Convert to JPG if needed (PNG/HEIC/WebP/etc).
    //    Rekognition Custom Labels accepts JPG and PNG, but the upload
    //    pipeline normalises everything to JPG for consistency.
    const needsConversion = !(file.type === 'image/jpeg' || file.type === 'image/jpg');
    if (needsConversion) {
        goBtn.innerHTML = '<span class="spinner" style="width:11px;height:11px;margin-right:6px;"></span> Converting…';
        resultEl.innerHTML = `<div class="upload-result-card"><div style="color:var(--muted);font-size:13px;"><span class="spinner" style="width:11px;height:11px;margin-right:6px;"></span> Converting ${escapeHtml(file.name)} to JPG…</div></div>`;
        try {
            const originalSize = file.size;
            file = await convertToJpg(file);
            console.log(`[upload] converted ${(originalSize/1024).toFixed(1)} KB → ${(file.size/1024).toFixed(1)} KB JPG`);
        } catch (err) {
            toast(`Conversion failed: ${err.message}`, 'error', 5000);
            resultEl.innerHTML = `<div class="upload-result-card" style="border-color:var(--danger);">
                <div style="color:var(--danger);font-weight:500;font-size:13px;">✗ ${escapeHtml(err.message)}</div>
            </div>`;
            goBtn.disabled = false;
            goBtn.innerHTML = 'Run detection';
            return;
        }
    }

    goBtn.innerHTML = '<span class="spinner" style="width:11px;height:11px;margin-right:6px;"></span> Uploading…';
    resultEl.innerHTML = `<div class="upload-result-card"><div style="color:var(--muted);font-size:13px;"><span class="spinner" style="width:11px;height:11px;margin-right:6px;"></span> Uploading to S3…</div></div>`;

    // Build a deterministic key the Lambda will route to the chosen camera config.
    // If the user set a Min confidence override, embed it in the waypoint segment as
    //   manual_test__conf{N}
    // The image-detection-handler Lambda parses this suffix and overrides
    // cam_config['min_confidence'] for just this one detection run. No DynamoDB
    // mutation, no race conditions, no cleanup needed.
    const ts = new Date().toISOString().replace(/[:.]/g, '-');
    const safeName = file.name.replace(/[^a-zA-Z0-9._-]/g, '_');
    // conf10 is the validated candidate floor of the production pipeline; the
    // user-facing threshold is the per-camera AI filter applied after the AI
    // check. __llm- picks the verification model for this run only.
    const llm = state.uploadModel || 'sonnet46';
    const waypoint = `manual_test__conf10__llm-${llm}`;
    const key = `frames/${cam}/${waypoint}/${ts}_${safeName}`;

    try {
        // 1. Get presigned URL from backend. v3.4.4 change: backend now returns
        //    generate_presigned_post format for uploads: {url, fields, method:"POST"}.
        //    Old PUT path is kept as a fallback for backward compatibility in case
        //    backend hasn't been updated yet.
        const presigned = await api.getPresignedUploadUrl(key);

        // 2. Upload file to S3.
        let putResp;
        if (presigned.fields && presigned.method === 'POST') {
            // Modern POST + FormData (AWS-recommended for browser uploads)
            const formData = new FormData();
            for (const [name, value] of Object.entries(presigned.fields)) {
                formData.append(name, value);
            }
            formData.append('file', file);   // file MUST be the last field
            putResp = await fetch(presigned.url, {
                method: 'POST',
                body: formData,
            });
        } else {
            // Legacy PUT path (backward compatibility)
            putResp = await fetch(presigned.url, {
                method: 'PUT',
                body: file,
            });
        }
        if (!putResp.ok) throw new Error(`S3 upload failed: HTTP ${putResp.status}`);

        toast(`Uploaded ${file.name} — waiting for detection…`, 'info', 3000);
        resultEl.innerHTML = `<div class="upload-result-card">
            <div style="color:var(--muted);font-size:13px;"><span class="spinner" style="width:11px;height:11px;margin-right:6px;"></span> Detection running… typically 5–15 s</div>
        </div>`;

        // 3. Poll /history for the detection result matching this image_id (v3.7 — was WebSocket)
        const detection = await waitForDetection(key, 60000);
        renderUploadResult(detection, file, cam);
    } catch (err) {
        toast(`Upload failed: ${err.message}`, 'error', 5000);
        resultEl.innerHTML = `<div class="upload-result-card" style="border-color:var(--danger);">
            <div style="color:var(--danger);font-weight:500;font-size:13px;">✗ ${escapeHtml(err.message)}</div>
        </div>`;
    } finally {
        goBtn.disabled = false;
        goBtn.innerHTML = 'Run detection';
    }
}

// v3.7: WebSocket removed — poll /history for detection result instead.
// Lambda typically writes the detection record within 5–15 s; we poll every 3 s up to 60 s.
function waitForDetection(image_id, timeoutMs = 60000) {
    return new Promise((resolve, reject) => {
        const startTime = Date.now();
        const pollInterval = 3000;
        const poll = async () => {
            if (Date.now() - startTime > timeoutMs) {
                reject(new Error('Detection result timeout (60s) — check CloudWatch for Lambda errors'));
                return;
            }
            try {
                const res = await api.getHistory({ limit: 10 });
                const match = (res.items || []).find(it => it.image_id === image_id);
                if (match) { resolve(match); return; }
            } catch (err) {
                console.warn('[waitForDetection] poll failed:', err);
            }
            setTimeout(poll, pollInterval);
        };
        // First poll after 2 s to let Lambda have time to write
        setTimeout(poll, 2000);
    });
}

async function renderUploadResult(det, file, cam) {
    const detected = det.target_detected === true;
    const conf = det.target_confidence;
    const resultEl = document.getElementById('upload-result');
    const camLabel = cleanLabel(state.cameras[cam]?.label) || cam;

    let imgHtml = '<div style="padding:30px;text-align:center;color:#888;font-size:13px;">No annotated image (target not detected)</div>';
    // v3.9: prefer original frame + drawn boxes (matches the new no-processed-image
    // pipeline); fall back to the baked processed image only for legacy records.
    const boxes = getDrawableBoxes(det).filter((b, i) => (state.verifyMap[det.image_id] || {})[i] !== 'FP');
    const useOriginal = boxes.length > 0;
    const imgKey = useOriginal
        ? (det.original_image_key || det.image_id)
        : (det.processed_image_key || det.original_image_key || null);
    if (imgKey) {
        try {
            const r = await api.getPresignedUrl(imgKey);
            if (useOriginal) {
                const boxesHtml = boxes.map(b =>
                    `<div class="ur-bbox" style="left:${b.left * 100}%;top:${b.top * 100}%;`
                    + `width:${b.width * 100}%;height:${b.height * 100}%;">`
                    + `<span class="ur-bbox-label">${escapeHtml(b.name)} ${b.confidence}%</span></div>`
                ).join('');
                imgHtml = `<span class="ur-stage"><img src="${escapeHtml(r.url)}" alt="Detection result">`
                        + `<span class="ur-bbox-overlay">${boxesHtml}</span></span>`;
            } else {
                imgHtml = `<img src="${escapeHtml(r.url)}" alt="Detection result">`;
            }
        } catch (e) { /* fall back */ }
    }

    resultEl.innerHTML = `<div class="upload-result-card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <div>
                <div style="font-size:14px;font-weight:600;">${detected ? '✓ Target detected' : '— Target not detected'}</div>
                <div style="font-size:12px;color:var(--muted);margin-top:2px;">
                    ${escapeHtml(camLabel)} · target ${escapeHtml(det.target_label || '—')}
                    ${detected ? ` · ${conf}% confidence` : ''}
                </div>
            </div>
            <span class="badge badge-${detected ? 'success' : 'muted'}">${detected ? 'Detected' : 'Clear'}</span>
        </div>
        <div class="upload-result-img-wrap">${imgHtml}</div>
        <div style="margin-top:12px;font-size:12px;color:var(--muted);">
            Saved to detection history. ${det.label_count || 0} label(s) returned.
        </div>
    </div>`;
}

/* Renders just the status badge + Start/Stop buttons (header right side).
   Called by both initial markup and patchCameraStatusUI() for live updates. */
function cameraStatusActionsHtml(id, cam) {
    const status = state.modelStatuses[id] || { status: 'UNKNOWN' };
    const isCustom = cam.model_type === 'custom';
    const arn = cam.custom_model_arn || '';
    const arnSet = arn && !arn.startsWith('REPLACE_');
    const isRunning = status.status === 'RUNNING';
    const isPending = MODEL_PENDING_STATES.has(status.status);
    const startDisabled = isPending || isRunning;
    const stopDisabled  = isPending || (status.status === 'STOPPED' || status.status === 'NOT_FOUND' || status.status === 'TRAINING_COMPLETED');

    let actionButtons = '';
    if (isCustom && arnSet) {
        actionButtons = `
            <button class="btn btn-outline btn-sm" onclick="modelStart('${escapeHtml(id)}')" ${startDisabled ? 'disabled' : ''}>
                ${status.status === 'STARTING' ? '<span class="spinner" style="width:10px;height:10px;margin-right:4px;"></span>' : '▶'} Start
            </button>
            <button class="btn btn-outline btn-sm" onclick="modelStop('${escapeHtml(id)}')" ${stopDisabled ? 'disabled' : ''}>
                ${status.status === 'STOPPING' ? '<span class="spinner" style="width:10px;height:10px;margin-right:4px;"></span>' : '■'} Stop
            </button>
        `;
    }
    return `${statusBadge(status.status)}${actionButtons}`;
}

function cameraStatusHintHtml(id, cam) {
    const status = state.modelStatuses[id] || { status: 'UNKNOWN' };
    let hint = '';
    if (status.status === 'STARTING') hint = '⏳ Starting up · ready in ~5 min';
    else if (status.status === 'STOPPING') hint = '⏳ Shutting down · ~1 min';
    else if (status.status === 'RUNNING') hint = '✓ Detection live';
    else if (status.status === 'STOPPED') hint = 'Idle · click Start to begin detection';
    else if (status.status === 'FAILED') hint = `✗ ${(status.message || 'Failed').slice(0, 60)}`;
    else if (status.status === 'TRAINING_COMPLETED') hint = 'Ready · click Start to begin detection';
    else if (status.status === 'NOT_CONFIGURED') hint = 'Using free general-purpose detection';
    else if (status.status === 'NOT_FOUND') hint = 'Setup incomplete — contact admin';
    if (!hint) return '';
    return `<div style="font-size:11.5px;color:var(--muted);padding:0 0 12px;">${escapeHtml(hint)}</div>`;
}

/* Surgical UI update: only refresh the status badge + buttons + hint + schedule
   toggle lock. Inputs, ARN, KVS field, and any focused element are NEVER touched. */
function patchCameraStatusUI() {
    if (state.tab !== 'settings' || state.settingsSub !== 'cameras') return;
    for (const [id, cam] of Object.entries(state.cameras)) {
        const actionsEl = document.querySelector(`[data-status-actions="${CSS.escape(id)}"]`);
        if (actionsEl) actionsEl.innerHTML = cameraStatusActionsHtml(id, cam);
        const hintEl = document.querySelector(`[data-status-hint="${CSS.escape(id)}"]`);
        if (hintEl) hintEl.innerHTML = cameraStatusHintHtml(id, cam);
        // v3.4: lock schedule toggle during STARTING/STOPPING
        const schedEl = document.getElementById(`cam-${id}-sched-toggle`);
        if (schedEl) {
            const status = state.modelStatuses[id]?.status;
            const isPending = status === 'STARTING' || status === 'STOPPING';
            schedEl.disabled = !needsSchedule(id, cam) || isPending;
        }
    }
}

function cameraCardMarkup(id, cam) {
    const showWaypoint = needsKvs(id);
    const showKvs = needsKvs(id);
    const ops = state.cameraOps?.[id] || {};
    const lastTime = ops.last_detection_time ? fmtTimeShort(ops.last_detection_time) : '—';
    const todayCount = ops.today_count ?? '—';
    const sched = state.schedules?.[id];
    const schedEnabled = sched?.enabled === true;
    // Lock auto-schedule during model state transitions (Issue 2: STARTING/STOPPING)
    const status = state.modelStatuses[id]?.status;
    const isPending = status === 'STARTING' || status === 'STOPPING';
    const schedDisabled = !needsSchedule(id, cam) || isPending;

    return `<div class="cam-card-v2" data-camera-id="${escapeHtml(id)}">
        <div class="cam-card-head-v2">
            <div class="cam-avatar ${camAvatarClass(id)}">${camInitial(id)}</div>
            <div style="flex:1;min-width:0;">
                <div class="cam-name-v2">${escapeHtml(cleanLabel(cam.label) || id)}</div>            </div>
            <div class="cam-actions-v2" data-status-actions="${escapeHtml(id)}">
                ${cameraStatusActionsHtml(id, cam)}
            </div>
        </div>
        <div data-status-hint="${escapeHtml(id)}">
            ${cameraStatusHintHtml(id, cam)}
        </div>

        <!-- Operational tier (always visible) -->
        <div class="cam-ops-row">
            <div class="cam-ops-cell">
                <div class="cam-ops-label">Last detection</div>
                <div class="cam-ops-value mono">${escapeHtml(lastTime)}</div>
            </div>
            <div class="cam-ops-cell">
                <div class="cam-ops-label">Today</div>
                <div class="cam-ops-value mono">${escapeHtml(String(todayCount))}</div>
            </div>
            <div class="cam-ops-cell" ${schedDisabled ? 'style="opacity:0.5;"' : ''}>
                <div class="cam-ops-label">Auto-schedule</div>
                <label class="toggle" style="margin-top:2px;">
                    <input type="checkbox" id="cam-${id}-sched-toggle" ${schedEnabled ? 'checked' : ''} ${schedDisabled ? 'disabled' : ''}
                           onchange="toggleScheduleQuick('${escapeHtml(id)}', this.checked)">
                    <span class="toggle-slider"></span>
                </label>
            </div>
        </div>

        <!-- Detection settings (collapsible) -->
        <div class="cam-collapsible" id="cam-${id}-coll">
            <div class="cam-collapsible-toggle" onclick="document.getElementById('cam-${id}-coll').classList.toggle('open')">
                <span class="arr">▸</span> Detection settings
            </div>
            <div class="cam-collapsible-body">
                <div class="field-row">
                    <div class="field-label">AI filter threshold</div>
                    <div class="conf-input-wrap">
                        <input type="number" class="conf-input" id="cam-${id}-conf" min="0" max="100" step="1" value="${cam.post_verify_floor ?? 33}"
                               oninput="debouncedSaveCamera('${escapeHtml(id)}')">
                        <span class="conf-input-suffix">%</span>
                    </div>
                </div>
                ${cam.model_type === 'custom' ? `<div class="field-row">
                    <div class="field-label">AI model
                        <div class="field-sub">Checks every detection on this camera</div>
                    </div>
                    <select class="select" id="cam-${escapeHtml(id)}-llm"
                            style="max-width:260px;"
                            onchange="debouncedSaveCamera('${escapeHtml(id)}')">
                        <option value="sonnet46" ${(cam.llm_model_id || 'sonnet46') === 'sonnet46' ? 'selected' : ''}>Claude Sonnet 4.6 &mdash; $20 / 1M tokens</option>
                        <option value="haiku45" ${cam.llm_model_id === 'haiku45' ? 'selected' : ''}>Claude Haiku 4.5 &mdash; $5 / 1M tokens</option>
                    </select>
                </div>
                <div class="field-row">
                    <div class="field-label">Zoom scan
                        <div class="field-sub">Split &amp; zoom in to catch small pests</div>
                    </div>
                    <label class="toggle" style="margin-top:2px;">
                        <input type="checkbox" id="cam-${escapeHtml(id)}-tiling" ${cam.tiling_enabled ? 'checked' : ''}
                               onchange="toggleTiling('${escapeHtml(id)}', this.checked)">
                        <span class="toggle-slider"></span>
                    </label>
                </div>` : ''}
                <div class="cam-save-indicator" id="cam-${id}-save" data-state="idle"></div>
            </div>
        </div>
    </div>`;
}

function schedulesSubMarkup() {
    const scheduleCams = Object.entries(state.cameras).filter(([id, c]) => needsSchedule(id, c));
    if (scheduleCams.length === 0) {
        return `<div class="empty-state"><h3>No scheduled-capable cameras</h3><p>Only cameras with a custom model ARN benefit from scheduling (endpoints cost $4/hr, so auto start/stop saves money). Configure a custom model under Cameras first.</p></div>`;
    }
    return `
        ${scheduleCams.map(([id, c]) => scheduleCardMarkup(id, c)).join('')}

        <div class="card" style="margin-top:24px;">
            <div class="card-head">
                <div>
                    <div class="card-title">Execution log</div>
                                    </div>
                <button class="btn btn-outline btn-sm" onclick="refreshScheduleLogs()">⟳ Refresh</button>
            </div>
            <div id="schedule-logs-body">${scheduleLogsMarkup()}</div>
        </div>
    `;
}

function scheduleCardMarkup(id, cam) {
    const s = cam.schedule || { enabled: false, start_time: '05:40', days: ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'] };
    const selected = new Set(s.days || []);
    const days = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
    return `<div class="schedule-card" data-camera="${escapeHtml(id)}">
        <div class="schedule-card-head">
            <div class="cam-avatar ${camAvatarClass(id)}">${camInitial(id)}</div>
            <div style="flex:1;">
                <div class="cam-name-v2">${escapeHtml(cleanLabel(cam.label) || id)}</div>            </div>
            <label class="toggle">
                <input type="checkbox" id="sch-${id}-enabled" ${s.enabled ? 'checked' : ''}>
                <span class="toggle-slider"></span>
            </label>
        </div>
        <div class="schedule-body-grid">
            <div>
                <div class="input-label">Start</div>
                <input type="time" class="input" id="sch-${id}-start" value="${escapeHtml(s.start_time || '05:40')}">
                <div class="input-sub" style="margin-top:6px;font-size:12px;color:var(--muted);">Starts the model, runs one detection round, shuts itself down.</div>
            </div>
        </div>
        <div>
            <div class="input-label">Active days</div>
            <div class="day-row" id="sch-${id}-days">
                ${days.map(d => `<span class="chip ${selected.has(d) ? 'active' : ''}" onclick="toggleChip(this)">${d}</span>`).join('')}
            </div>
            <div style="display:flex;gap:6px;margin-top:8px;">
                <button class="btn-ghost btn-sm" onclick="setDays('sch-${id}-days', ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'])">Every day</button>
                <button class="btn-ghost btn-sm" onclick="setDays('sch-${id}-days', ['Mon','Tue','Wed','Thu','Fri'])">Weekdays</button>
            </div>
        </div>
        <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;padding-top:14px;border-top:1px solid var(--border);">
            <button class="btn btn-danger btn-sm" onclick="deleteSchedule('${escapeHtml(id)}')">Delete</button>
            <button class="btn btn-primary btn-sm" onclick="saveSchedule('${escapeHtml(id)}')">Save</button>
        </div>
    </div>`;
}

export function toggleChip(el) { el.classList.toggle('active'); }
export function setDays(containerId, days) {
    const set = new Set(days);
    document.querySelectorAll(`#${containerId} .chip`).forEach(chip => {
        chip.classList.toggle('active', set.has(chip.textContent.trim()));
    });
}

function scheduleLogsMarkup() {
    if (!state.scheduleLogs || state.scheduleLogs.length === 0) {
        return '<div style="color:var(--muted);font-size:13px;padding:10px 0;">No executions yet.</div>';
    }
    return `<div style="overflow-x:auto;"><table class="logs-table">
        <thead><tr><th>Time</th><th>Camera</th><th>Action</th><th>Result</th><th>Message</th></tr></thead>
        <tbody>${state.scheduleLogs.map(l => `<tr>
            <td>${fmtTime(l.timestamp)}</td>
            <td>${escapeHtml(l.camera_id)}</td>
            <td><span class="badge ${l.action === 'start' ? 'badge-success' : 'badge-muted'}">${escapeHtml(l.action)}</span></td>
            <td><span class="badge ${l.result === 'SUCCESS' ? 'badge-success' : l.result === 'FAILED' ? 'badge-danger' : 'badge-warning'}">${escapeHtml(l.result)}</span></td>
            <td style="color:var(--muted);">${escapeHtml((l.message || l.error || '').slice(0, 100))}</td>
        </tr>`).join('')}</tbody>
    </table></div>`;
}

function alertsSubMarkup() {
    const list = state.identities;
    const rows = list.map(e => {
        const isPending = e.verification_status === 'Pending';
        const isSuccess = e.verification_status === 'Success';
        return `<div class="identity-row">
            <div class="identity-email">${escapeHtml(e.email)}</div>
            ${e.is_primary ? '<span class="badge badge-teal">Primary</span>' : ''}
            <span class="badge ${isSuccess ? 'badge-success' : isPending ? 'badge-warning' : 'badge-muted'}">
                ${escapeHtml(e.verification_status || '—')}
            </span>
            ${isPending ? `<button class="btn btn-outline btn-sm" onclick="resendVerification('${escapeHtml(e.email)}')" title="Re-send the AWS verification email">↻ Resend</button>` : ''}
            ${!e.is_primary ? `<button class="btn btn-danger btn-sm" onclick="removeIdentity('${escapeHtml(e.email)}')">Remove</button>` : ''}
        </div>`;
    }).join('');

    return `<div class="card">
        <div class="card-head">
            <div>
                <div class="card-title">Email subscribers</div>
                <div class="card-sub">Verified addresses receive detection alerts</div>
            </div>
            <button class="btn btn-outline btn-sm" onclick="refreshIdentities()">⟳ Refresh status</button>
        </div>
        <div class="input-group" style="margin-bottom:14px;">
            <input type="email" class="input" id="new-email" placeholder="Add subscriber…" onkeydown="if(event.key==='Enter')addIdentity()">
            <button class="btn btn-primary" onclick="addIdentity()">Add</button>
        </div>
        ${list.length === 0 ? '<div style="color:var(--muted);font-size:13px;">No subscribers yet.</div>' : rows}

        <div style="margin-top:14px;font-size:12.5px;color:var(--muted);">
            New subscribers must click the link in the AWS confirmation email before alerts reach them.
        </div>
    </div>`;
}

/* Save settings then re-fetch and verify backend actually changed.
   Toast confirms specific fields the backend returned. */
/* v3.4: Auto-save with 0.5s debounce. Each call resets the timer; the actual
   save runs once the user has been quiet for 500ms. The save still goes through
   the same backend round-trip + verify-by-re-fetch contract. */
const _saveDebounceTimers = {};
export function debouncedSaveCamera(id) {
    clearTimeout(_saveDebounceTimers[id]);
    const indicator = document.getElementById(`cam-${id}-save`);
    if (indicator) indicator.dataset.state = 'idle';
    _saveDebounceTimers[id] = setTimeout(() => saveCameraSettings(id), 500);
}

async function saveCameraSettings(id) {
    const indicator = document.getElementById(`cam-${id}-save`);
    if (indicator) indicator.dataset.state = 'saving';

    const fields = {};
    const labelEl = document.getElementById(`cam-${id}-label`);
    if (labelEl) fields.label = labelEl.value;
    const targetEl = document.getElementById(`cam-${id}-target`);
    if (targetEl) fields.target_label = targetEl.value;
    const confEl = document.getElementById(`cam-${id}-conf`);
    if (confEl) fields.post_verify_floor = parseInt(confEl.value, 10);
    // v6.3: per-camera AI verification model. Only rendered for custom-model
    // cameras, so the element is absent (and the field untouched) otherwise.
    const llmEl = document.getElementById(`cam-${id}-llm`);
    if (llmEl) fields.llm_model_id = llmEl.value;
    const wpEl = document.getElementById(`cam-${id}-waypoint`);
    if (wpEl) fields.default_waypoint_id = wpEl.value || null;
    const kvsEl = document.getElementById(`cam-${id}-kvs`);
    if (kvsEl) fields.kvs_stream_name = kvsEl.value || null;

    try {
        await api.postSettings({ camera_id: id, fields });
        // Verify by re-fetching
        const fresh = await api.getSettings();
        const updated = fresh.cameras?.[id];
        if (!updated) throw new Error('Camera missing from backend after save');
        const mismatches = [];
        for (const [k, v] of Object.entries(fields)) {
            if (v == null || v === '') continue;
            const got = updated[k];
            if (k === 'min_confidence' || k === 'post_verify_floor') {
                if (parseInt(got, 10) !== v) mismatches.push(k);
            } else if (String(got) !== String(v)) {
                mismatches.push(k);
            }
        }
        state.settings = fresh;
        state.cameras = fresh.cameras || {};
        if (mismatches.length === 0) {
            if (indicator) {
                indicator.dataset.state = 'ok';
                setTimeout(() => { if (indicator.dataset.state === 'ok') indicator.dataset.state = 'idle'; }, 2000);
            }
            toast(`✓ ${id}: ${Object.keys(fields).length} fields saved`, 'success', 2400);
        } else {
            if (indicator) indicator.dataset.state = 'error';
            toast(`⚠ ${id}: mismatched fields — ${mismatches.join(', ')}`, 'error', 5000);
        }
        patchCameraStatusUI();
    } catch (err) {
        if (indicator) indicator.dataset.state = 'error';
        toast(`Save failed (${id}): ${err.message}`, 'error', 4000);
    }
}

/* Quick toggle from the operational tier — flips schedule.enabled and saves.
   Reuses existing start_time/end_time/days if set; otherwise defaults to a
   reasonable 9 AM–5 PM Mon–Fri pattern that the user can refine in Schedules tab. */
export async function toggleScheduleQuick(id, enabled) {
    try {
        const existing = state.schedules?.[id] || {};
        const body = {
            camera_id: id,
            enabled,
            start_time: existing.start_time || '05:40',
            days: existing.days && existing.days.length ? existing.days : ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'],
        };
        await api.postSchedule(body);
        if (!state.schedules) state.schedules = {};
        state.schedules[id] = { ...existing, ...body, enabled };
        toast(`✓ ${id}: auto-schedule ${enabled ? 'enabled' : 'disabled'}`, 'success', 2400);
    } catch (err) {
        // Revert UI
        const cb = document.getElementById(`cam-${id}-sched-toggle`);
        if (cb) cb.checked = !enabled;
        toast(`Schedule update failed: ${err.message}`, 'error', 4000);
    }
}

/* Zoom-scan (tiling) on/off — same camera, just a switch, NOT a separate camera.
   Persists tiling_enabled so the processor tiles (or not) for this camera. */
export async function toggleTiling(id, enabled) {
    try {
        await api.postSettings({ camera_id: id, fields: { tiling_enabled: enabled } });
        if (state.cameras[id]) state.cameras[id].tiling_enabled = enabled;
        toast(`✓ Zoom scan ${enabled ? 'on' : 'off'}`, 'success', 2000);
    } catch (err) {
        const cb = document.getElementById(`cam-${id}-tiling`);
        if (cb) cb.checked = !enabled;
        toast(`Update failed: ${err.message}`, 'error', 4000);
    }
}

export async function saveGlobal() {
    const sent = {
        email_enabled: document.getElementById('g-email').checked,
        recipient_email: document.getElementById('g-recipient').value,
    };
    try {
        await api.postSettings({ global: sent });
        const fresh = await api.getSettings();
        const mismatches = [];
        for (const [k, v] of Object.entries(sent)) {
            if (String(fresh[k]) !== String(v)) mismatches.push(k);
        }
        state.settings = fresh;
        if (mismatches.length === 0) {
            toast('✓ Saved', 'success', 2500);
        } else {
            toast(`⚠ Saved but mismatches: ${mismatches.join(', ')}`, 'error', 5000);
        }
    } catch (err) { toast('Save failed: ' + err.message, 'error'); }
}

/* ==============================================================
   MODEL STATUS POLLING (custom Custom Labels endpoints)
   Mirrors Wilbur's STARTING/RUNNING/STOPPING/STOPPED state machine.
   Polls every 5s while Settings → Cameras tab is open AND there's at
   least one custom-model camera. Stops on tab change to save API calls.
   ============================================================== */
const MODEL_RUNNING_STATES = new Set(['RUNNING', 'STARTING']);
const MODEL_PENDING_STATES = new Set(['STARTING', 'STOPPING']);
const MODEL_FINAL_STATES   = new Set(['RUNNING', 'STOPPED', 'TRAINING_COMPLETED', 'FAILED', 'NOT_FOUND', 'NOT_CONFIGURED']);

function hasCustomModelCameras() {
    return Object.values(state.cameras).some(c => c.model_type === 'custom' && c.custom_model_arn && !String(c.custom_model_arn).startsWith('REPLACE_'));
}

export function startModelPolling() {
    stopModelPolling();
    if (!hasCustomModelCameras()) return;
    state.modelPollTimer = setInterval(async () => {
        try {
            const r = await api.getModelStatus();
            const before = JSON.stringify(state.modelStatuses);
            state.modelStatuses = r.cameras || {};
            const after = JSON.stringify(state.modelStatuses);

            if (before !== after) {
                // SURGICAL update: only refresh status badge/buttons/hint.
                // User-edited input values are never touched.
                patchCameraStatusUI();
                // Toast on transition
                try {
                    const prev = JSON.parse(before);
                    for (const [cid, cur] of Object.entries(state.modelStatuses)) {
                        const old = prev[cid]?.status;
                        if (old && old !== cur.status) {
                            if (cur.status === 'RUNNING') toast(`✓ ${cid} model is now RUNNING`, 'success', 4000);
                            else if (cur.status === 'STOPPED' && old === 'STOPPING') toast(`✓ ${cid} model stopped`, 'info', 3000);
                            else if (cur.status === 'FAILED') toast(`✗ ${cid} model FAILED`, 'error', 5000);
                        }
                    }
                } catch {}
            }
        } catch (err) {
            console.warn('[ModelPoll]', err.message);
        }
    }, CONFIG.MODEL_POLL_INTERVAL_MS);
    refreshModelStatusOnce();
}

export function stopModelPolling() {
    if (state.modelPollTimer) {
        clearInterval(state.modelPollTimer);
        state.modelPollTimer = null;
    }
}

async function refreshModelStatusOnce() {
    try {
        const r = await api.getModelStatus();
        state.modelStatuses = r.cameras || {};
        patchCameraStatusUI();
    } catch (err) { console.warn('[ModelStatus]', err.message); }
}

export async function modelStart(id) {
    if (!confirm(`Start ${id} model endpoint?\n\n• Costs ~$4/hour while running\n• Takes 5–10 minutes to reach RUNNING\n• You'll see status: STARTING → RUNNING`)) return;
    try {
        await api.startModel(id);
        toast(`${id} start initiated — status will transition to STARTING then RUNNING in ~5 min`, 'info', 6000);
        // Optimistic local update + surgical UI patch
        state.modelStatuses[id] = { ...state.modelStatuses[id], status: 'STARTING' };
        patchCameraStatusUI();
        startModelPolling();
    } catch (err) {
        toast('Start failed: ' + err.message, 'error', 5000);
    }
}

export async function modelStop(id) {
    if (!confirm(`Stop ${id} model endpoint?\n\nNo more inference will run on this camera.`)) return;
    try {
        await api.stopModel(id);
        toast(`${id} stop initiated — status will transition to STOPPING then STOPPED`, 'info', 4000);
        state.modelStatuses[id] = { ...state.modelStatuses[id], status: 'STOPPING' };
        patchCameraStatusUI();
        startModelPolling();
    } catch (err) {
        toast('Stop failed: ' + err.message, 'error', 5000);
    }
}

/* Backwards compat shim — old code calls refreshStatuses() */
export function refreshStatuses() { refreshModelStatusOnce(); }

export async function addIdentity() {
    const el = document.getElementById('new-email');
    const email = el.value.trim();
    if (!email || !email.includes('@')) { toast('Enter a valid email', 'error'); return; }
    try {
        await api.addIdentity(email);
        toast(`Verification email sent to ${email} — check inbox`, 'success', 5000);
        el.value = '';
        state.identities = (await api.getIdentities()).emails || [];
        renderSettingsSub();
    } catch (err) { toast('Add failed: ' + err.message, 'error'); }
}

/* Resend = same backend call (SES verify_email_identity is idempotent —
   it re-sends if address is already in Pending state) */
export async function resendVerification(email) {
    try {
        await api.addIdentity(email);
        toast(`Verification email re-sent to ${email}`, 'success', 4000);
    } catch (err) { toast('Resend failed: ' + err.message, 'error'); }
}

export async function refreshIdentities() {
    try {
        state.identities = (await api.getIdentities()).emails || [];
        toast('Status refreshed', 'success', 2000);
        renderSettingsSub();
    } catch (err) { toast('Refresh failed: ' + err.message, 'error'); }
}
export async function removeIdentity(email) {
    if (!confirm(`Remove ${email}?`)) return;
    try {
        await api.removeIdentity(email);
        toast('Removed', 'success');
        state.identities = (await api.getIdentities()).emails || [];
        renderSettingsSub();
    } catch (err) { toast('Remove failed: ' + err.message, 'error'); }
}

export async function saveSchedule(id) {
    const enabled = document.getElementById(`sch-${id}-enabled`).checked;
    const start_time = document.getElementById(`sch-${id}-start`).value;
    const days = Array.from(document.querySelectorAll(`#sch-${id}-days .chip.active`)).map(c => c.textContent.trim());
    try {
        await api.postSchedule({ camera_id: id, enabled, start_time, days });
        toast(`Schedule ${enabled ? 'enabled' : 'disabled'} for ${id}`, 'success');
        state.settings = await api.getSettings();
        state.cameras = state.settings.cameras || {};
    } catch (err) { toast('Schedule save failed: ' + err.message, 'error'); }
}
export async function deleteSchedule(id) {
    if (!confirm(`Delete schedule for ${id}?`)) return;
    try {
        await api.deleteSchedule(id);
        toast('Deleted', 'success');
        state.settings = await api.getSettings();
        state.cameras = state.settings.cameras || {};
        renderSettingsSub();
    } catch (err) { toast('Delete failed: ' + err.message, 'error'); }
}
export async function refreshScheduleLogs() {
    try {
        state.scheduleLogs = (await api.getScheduleLogs(30)).logs || [];
        const el = document.getElementById('schedule-logs-body');
        if (el) el.innerHTML = scheduleLogsMarkup();
        toast('Refreshed', 'success');
    } catch (err) { toast('Refresh failed: ' + err.message, 'error'); }
}
