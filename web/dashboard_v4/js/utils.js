/* ==============================================================
   HELPERS — toast, time/format, escaping, camera classifiers,
   chart options. Split from dashboard_v3_9.html (v4.0 module split).
   NOTE: toast markup references refreshToastFan via inline onclick,
   so main.js exposes it on window.
   ============================================================== */

export function toast(msg, kind = 'info', ms = 3000) {
    const stack = document.getElementById('toast-stack');
    if (!stack) return;
    const t = document.createElement('div');
    t.className = 'toast ' + kind;
    t.innerHTML = `<span class="toast-close" onclick="this.parentElement.remove(); refreshToastFan();">×</span>${escapeHtml(msg)}`;
    // Newest on top: prepend so :nth-child(1) is most recent
    stack.insertBefore(t, stack.firstChild);
    refreshToastFan();
    // Auto-dismiss after ms
    setTimeout(() => {
        t.style.transition = 'opacity 0.22s, transform 0.22s';
        t.style.opacity = '0';
        t.style.transform = 'translateX(120%) scale(0.95)';
        setTimeout(() => { t.remove(); refreshToastFan(); }, 220);
    }, ms);
}

/* Recompute hover-fan offsets so toasts don't overlap on hover.
   Each toast uses --fan-offset = sum of heights of preceding toasts + 8px gaps. */
export function refreshToastFan() {
    const stack = document.getElementById('toast-stack');
    if (!stack) return;
    let cumulative = 0;
    [...stack.children].forEach((t, i) => {
        t.style.setProperty('--fan-offset', cumulative + 'px');
        cumulative += t.offsetHeight + 8;
    });
}

/* Time helpers — backend stores UTC, all UI shows Singapore (UTC+8). */
export function _parseUtc(iso) {
    if (!iso) return null;
    // DynamoDB stores "YYYY-MM-DD HH:MM:SS" without timezone; treat as UTC.
    const s = String(iso);
    if (s.includes('T')) return new Date(s);  // already ISO with TZ
    // Backend "YYYY-MM-DD HH:MM:SS" -> assume UTC -> append Z
    return new Date(s.replace(' ', 'T') + 'Z');
}
export function _toSg(d) {
    // Render in SGT regardless of browser locale
    return d;  // we use timeZone:'Asia/Singapore' in Intl options
}
export function fmtTime(iso) {
    const d = _parseUtc(iso);
    if (!d || isNaN(d)) return iso || '—';
    return d.toLocaleString('en-GB', {
        month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit',
        timeZone: 'Asia/Singapore',
    });
}
export function fmtTimeShort(iso) {
    const d = _parseUtc(iso);
    if (!d || isNaN(d)) return iso || '—';
    return d.toLocaleTimeString('en-GB', {
        hour: '2-digit', minute: '2-digit',
        timeZone: 'Asia/Singapore',
    });
}
export function fmtDate(iso) {
    if (!iso) return '—';
    // For YYYY-MM-DD strings (no time), just format
    if (String(iso).length === 10 && !iso.includes(':')) {
        const d = new Date(iso + 'T00:00:00+08:00');
        return d.toLocaleDateString('en-GB', { month: 'short', day: 'numeric', timeZone: 'Asia/Singapore' });
    }
    const d = _parseUtc(iso);
    if (!d || isNaN(d)) return iso;
    return d.toLocaleDateString('en-GB', { month: 'short', day: 'numeric', timeZone: 'Asia/Singapore' });
}
/* "Today" in Singapore — used for filter date defaults */
export function todayYMD() {
    const sgNow = new Date(Date.now() + 8 * 3600 * 1000);
    return sgNow.toISOString().slice(0, 10);
}
export function daysAgoYMD(n) {
    const sg = new Date(Date.now() + 8 * 3600 * 1000);
    sg.setUTCDate(sg.getUTCDate() - n);
    return sg.toISOString().slice(0, 10);
}
/* Convert "YYYY-MM-DD HH:MM:SS" (UTC) to Singapore Date for comparisons */
export function utcToSgDate(iso) {
    const d = _parseUtc(iso);
    if (!d || isNaN(d)) return null;
    return new Date(d.getTime() + 8 * 3600 * 1000);
}

export function escapeHtml(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* ---------------------------------------------------------------
   convertToJpg(file, quality)
   Convert any browser-readable image File (PNG, HEIC, WebP, BMP,
   GIF, JPEG) into a JPG File. Pass-through if already JPEG.
   Transparency is flattened onto white. Filename .ext is rewritten
   to .jpg. Returns a new File with type='image/jpeg'.
   --------------------------------------------------------------- */
export async function convertToJpg(file, quality = 0.92) {
    if (file.type === 'image/jpeg' || file.type === 'image/jpg') {
        return file;
    }
    return new Promise((resolve, reject) => {
        const img = new Image();
        const url = URL.createObjectURL(file);
        img.onload = () => {
            URL.revokeObjectURL(url);
            const canvas = document.createElement('canvas');
            canvas.width = img.naturalWidth;
            canvas.height = img.naturalHeight;
            const ctx = canvas.getContext('2d');
            ctx.fillStyle = '#FFFFFF';   // flatten transparency to white
            ctx.fillRect(0, 0, canvas.width, canvas.height);
            ctx.drawImage(img, 0, 0);
            canvas.toBlob((blob) => {
                if (!blob) { reject(new Error('JPG conversion produced empty blob')); return; }
                const newName = file.name.replace(/\.[^.]+$/, '') + '.jpg';
                resolve(new File([blob], newName, { type: 'image/jpeg', lastModified: Date.now() }));
            }, 'image/jpeg', quality);
        };
        img.onerror = () => {
            URL.revokeObjectURL(url);
            reject(new Error(`Cannot decode image "${file.name}" (format may be unsupported by this browser)`));
        };
        img.src = url;
    });
}

export function pestClass(label) {
    const l = String(label || '').toLowerCase();
    if (l === 'moth') return 'moth';
    if (l.includes('worm') || l.includes('caterpillar')) return 'worm';
    if (l === 'person' || l === 'face') return 'person';
    return 'generic';
}

export function isManualCamera(id) { return id === 'manual_upload'; }
export function isTestCamera(id) { return id === 'person_cam'; }
export function needsKvs(id) { return !isManualCamera(id) && !isTestCamera(id); }
export function needsSchedule(id, cam) {
    // Only cameras with a real custom model benefit from scheduling ($4/hr endpoints)
    return (cam?.model_type === 'custom') && !isManualCamera(id);
}
export function isWormCam(id) { return id.startsWith('worm') || id.startsWith('armyworm'); }
export function camAvatarClass(id) {
    if (id.startsWith('moth')) return 'moth';
    if (isWormCam(id)) return 'worm';
    if (isManualCamera(id)) return 'gray';
    return '';
}
export function camInitial(id) {
    if (id.startsWith('moth')) return 'M';
    if (isWormCam(id)) return 'W';
    if (isManualCamera(id)) return '✎';
    return id.slice(0, 1).toUpperCase();
}

export function statusBadge(status) {
    const map = {
        'RUNNING': ['success', 'Live'],
        'STARTING': ['warning', 'Starting'],
        'STOPPING': ['warning', 'Stopping'],
        'STOPPED': ['muted', 'Standby'],
        'TRAINING_COMPLETED': ['info', 'Ready'],
        'FAILED': ['danger', 'Failed'],
        'NOT_CONFIGURED': ['muted', 'Free mode'],
        'NOT_FOUND': ['danger', 'Setup incomplete'],
        'UNKNOWN': ['muted', 'Unknown'],
        'ERROR': ['danger', 'Error'],
    };
    const [k, l] = map[status] || ['muted', status || 'Unknown'];
    return `<span class="badge badge-${k}">${escapeHtml(l)}</span>`;
}

export function cleanLabel(label) {
    // Strip a trailing "(...)" for display only — DB label stays intact.
    return (label || '').replace(/\s*\([^)]*\)\s*$/, '').trim();
}

/* Friendly display name for a camera_id, used everywhere in the gallery/modal so
   users never see internal ids (armyworm_go2_a8mini, wilbur-fyp-project). Static
   map so it also covers legacy/migrated ids that aren't in the cameras table.
   Unknown ids fall back to the id itself. */
const CAM_DISPLAY = {
    'worm_cam':            'Worm Cam',
    'moth_cam':            'Moth Cam',
    'armyworm_go2_a8mini': 'Worm Cam',   // pre-migration id
    'moth_cam_01':         'Moth Cam',   // pre-migration id
    'wilbur-fyp-project':  'Moth Cam',   // Wilbur-era legacy records
    'manual_upload':       'Test upload',
};
export function camDisplayName(id) {
    if (!id) return '—';
    return CAM_DISPLAY[id] || id;
}

export function intVal(sec) {
    if (sec % 3600 === 0 && sec >= 3600) return { v: sec / 3600, u: 'hours' };
    if (sec % 60 === 0 && sec >= 60) return { v: sec / 60, u: 'minutes' };
    return { v: sec, u: 'seconds' };
}
export function valToSec(v, u) { v = parseInt(v, 10) || 60; return u === 'hours' ? v * 3600 : u === 'minutes' ? v * 60 : v; }

/* Shared Chart.js option factory (analytics + costs). */
export function chartOpts(xLabel, yLabel) {
    return {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: { display: false },
            tooltip: { backgroundColor: '#1c1917', bodyFont: { family: "'JetBrains Mono'" } },
        },
        scales: {
            x: {
                title: { display: true, text: xLabel, color: '#78716c', font: { size: 11 } },
                grid: { display: false },
                ticks: { color: '#78716c', font: { size: 11, family: "'JetBrains Mono'" } },
            },
            y: {
                title: { display: true, text: yLabel, color: '#78716c', font: { size: 11 } },
                beginAtZero: true,
                ticks: { precision: 0, color: '#8b8b93', font: { size: 11, family: "'JetBrains Mono'" } },
                grid: { color: 'rgba(20,30,45,0.07)' },
            },
        },
    };
}
