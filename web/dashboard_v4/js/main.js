/* ==============================================================
   MAIN — bootstrap, tab router, topbar notifications, and the
   window bridge for inline onclick/onchange handlers.
   Split from dashboard_v3_9.html (v4.0 module split).

   Why the window bridge: the app renders its UI as HTML strings
   with inline handlers (onclick="switchTab('live')" etc). Inline
   handlers resolve names on window, and module scope is NOT
   window scope — so main.js explicitly exposes ONLY this handler
   surface. Everything else stays module-private (the point of the
   split; see docs/dashboard.md).
   ============================================================== */
import { state, imageCache } from './state.js';
import { api } from './api.js';
import { initAuthUi, isLoggedIn, logout, requireLogin } from './auth.js';
import { toast, refreshToastFan, escapeHtml, fmtTimeShort, camDisplayName } from './utils.js';
import { renderLivePage, selectLiveCam, toggleStream } from './live.js';
import {
    renderGalleryPage, renderGalleryGrid, applyGalleryFilters, resetGalleryFilters,
    clearImageCache, retryThumb, deleteGalleryItem, gotoGalleryPage,
} from './gallery.js';
import { openImg, closeImageModal, deleteOpenImage } from './modal.js';
import { verifyClick } from './bbox.js';
import { renderAnalyticsPage, loadAnalytics } from './analytics.js';
import {
    renderSettingsPage, switchSettingsSub, stopModelPolling,
    modelStart, modelStop, debouncedSaveCamera, toggleScheduleQuick, toggleTiling,
    toggleChip, setDays, saveSchedule, deleteSchedule, refreshScheduleLogs,
    addIdentity, removeIdentity, resendVerification, refreshIdentities,
    saveGlobal, submitUpload, resetUpload, refreshUploadModelHint,
} from './settings.js';
import { renderCostsPage, changeCostRange } from './costs.js';

const TABS = [
    { id: 'live',      label: 'Live stream', render: renderLivePage,      sub: '' },
    { id: 'gallery',   label: 'Gallery',     render: renderGalleryPage,   sub: '' },
    { id: 'analytics', label: 'Analytics',   render: renderAnalyticsPage, sub: '' },
    { id: 'settings',  label: 'Settings',    render: renderSettingsPage,  sub: '' },
    // Costs tab removed: IAM user has no Cost Explorer access (ce:GetCostAndUsage requires
    // account-root to enable "IAM access to Billing", which is not available on shared nbk2).
];

/* ==============================================================
   NOTIFICATIONS (topbar) — polling-fed since v3.7 (WebSocket removed)
   ============================================================== */
export function addNotif(e) {
    state.notifs.unshift(e);
    if (state.notifs.length > state.notifMax) state.notifs.length = state.notifMax;
    renderNotifs();
}
function renderNotifs() {
    const cnt = document.getElementById('notif-count');
    cnt.textContent = state.notifs.length;
    cnt.classList.toggle('zero', state.notifs.length === 0);
    const body = document.getElementById('notif-body');
    if (state.notifs.length === 0) {
        body.innerHTML = '<div class="notif-empty">No events yet.</div>';
        return;
    }
    body.innerHTML = state.notifs.map(e => {
        const t = fmtTimeShort(e.timestamp || e.detection_time || new Date().toISOString());
        if (e.kind === 'detected') {
            const msg = e.target_detected
                ? `<strong>${escapeHtml(e.target_label)}</strong> detected`
                : `${escapeHtml(e.target_label)} not detected`;
            return `<div class="notif-item detected">
                <div class="notif-item-head"><span class="notif-item-type">🔍 Detection</span><span class="notif-item-time">${t}</span></div>
                <div class="notif-item-body">${msg}</div>
                <div class="notif-item-meta">${escapeHtml(camDisplayName(e.camera_id))} · ${escapeHtml(e.waypoint_id || '')}</div>
            </div>`;
        } else if (e.kind === 'schedule') {
            const icon = e.result === 'SUCCESS' ? '✓' : '✗';
            return `<div class="notif-item schedule">
                <div class="notif-item-head"><span class="notif-item-type">⏱ Schedule ${icon}</span><span class="notif-item-time">${t}</span></div>
                <div class="notif-item-body">${escapeHtml(e.action)} · ${escapeHtml(camDisplayName(e.camera_id))}</div>
                <div class="notif-item-meta">${escapeHtml(e.message || e.error || '')}</div>
            </div>`;
        }
        return '';
    }).join('');
}
function toggleNotifs() {
    document.getElementById('notif-dropdown').classList.toggle('open');
}
function clearNotifs() { state.notifs = []; renderNotifs(); }
document.addEventListener('click', (e) => {
    const dd = document.getElementById('notif-dropdown');
    if (!dd) return;
    if (dd.classList.contains('open') && !e.target.closest('.notif-btn') && !e.target.closest('.notif-dropdown')) {
        dd.classList.remove('open');
    }
});

/* ==============================================================
   TAB ROUTER
   ============================================================== */
function renderTabs() {
    document.getElementById('tabs').innerHTML = TABS.map(t =>
        `<button class="tab ${state.tab === t.id ? 'active' : ''}" onclick="switchTab('${t.id}')">${escapeHtml(t.label)}</button>`
    ).join('');
}

async function switchTab(id) {
    state.tab = id;
    renderTabs();
    const tab = TABS.find(t => t.id === id);
    if (!tab) return;
    document.getElementById('page-title').textContent = tab.label;
    document.getElementById('page-sub').textContent = tab.sub;
    document.getElementById('page-actions').innerHTML = '';
    const content = document.getElementById('page-content');
    content.innerHTML = '<div class="loading-wrapper"><span class="spinner"></span> Loading…</div>';

    // Teardown side effects
    if (id !== 'live' && state.hls) { try { state.hls.destroy(); } catch {}; state.hls = null; }
    if (id !== 'analytics') {
        if (state.hourlyChart) { state.hourlyChart.destroy(); state.hourlyChart = null; }
        if (state.dailyChart) { state.dailyChart.destroy(); state.dailyChart = null; }
        if (state.byZoneChart) { state.byZoneChart.destroy(); state.byZoneChart = null; }
    }
    if (id !== 'costs' && state.costChart) { state.costChart.destroy(); state.costChart = null; }
    // Stop model polling whenever leaving Settings
    if (id !== 'settings') stopModelPolling();

    try { await tab.render(); }
    catch (err) {
        console.error('[Tab]', err);
        content.innerHTML = `<div class="empty-state"><h3>Failed to load</h3><p>${escapeHtml(err.message)}</p></div>`;
    }
}

/* ==============================================================
   WINDOW BRIDGE — the complete inline-handler surface.
   If a new inline onclick is added anywhere, its function must be
   added here too, or the click will throw "X is not defined".
   ============================================================== */
Object.assign(window, {
    // shared state (the upload sub-tab's inline onchange writes state.uploadCam)
    state,
    // auth (v4.1)
    logoutClick: logout,
    // topbar
    toggleNotifs, clearNotifs, switchTab,
    // toast
    refreshToastFan,
    // live
    selectLiveCam, toggleStream,
    // gallery
    applyGalleryFilters, resetGalleryFilters, clearImageCache, openImg, retryThumb,
    deleteGalleryItem, gotoGalleryPage,
    // bbox / modal
    verifyClick, closeImageModal, deleteOpenImage,
    // analytics
    loadAnalytics,
    // settings
    switchSettingsSub, modelStart, modelStop, debouncedSaveCamera, toggleScheduleQuick, toggleTiling,
    toggleChip, setDays, saveSchedule, deleteSchedule, refreshScheduleLogs,
    addIdentity, removeIdentity, resendVerification, refreshIdentities,
    saveGlobal, submitUpload, resetUpload, refreshUploadModelHint,
    // costs (tab currently removed from TABS; markup self-references these)
    renderCostsPage, changeCostRange,
});

/* ==============================================================
   INIT
   ============================================================== */
async function init() {
    // v3.7: WebSocket removed; dashboard now uses polling for live updates
    renderTabs();
    // Open IndexedDB cache early so first gallery render is instant
    try { await imageCache.open(); } catch (err) { console.warn('[Cache] failed to open:', err); }
    // v4.1: gate the app behind Cognito sign-in; login submit re-enters startApp
    initAuthUi(startApp);
    if (isLoggedIn()) await startApp();
    else requireLogin();
}
async function startApp() {
    try {
        state.settings = await api.getSettings();
        state.cameras = state.settings.cameras || {};
    } catch (err) {
        toast('Settings failed to load: ' + err.message, 'error', 6000);
        state.settings = { cameras: {} };
        state.cameras = {};
    }
    try { state.modelStatuses = (await api.getModelStatus()).cameras || {}; } catch {}
    try { state.streamStatuses = (await api.getStreamStatus()).streams || {}; } catch {}
    switchTab(state.tab);
    // Model status polling lifecycle is managed by switchTab/switchSettingsSub.
    // No global setInterval needed.
}
window.addEventListener('load', init);
