/* ==============================================================
   PAGE: LIVE STREAM (KVS HLS playback + stream toggles)
   Split from dashboard_v3_9.html (v4.0 module split).
   `Hls` is the hls.js CDN global loaded in index.html.
   ============================================================== */
import { state } from './state.js';
import { api } from './api.js';
import { toast, escapeHtml, cleanLabel, isWormCam } from './utils.js';

export async function renderLivePage() {
    const content = document.getElementById('page-content');
    const streamCams = Object.entries(state.cameras).filter(([id, c]) => c.kvs_stream_name);

    if (streamCams.length === 0) {
        content.innerHTML = `<div class="empty-state">
            <h3>No streaming cameras configured</h3>
            <p>Set a KVS stream name on a camera in Settings → Cameras to enable live playback.</p>
        </div>`;
        return;
    }
    if (!state.liveCam || !state.cameras[state.liveCam]?.kvs_stream_name) {
        // Prefer the worm camera (its KVS stream is live); fall back to first.
        const preferred = streamCams.find(([id]) => isWormCam(id));
        state.liveCam = (preferred || streamCams[0])[0];
    }

    // Pull stream status (best-effort; non-blocking)
    try {
        const r = await api.getStreamStatus();
        state.streamStatuses = r.streams || {};
    } catch (err) { console.warn('[Stream] status fetch failed', err); }

    content.innerHTML = `
        <div class="camera-grid">
            ${streamCams.map(([id, c]) => liveCamCardMarkup(id, c)).join('')}
        </div>
        <div class="video-wrapper" id="video-wrap">
            <div class="video-overlay">
                <div class="title"><span class="spinner" style="border-top-color:white;"></span> Loading stream…</div>
            </div>
        </div>
        <div style="margin-top:12px;font-size:11.5px;color:var(--muted);text-align:center;">
            Streaming control is real-time. KVS producer integration with Wilbur's mini PC is pending — the toggle above sets the desired state, the producer will start/stop pushing on next poll.
        </div>
    `;
    loadStream();
}

function liveCamCardMarkup(id, c) {
    const selected = id === state.liveCam;
    const sStatus = state.streamStatuses[id] || {};
    const enabled = !!sStatus.stream_enabled;
    const sub = id.startsWith('moth') ? 'Fixed · Jewel indoor garden'
              : isWormCam(id) ? 'Mobile · robot patrol'
              : 'Mobile';
    return `<div class="camera-card ${selected ? 'selected' : ''}" onclick="selectLiveCam('${escapeHtml(id)}', event)">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;">
            <div style="min-width:0;flex:1;">
                <div class="camera-card-title">${escapeHtml(cleanLabel(c.label) || id)}</div>
                <div class="camera-card-sub">${escapeHtml(sub)}</div>
            </div>
            <label class="toggle" title="Enable / disable KVS push" onclick="event.stopPropagation()">
                <input type="checkbox" ${enabled ? 'checked' : ''} onchange="toggleStream('${escapeHtml(id)}', this.checked)">
                <span class="toggle-slider"></span>
            </label>
        </div>
        <div class="camera-card-status">
            ${selected ? '<span class="badge badge-success">● Selected</span>' : '<span class="badge badge-muted">Idle</span>'}
            ${enabled
                ? '<span class="badge badge-info" style="margin-left:6px;">Stream on</span>'
                : '<span class="badge badge-muted" style="margin-left:6px;">Stream off</span>'}
        </div>
    </div>`;
}

export function selectLiveCam(id, ev) {
    if (ev && ev.target.closest('.toggle')) return;  // ignore toggle clicks
    state.liveCam = id;
    renderLivePage();
}

export async function toggleStream(camId, enable) {
    try {
        if (enable) {
            const r = await api.startStream(camId);
            toast(`Stream enabled for ${camId}`, 'success');
            state.streamStatuses[camId] = { stream_enabled: true, kvs_stream_name: r.kvs_stream_name };
        } else {
            await api.stopStream(camId);
            toast(`Stream disabled for ${camId}`, 'info');
            state.streamStatuses[camId] = { stream_enabled: false };
        }
        // Re-render just the selected camera card status
        renderLivePage();
    } catch (err) {
        toast('Stream toggle failed: ' + err.message, 'error');
        renderLivePage();  // re-render to revert toggle
    }
}

async function loadStream() {
    const cam = state.cameras[state.liveCam];
    if (!cam || !cam.kvs_stream_name) return;
    const wrap = document.getElementById('video-wrap');
    if (!wrap) return;
    try {
        const r = await api.getVideoPlayback(cam.kvs_stream_name);
        playHls(wrap, r.hls_url);
    } catch (err) {
        wrap.innerHTML = `
            <div class="video-overlay">
                <div class="title">No live stream available</div>
                <div class="msg">${escapeHtml(err.message)}</div>
                <div class="msg" style="margin-top:6px;font-size:11px;color:#999;">
                </div>
            </div>
            <div class="live-indicator"><span class="live-dot"></span> LIVE · ${escapeHtml(state.liveCam)}</div>
        `;
    }
}

function playHls(container, url) {
    if (state.hls) { try { state.hls.destroy(); } catch {} state.hls = null; }
    container.innerHTML = `
        <video controls autoplay muted playsinline></video>
        <div class="live-indicator"><span class="live-dot"></span> LIVE · ${escapeHtml(state.liveCam)}</div>`;
    const video = container.querySelector('video');
    if (Hls.isSupported()) {
        state.hls = new Hls({ lowLatencyMode: true });
        state.hls.loadSource(url);
        state.hls.attachMedia(video);
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = url;
    }
}
