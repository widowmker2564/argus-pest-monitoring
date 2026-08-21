/* ==============================================================
   PAGE: ANALYTICS  (v3.4 overhaul — operational signals for CAG)
   - Removed: Hourly pattern chart, Peak hour stat, Avg/hour stat
     (Patrols run only at 2 fixed daypoints — hourly resolution is meaningless.)
   - Added: By-zone bar chart (top 5 waypoints, last 7d) — answers "where to inspect"
   - Added: Days-since-last-detection per camera (>7d red) — answers "is the camera live"
   - Added: FP rate stat (computed from verified ✓/✗ in gallery)
   - Kept: Daily trend, By camera, Today, Range total, Peak day, Avg/day
   Split from dashboard_v3_9.html (v4.0 module split).
   `Chart` is the chart.js CDN global loaded in index.html.
   ============================================================== */
import { state } from './state.js';
import { api } from './api.js';
import { toast, escapeHtml, fmtDate, todayYMD, daysAgoYMD, utcToSgDate, cleanLabel, isManualCamera, chartOpts, camDisplayName } from './utils.js';
import { getVerifiableBoxes, getCountedBoxes, hydrateVerifyMap } from './bbox.js';

export async function renderAnalyticsPage() {
    const content = document.getElementById('page-content');
    content.innerHTML = `
        <div class="stats-grid" id="stats-grid"></div>
        <div class="chart-grid">
            <div class="card">
                <div class="card-head">
                    <div><div class="card-title">By zone</div></div>
                    <button class="btn btn-outline btn-sm" onclick="loadAnalytics()">⟳</button>
                </div>
                <div class="chart-canvas-wrap"><canvas id="by-zone-chart"></canvas></div>
            </div>
            <div class="card">
                <div class="card-head">
                    <div><div class="card-title">Daily trend</div><div class="card-sub" id="daily-sub"></div></div>
                    <div style="display:flex;gap:8px;align-items:center;">
                        <input type="date" class="input" id="ana-from" style="width:140px;" value="${daysAgoYMD(29)}">
                        <span style="color:var(--muted);">→</span>
                        <input type="date" class="input" id="ana-to" style="width:140px;" value="${todayYMD()}">
                        <button class="btn btn-primary btn-sm" onclick="loadAnalytics()">Load</button>
                    </div>
                </div>
                <div class="chart-canvas-wrap"><canvas id="daily-chart"></canvas></div>
            </div>
            <div class="card">
                <div class="card-head">
                    <div><div class="card-title">Zone heatmap</div></div>
                </div>
                <div class="heatmap-wrap"><div id="zone-heatmap"></div></div>
                <div class="hm-legend" id="hm-legend"></div>
            </div>
            <div class="card">
                <div class="card-head">
                    <div><div class="card-title">Camera health</div></div>
                </div>
                <div id="cam-health"></div>
            </div>
            <div class="card">
                <div class="card-head">
                    <div><div class="card-title">By camera</div></div>
                </div>
                <div id="by-cam"></div>
            </div>
        </div>
    `;
    loadAnalytics();
}

export async function loadAnalytics() {
    const from = document.getElementById('ana-from')?.value || daysAgoYMD(29);
    const to = document.getElementById('ana-to')?.value || todayYMD();
    try {
        const resp = await api.getHistory({ date_from: from, date_to: to, limit: 500 });
        // User may have switched tabs while we were awaiting; bail silently
        if (state.tab !== 'analytics') return;
        const items = resp.items || [];
        // v3.6: hydrate verifyMap from backend.
        // New format: it.verifications = {0: "TP", 1: "FP"} (or stringified JSON).
        // Legacy: it.verified = true|false → all bboxes that record TP or FP respectively.
        for (const it of items) hydrateVerifyMap(it);
        renderStats(items);
        renderByZoneChart(items);
        renderZoneHeatmap(items);
        renderDailyChart(items, from, to);
        renderCamHealth(items);
        renderByCam(items);
    } catch (err) {
        // Don't toast if it's because the page changed
        if (state.tab !== 'analytics') return;
        toast('Analytics: ' + err.message, 'error');
    }
}

/* Bucket detected items by waypoint, last 7 days only. */
/* v3.6.2: count visible bboxes (>= current threshold) per zone, last 7 days.
   Previously counted records — 1 photo with 5 worms only contributed 1 to its zone.
   Now contributes 5, matching what CAG actually wants ("where are the pests"). */
function zoneBuckets7d(items) {
    const cutoff = Date.now() - 7 * 24 * 60 * 60 * 1000;
    const b = {};
    for (const it of items) {
        const d = utcToSgDate(it.detection_time);
        if (!d || d.getTime() < cutoff) continue;
        const n = getCountedBoxes(it).length;
        if (n === 0) continue;
        const z = it.waypoint_id || '(unknown)';
        b[z] = (b[z] || 0) + n;
    }
    return b;
}

/* v3.6.2: count visible bboxes per day across the full range. */
function dailyBuckets(items) {
    const b = {};
    for (const it of items) {
        const sg = utcToSgDate(it.detection_time);
        if (!sg) continue;
        const n = getCountedBoxes(it).length;
        if (n === 0) continue;
        const ymd = sg.toISOString().slice(0, 10);
        b[ymd] = (b[ymd] || 0) + n;
    }
    return b;
}

function renderStats(items) {
    // v3.6.2: all stat cards are bbox-level now, not record-level.
    // A photo with 5 worms contributes 5 to "Today" — matches Review list and Gallery.
    // dailyBuckets() already returns bbox counts keyed by date.
    const today = todayYMD();
    const d = dailyBuckets(items);
    const totalBboxes = Object.values(d).reduce((a, b) => a + b, 0);
    const todaysBboxes = d[today] || 0;
    const peakD = Object.entries(d).sort((a, b) => b[1] - a[1])[0];
    const activeDays = Object.keys(d).length;
    const avgDay = activeDays > 0 ? (totalBboxes / activeDays).toFixed(1) : '0';

    // For the FP rate denominator we still need to restrict to records that produced bboxes
    // (otherwise we'd iterate empty records pointlessly).
    const det = items.filter(it => getVerifiableBoxes(it).length > 0);

    // ✓ removed (opt-out model): a detection counts by default; humans only flag FPs.
    // "Flagged FP" = share of over-threshold detections marked false positive (last 7 days).
    // Denominator is ALL detections in the window, not just reviewed ones.
    const cutoff = Date.now() - 7 * 24 * 60 * 60 * 1000;
    let flaggedFP = 0, windowTotal = 0;
    for (const it of det) {
        const sg = utcToSgDate(it.detection_time);
        if (!sg || sg.getTime() < cutoff) continue;
        windowTotal += getVerifiableBoxes(it).length;
        const rec = state.verifyMap[it.image_id];
        if (rec && typeof rec === 'object') {
            for (const v of Object.values(rec)) if (v === 'FP') flaggedFP++;
        }
    }
    const fpRate = windowTotal > 0 ? ((flaggedFP / windowTotal) * 100).toFixed(0) + '%' : '—';
    const fpSub = windowTotal > 0 ? `${flaggedFP} of ${windowTotal} detections flagged` : 'No detections in last 7 days';

    const grid = document.getElementById('stats-grid');
    if (!grid) return;
    grid.innerHTML = `
        <div class="stat-card success">
            <div class="stat-label">Today</div>
            <div class="stat-value">${todaysBboxes}</div>
            <div class="stat-sub">pests sighted today</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Range total</div>
            <div class="stat-value">${totalBboxes}</div>
            <div class="stat-sub">across ${det.length} ${det.length === 1 ? 'photo' : 'photos'}</div>
        </div>
        <div class="stat-card warning">
            <div class="stat-label">Flagged FP (7d)</div>
            <div class="stat-value">${fpRate}</div>
            <div class="stat-sub">${fpSub}</div>
        </div>
        <div class="stat-card info">
            <div class="stat-label">Peak day</div>
            <div class="stat-value">${peakD ? fmtDate(peakD[0]) : '—'}</div>
            <div class="stat-sub">${peakD ? peakD[1] + ' pests' : 'No data'}</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Avg / day</div>
            <div class="stat-value">${avgDay}</div>
            <div class="stat-sub">pests across ${activeDays} ${activeDays === 1 ? 'day' : 'days'}</div>
        </div>
    `;
}

function renderByZoneChart(items) {
    const buckets = zoneBuckets7d(items);
    const sorted = Object.entries(buckets).sort((a, b) => b[1] - a[1]).slice(0, 5);
    const canvas = document.getElementById('by-zone-chart');
    if (!canvas) return;
    if (state.byZoneChart) state.byZoneChart.destroy();

    if (sorted.length === 0) {
        const ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--muted').trim();
        ctx.font = "13px 'DM Sans', sans-serif";
        ctx.textAlign = 'center';
        ctx.fillText('No detections in the last 7 days.', canvas.width / 2, canvas.height / 2);
        return;
    }

    state.byZoneChart = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: sorted.map(([z]) => z),
            datasets: [{
                data: sorted.map(([, n]) => n),
                backgroundColor: getComputedStyle(document.documentElement).getPropertyValue('--teal').trim(),
                borderRadius: 4,
            }],
        },
        options: {
            // v5.5: indexAxis 'y' makes this a horizontal bar, so x carries the
            // counts and y carries the zone names. The titles were the wrong way round.
            ...chartOpts('Detections', 'Zone'),
            indexAxis: 'y',
        },
    });
}

/* v5.0: zone x day heatmap, last 14 days. Answers "WHERE are the pests,
   and is the cluster moving" at a glance — bbox counts, same counting rules
   as every other analytics widget (getCountedBoxes). Render-only addition. */
function renderZoneHeatmap(items) {
    const el = document.getElementById('zone-heatmap');
    if (!el) return;
    const DAYS = 14;
    const dayKeys = [];
    for (let i = DAYS - 1; i >= 0; i--) {
        const d = new Date(Date.now() - i * 24 * 60 * 60 * 1000);
        dayKeys.push(d.toISOString().slice(0, 10));
    }
    // zone -> day -> bbox count
    const zones = {};
    for (const it of items) {
        const sg = utcToSgDate(it.detection_time);
        if (!sg) continue;
        const ymd = sg.toISOString().slice(0, 10);
        if (!dayKeys.includes(ymd)) continue;
        const n = getCountedBoxes(it).length;
        if (n === 0) continue;
        const z = it.waypoint_id || '(unknown)';
        zones[z] = zones[z] || {};
        zones[z][ymd] = (zones[z][ymd] || 0) + n;
    }
    const ranked = Object.entries(zones)
        .map(([z, m]) => [z, Object.values(m).reduce((a, b) => a + b, 0), m])
        .sort((a, b) => b[1] - a[1])
        .slice(0, 6);
    if (ranked.length === 0) {
        el.innerHTML = '<div style="color:var(--muted);font-size:13px;padding:10px 0;">No pests in the last 14 days.</div>';
        const lg = document.getElementById('hm-legend');
        if (lg) lg.innerHTML = '';
        return;
    }
    const max = Math.max(...ranked.flatMap(([, , m]) => Object.values(m)), 1);
    const shade = (n) => n === 0 ? 'rgba(20,30,45,0.05)'
        : `rgba(10,132,255,${(0.16 + 0.72 * (n / max)).toFixed(2)})`;

    let html = '';
    for (const [z, , m] of ranked) {
        html += `<div class="hm-zone">${escapeHtml(z)}</div>`;
        for (const day of dayKeys) {
            const n = m[day] || 0;
            html += `<div class="hm-cell" style="background:${shade(n)}"
                title="${escapeHtml(z)} &#183; ${fmtDate(day)} &#183; ${n} pest${n === 1 ? '' : 's'}"></div>`;
        }
    }
    // day label row (every other day to stay compact)
    html += `<div></div>`;
    dayKeys.forEach((day, i) => {
        html += `<div class="hm-day">${i % 2 === 0 ? fmtDate(day).replace(/ .*/, '') : ''}</div>`;
    });
    el.className = 'heatmap';
    el.style.gridTemplateColumns = `auto repeat(${DAYS}, 1fr)`;
    el.innerHTML = html;

    const lg = document.getElementById('hm-legend');
    if (lg) lg.innerHTML = `less <i style="background:${shade(0)}"></i>` +
        `<i style="background:${shade(Math.ceil(max * .33))}"></i>` +
        `<i style="background:${shade(Math.ceil(max * .66))}"></i>` +
        `<i style="background:${shade(max)}"></i> more &#183; peak ${max}/day`;
}

function renderCamHealth(items) {
    const el = document.getElementById('cam-health');
    if (!el) return;

    // For each deployed camera, find most recent detection_time
    const cams = Object.entries(state.cameras).filter(([id]) => !isManualCamera(id));
    if (cams.length === 0) {
        el.innerHTML = '<div style="color:var(--muted);font-size:13px;padding:10px 0;">No deployed cameras.</div>';
        return;
    }
    const lastByCam = {};
    for (const it of items) {
        if (!(it.target_detected === true || it.target_detected === 'true')) continue;
        const cid = it.camera_id;
        if (!lastByCam[cid] || it.detection_time > lastByCam[cid]) {
            lastByCam[cid] = it.detection_time;
        }
    }
    const now = Date.now();
    el.innerHTML = cams.map(([id, c]) => {
        const last = lastByCam[id];
        const lastDate = last ? utcToSgDate(last) : null;
        const days = lastDate ? Math.floor((now - lastDate.getTime()) / (24 * 60 * 60 * 1000)) : null;
        let statusClass = 'success';
        let statusText = '—';
        if (days === null) { statusClass = 'muted'; statusText = 'No data'; }
        else if (days === 0) { statusClass = 'success'; statusText = 'Today'; }
        else if (days <= 3) { statusClass = 'success'; statusText = `${days} day${days===1?'':'s'} ago`; }
        else if (days <= 7) { statusClass = 'warning'; statusText = `${days} days ago`; }
        else { statusClass = 'danger'; statusText = `${days} days · stale`; }

        return `<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border);">
            <strong style="font-size:13px;">${escapeHtml(cleanLabel(c.label) || id)}</strong>
            <span class="badge badge-${statusClass}">${escapeHtml(statusText)}</span>
        </div>`;
    }).join('');
}

function renderDailyChart(items, from, to) {
    // v3.6.2: dailyBuckets() now returns bbox counts per day, not record counts.
    const b = dailyBuckets(items);
    const labels = []; const data = [];
    for (let d = new Date(from); d <= new Date(to); d.setDate(d.getDate() + 1)) {
        const ymd = d.toISOString().slice(0, 10);
        labels.push(fmtDate(ymd));
        data.push(b[ymd] || 0);
    }
    const totalPests = data.reduce((a, b) => a + b, 0);
    // v5.5: `sub` was read without ever being declared. The ReferenceError aborted
    // loadAnalytics() here, so Camera health and By camera never rendered at all.
    const sub = document.getElementById('daily-sub');
    if (sub) sub.textContent = `${labels.length} days · ${totalPests} pests`;
    const canvas = document.getElementById('daily-chart');
    if (!canvas) return;
    if (state.dailyChart) state.dailyChart.destroy();
    state.dailyChart = new Chart(canvas, {
        type: 'bar',
        data: {
            labels,
            datasets: [
                { type: 'line', label: 'Trend', data, borderColor: '#af52de', backgroundColor: 'transparent', tension: 0.3, pointRadius: 3, borderWidth: 2 },
                { type: 'bar', label: 'Pests', data, backgroundColor: 'rgba(10, 132, 255, 0.30)', borderRadius: 4, borderSkipped: false },
            ],
        },
        options: chartOpts('Date', 'Pests'),
    });
}

function renderByCam(items) {
    // v3.6.2: count bboxes per camera, not records.
    const byCam = {};
    let total = 0;
    for (const it of items) {
        const n = getCountedBoxes(it).length;
        if (n === 0) continue;
        const cam = it.camera_id || 'unknown';
        byCam[cam] = (byCam[cam] || 0) + n;
        total += n;
    }
    total = Math.max(1, total);
    const el = document.getElementById('by-cam');
    const entries = Object.entries(byCam).sort((a, b) => b[1] - a[1]);
    if (entries.length === 0) {
        el.innerHTML = '<div style="color:var(--muted);font-size:13px;padding:10px 0;">No pests in this range.</div>';
        return;
    }
    el.innerHTML = entries.map(([cam, count]) => {
        const pct = (count / total * 100).toFixed(1);
        return `<div style="margin-bottom:12px;">
            <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px;">
                <strong>${escapeHtml(camDisplayName(cam))}</strong>
                <span class="mono">${count} · ${pct}%</span>
            </div>
            <div style="background:var(--bg);border-radius:3px;height:7px;overflow:hidden;">
                <div style="background:var(--teal);height:100%;width:${pct}%;"></div>
            </div>
        </div>`;
    }).join('');
}
