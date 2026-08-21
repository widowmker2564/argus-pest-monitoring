/* ==============================================================
   PAGE: COSTS — NOT in TABS (removed: the shared-nbk2 IAM user has
   no Cost Explorer access). Functions kept verbatim from v3_9 so
   the tab can be re-enabled by adding it back to TABS in main.js.
   Split from dashboard_v3_9.html (v4.0 module split).
   ============================================================== */
import { state } from './state.js';
import { api } from './api.js';
import { escapeHtml, fmtDate, chartOpts } from './utils.js';

export async function renderCostsPage() {
    const content = document.getElementById('page-content');
    if (!state.costRange) state.costRange = 30;  // default 30 days
    content.innerHTML = '<div class="loading-wrapper"><span class="spinner"></span> Loading costs…</div>';
    try {
        const data = await api.getCost(state.costRange);
        state.costData = data;
        renderCostContent(data);
    } catch (err) {
        content.innerHTML = `<div class="empty-state"><h3>Cost data unavailable</h3><p>${escapeHtml(err.message)}</p><p style="margin-top:8px;font-family:var(--mono);font-size:11px;">Lambda needs <code>ce:GetCostAndUsage</code> permission.</p></div>`;
    }
}

const COST_RANGES = [
    { days: 7,   label: '7 days' },
    { days: 30,  label: '30 days' },
    { days: 90,  label: '90 days' },
    { days: 180, label: '6 months' },
    { days: 365, label: '12 months' },
];

function renderCostContent(data) {
    const buckets = data.buckets || data.daily || [];
    const isMonthly = data.granularity === 'MONTHLY';

    // Range filter pills
    const rangePills = COST_RANGES.map(r =>
        `<span class="chip ${state.costRange === r.days ? 'active' : ''}" onclick="changeCostRange(${r.days})">${r.label}</span>`
    ).join('');

    // Day count for label
    const days = data.period.days || (state.costRange);
    const headerLabel = isMonthly ? `Last ${days} days · monthly` : `Last ${days} days · daily`;

    // Service rows with usage info
    const totalCost = data.grand_total || 1;
    const serviceRows = Object.entries(data.service_totals).map(([svc, amt]) => {
        const usage = data.service_usage?.[svc];
        const pct = (amt / totalCost * 100).toFixed(1);
        return `<div class="service-row-detailed">
            <div class="service-row-head">
                <span class="service-name">${escapeHtml(svc)}</span>
                <span class="service-amount mono">$${amt.toFixed(4)}</span>
            </div>
            <div class="service-row-bar">
                <div class="service-row-fill" style="width:${pct}%"></div>
            </div>
            <div class="service-row-meta">
                <span class="mono" style="color:var(--muted);">${pct}% of total</span>
                ${usage ? `<span class="mono" style="color:var(--muted);">${usage.toFixed(2)} units</span>` : ''}
            </div>
        </div>`;
    }).join('');

    // MTD card
    const mtdHtml = data.month_to_date != null ? `
        <div class="cost-mtd-card">
            <div class="cost-label">Month to date</div>
            <div class="cost-mtd-value mono">$${data.month_to_date.toFixed(2)}</div>
            <div class="cost-period">since ${new Date().toISOString().slice(0,7)}-01</div>
        </div>` : '';

    document.getElementById('page-actions').innerHTML = `
        <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">
            <span style="font-size:11px;color:var(--muted);text-transform:uppercase;font-weight:500;letter-spacing:0.06em;">Range</span>
            ${rangePills}
            <button class="btn btn-outline btn-sm" onclick="renderCostsPage()" title="Refresh">⟳</button>
        </div>
    `;

    document.getElementById('page-content').innerHTML = `
        <div class="cost-hero-grid">
            <div class="cost-hero">
                <div class="cost-label">${headerLabel}</div>
                <div class="cost-big mono">$${data.grand_total.toFixed(2)}</div>
                <div class="cost-period">${data.period.start} → ${data.period.end}</div>
            </div>
            ${mtdHtml}
        </div>

        <div class="chart-grid">
            <div class="card">
                <div class="card-head">
                    <div>
                        <div class="card-title">${isMonthly ? 'Monthly' : 'Daily'} spend</div>
                        <div class="card-sub">${escapeHtml(data.note || '')}</div>
                    </div>
                </div>
                <div class="chart-canvas-wrap"><canvas id="cost-chart"></canvas></div>
            </div>

            <div class="card">
                <div class="card-head">
                    <div>
                        <div class="card-title">By service · ${Object.keys(data.service_totals).length} services</div>
                        <div class="card-sub">Cost · share · usage units</div>
                    </div>
                </div>
                <div class="service-list">${serviceRows || '<div style="color:var(--muted);font-size:13px;padding:12px 0;">No costs in this period.</div>'}</div>
            </div>

            <div class="card">
                <div class="card-head">
                    <div>
                        <div class="card-title">Per-${isMonthly ? 'month' : 'day'} breakdown</div>
                        <div class="card-sub">Newest first</div>
                    </div>
                </div>
                <div style="overflow-x:auto;max-height:420px;">
                    <table class="logs-table" style="width:100%;">
                        <thead>
                            <tr>
                                <th>${isMonthly ? 'Month' : 'Date'}</th>
                                <th style="text-align:right;">Total</th>
                                <th>Top service</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${[...buckets].reverse().map(b => {
                                const top = Object.entries(b.services)
                                    .sort((a,c) => c[1].cost - a[1].cost)[0];
                                return `<tr>
                                    <td>${isMonthly ? b.date.slice(0,7) : fmtDate(b.date)}</td>
                                    <td style="text-align:right;">$${b.total.toFixed(4)}</td>
                                    <td style="color:var(--muted);">${top ? escapeHtml(top[0]) + ' · $' + top[1].cost.toFixed(4) : '—'}</td>
                                </tr>`;
                            }).join('')}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    `;

    if (state.costChart) state.costChart.destroy();
    state.costChart = new Chart(document.getElementById('cost-chart'), {
        type: 'bar',
        data: {
            labels: buckets.map(b => isMonthly ? b.date.slice(0,7) : fmtDate(b.date)),
            datasets: [{ data: buckets.map(b => b.total), backgroundColor: '#0f766e', borderRadius: 4, borderSkipped: false }],
        },
        options: { ...chartOpts(isMonthly ? 'Month' : 'Date', 'USD'),
            plugins: { ...chartOpts().plugins, tooltip: { backgroundColor: '#1c1917', callbacks: { label: c => '$' + c.parsed.y.toFixed(4) } } },
        },
    });
}

export function changeCostRange(days) {
    state.costRange = days;
    renderCostsPage();
}
