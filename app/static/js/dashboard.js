// Dashboard JavaScript
let trendChart = null;

document.addEventListener("DOMContentLoaded", () => {
    loadDashboardData();
    window.addEventListener("themeChanged", () => {
        if (trendChart) {
            updateChartTheme();
        }
    });
});

async function loadDashboardData() {
    try {
        const res = await fetch("/api/dashboard/stats");
        if (!res.ok) return;
        const data = await res.json();

        // Update metrics
        document.getElementById("kpi-total-passengers").textContent = data.metrics.total_identified;
        document.getElementById("kpi-confirmed-present").textContent = data.metrics.confirmed_present;
        document.getElementById("kpi-possibly-absent").textContent = data.metrics.possibly_absent;
        document.getElementById("kpi-photos-processed").textContent = data.metrics.photos_processed;

        // Active journey badge
        const jTitle = document.getElementById("active-journey-title");
        if (jTitle) {
            if (data.current_journey) {
                jTitle.textContent = `${data.current_journey.journey_code} (${data.current_journey.bus_number || 'Bus'}) - ${data.current_journey.origin} to ${data.current_journey.destination}`;
            } else {
                jTitle.textContent = "No Active Journey. Click 'Journeys' to start one.";
            }
        }

        // Render Chart
        renderTrendChart(data.trend);

        // Render Recent Uploads
        renderRecentUploads(data.recent_uploads);

        // Render Recent Events
        renderRecentEvents(data.recent_events);

    } catch (e) {
        console.error("Dashboard error:", e);
    }
}

function renderTrendChart(trend) {
    const ctx = document.getElementById("presenceTrendChart");
    if (!ctx) return;

    const isDark = document.documentElement.getAttribute("data-theme") === "dark";
    const textColor = isDark ? "#94a3b8" : "#64748b";
    const gridColor = isDark ? "#334155" : "#e2e8f0";

    const labels = (trend.labels && trend.labels.length > 0) ? trend.labels : ["No uploads yet"];
    const counts = (trend.present_counts && trend.present_counts.length > 0) ? trend.present_counts : [0];

    if (trendChart) {
        trendChart.destroy();
    }

    trendChart = new Chart(ctx, {
        type: "line",
        data: {
            labels: labels,
            datasets: [{
                label: "Visible Confirmed Passengers",
                data: counts,
                borderColor: "#3b82f6",
                backgroundColor: "rgba(59, 130, 246, 0.12)",
                fill: true,
                tension: 0.35,
                borderWidth: 2.5,
                pointBackgroundColor: "#3b82f6",
                pointRadius: 4,
                pointHoverRadius: 6
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    display: true,
                    labels: { color: textColor, font: { family: "-apple-system, sans-serif", size: 12 } }
                },
                tooltip: {
                    backgroundColor: isDark ? "#1e293b" : "#ffffff",
                    titleColor: isDark ? "#f8fafc" : "#1e293b",
                    bodyColor: isDark ? "#94a3b8" : "#475569",
                    borderColor: gridColor,
                    borderWidth: 1
                }
            },
            scales: {
                x: {
                    grid: { color: gridColor },
                    ticks: { color: textColor }
                },
                y: {
                    beginAtZero: true,
                    grid: { color: gridColor },
                    ticks: {
                        color: textColor,
                        stepSize: 1,
                        precision: 0
                    }
                }
            }
        }
    });
}

function updateChartTheme() {
    if (!trendChart) return;
    const isDark = document.documentElement.getAttribute("data-theme") === "dark";
    const textColor = isDark ? "#94a3b8" : "#64748b";
    const gridColor = isDark ? "#334155" : "#e2e8f0";

    trendChart.options.plugins.legend.labels.color = textColor;
    trendChart.options.scales.x.grid.color = gridColor;
    trendChart.options.scales.x.ticks.color = textColor;
    trendChart.options.scales.y.grid.color = gridColor;
    trendChart.options.scales.y.ticks.color = textColor;
    trendChart.update();
}

function renderRecentUploads(uploads) {
    const tbody = document.getElementById("recent-uploads-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";

    if (!uploads || uploads.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" class="text-center py-4" style="color:var(--text-muted)">No bus interior photos uploaded yet. Click 'Upload Bus Photo' to begin.</td></tr>`;
        return;
    }

    uploads.forEach(up => {
        const tr = document.createElement("tr");
        const gpsBadge = (up.latitude !== null && up.longitude !== null)
            ? `<span class="badge-status badge-present"><i class="bi bi-geo-alt"></i> ${up.latitude.toFixed(4)}, ${up.longitude.toFixed(4)}</span>`
            : `<span class="badge-status badge-needs-review"><i class="bi bi-geo-alt-fill"></i> No GPS</span>`;

        tr.innerHTML = `
            <td><strong>#${up.id}</strong></td>
            <td>${up.capture_timestamp || up.upload_timestamp}</td>
            <td><span class="badge-status badge-present">${up.passengers_detected_count} Detected</span></td>
            <td>${gpsBadge} <small style="color:var(--text-muted);font-size:11px;">(${up.location_source})</small></td>
            <td>
                ${up.annotated_path ? `<a href="/data/${up.annotated_path}" target="_blank" class="btn-secondary" style="padding:4px 8px;font-size:12px;"><i class="bi bi-eye"></i> View HUD</a>` : '-'}
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function renderRecentEvents(events) {
    const container = document.getElementById("recent-events-list");
    if (!container) return;
    container.innerHTML = "";

    if (!events || events.length === 0) {
        container.innerHTML = `<div style="color:var(--text-muted);padding:16px 0;">No presence events recorded yet.</div>`;
        return;
    }

    events.forEach(e => {
        const div = document.createElement("div");
        div.className = "timeline-item";

        let badgeClass = "badge-present";
        if (e.event_type === "POSSIBLY_ABSENT") badgeClass = "badge-possibly-absent";
        else if (e.event_type === "EXIT_UNCONFIRMED") badgeClass = "badge-exit-unconfirmed";
        else if (e.event_type === "EXIT_CONFIRMED") badgeClass = "badge-exit-confirmed";

        div.innerHTML = `
            <div class="timeline-marker"></div>
            <div class="timeline-content">
                <div class="timeline-time">${e.event_time}</div>
                <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
                    <strong>${e.person_id || 'Passenger'}</strong>
                    <span class="badge-status ${badgeClass}">${e.event_type.replace('_', ' ')}</span>
                </div>
                <div style="font-size:13px;color:var(--text-muted);">${e.notes || ''}</div>
            </div>
        `;
        container.appendChild(div);
    });
}
