// Live Passenger Monitoring & Timeline Review
let currentPassengerId = null;

document.addEventListener("DOMContentLoaded", () => {
    loadMonitoringJourneys();
    loadPassengers();
});

async function loadMonitoringJourneys() {
    try {
        const res = await fetch("/api/journeys");
        if (!res.ok) return;
        const journeys = await res.json();
        const select = document.getElementById("filter-journey");
        if (!select) return;

        journeys.forEach(j => {
            const opt = document.createElement("option");
            opt.value = j.id;
            opt.textContent = `${j.journey_code} (${j.origin} -> ${j.destination})`;
            select.appendChild(opt);
        });
    } catch (e) {
        console.error(e);
    }
}

async function loadPassengers() {
    const journeyId = document.getElementById("filter-journey")?.value || "";
    const status = document.getElementById("filter-status")?.value || "";
    const search = document.getElementById("search-passenger")?.value || "";

    const url = new URL("/api/passengers", window.location.origin);
    if (journeyId) url.searchParams.append("journey_id", journeyId);
    if (status) url.searchParams.append("status", status);
    if (search) url.searchParams.append("search", search);

    try {
        const res = await fetch(url);
        if (!res.ok) return;
        const passengers = await res.json();
        renderPassengersTable(passengers);
    } catch (e) {
        console.error("Failed to load passengers:", e);
    }
}

function renderPassengersTable(passengers) {
    const tbody = document.getElementById("passengers-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";

    // Update Top Summary Counts (Kitne Aaye, Present Kitne Hai, Kitne Gaye)
    const list = passengers || [];
    const totalCount = list.length;
    const presentCount = list.filter(p => p.status === "PRESENT").length;
    const exitedCount = list.filter(p => (p.status || "").includes("EXIT")).length;
    const reviewCount = list.filter(p => (p.status || "").includes("REVIEW") || (p.status || "").includes("POSSIBLY")).length;

    const elTotal = document.getElementById("dir-total-count");
    const elPresent = document.getElementById("dir-present-count");
    const elExited = document.getElementById("dir-exited-count");
    const elReview = document.getElementById("dir-review-count");

    if (elTotal) elTotal.textContent = totalCount;
    if (elPresent) elPresent.textContent = presentCount;
    if (elExited) elExited.textContent = exitedCount;
    if (elReview) elReview.textContent = reviewCount;

    if (!passengers || passengers.length === 0) {
        tbody.innerHTML = `<tr><td colspan="8" class="text-center py-4" style="color:var(--text-muted)">No analysis data available. Start a new analysis.</td></tr>`;
        return;
    }

    passengers.forEach(p => {
        const tr = document.createElement("tr");

        let badgeClass = "badge-present";
        if (p.status === "POSSIBLY_ABSENT") badgeClass = "badge-possibly-absent";
        else if (p.status === "EXIT_UNCONFIRMED") badgeClass = "badge-exit-unconfirmed";
        else if (p.status === "EXIT_CONFIRMED") badgeClass = "badge-exit-confirmed";
        else if (p.status === "NEEDS_REVIEW") badgeClass = "badge-needs-review";

        const locText = (p.last_latitude !== null && p.last_longitude !== null)
            ? `${p.last_latitude.toFixed(4)}, ${p.last_longitude.toFixed(4)}`
            : `<span style="color:var(--text-muted)">Missing GPS</span>`;

        tr.innerHTML = `
            <td>
                ${p.thumbnail_url ? `<img src="${p.thumbnail_url}" class="passenger-thumb" alt="${p.person_id}">` : `<div class="user-avatar">${p.person_id.substring(0, 2)}</div>`}
            </td>
            <td><strong>${p.person_id}</strong></td>
            <td><span class="badge-status ${badgeClass}">${p.status.replace("_", " ")}</span></td>
            <td>
                <div>${p.first_seen_time || 'N/A'}</div>
                <small style="color:var(--text-muted);font-size:11px;">(Est. Boarding)</small>
            </td>
            <td>
                <div>${p.last_seen_time || 'N/A'}</div>
                <small style="color:var(--text-muted);font-size:11px;">(Last Confirmed)</small>
            </td>
            <td>${locText}</td>
            <td>
                <span class="badge-status badge-present">${p.total_observations} Confirmed</span>
                ${p.consecutive_missed_count > 0 ? `<span class="badge-status badge-possibly-absent">${p.consecutive_missed_count} Missed</span>` : ''}
            </td>
            <td>
                <button class="btn-primary" style="padding:5px 10px;font-size:12px;" onclick="openPassengerModal(${p.id})">
                    <i class="bi bi-clock-history"></i> Timeline
                </button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

async function openPassengerModal(passengerId) {
    currentPassengerId = passengerId;
    const modal = document.getElementById("passenger-modal");
    if (!modal) return;

    try {
        const res = await fetch(`/api/passengers/${passengerId}`);
        if (!res.ok) return;
        const p = await res.json();

        document.getElementById("modal-passenger-id").textContent = `${p.person_id} Journey Timeline`;
        document.getElementById("modal-status-badge").textContent = p.status.replace("_", " ");
        document.getElementById("modal-first-seen").textContent = p.first_seen_time || "N/A";
        document.getElementById("modal-last-seen").textContent = p.last_seen_time || "N/A";

        const bLoc = p.boarding_stop_name || ((p.first_latitude && p.first_longitude) ? `${p.first_latitude.toFixed(6)}, ${p.first_longitude.toFixed(6)}` : "Boarding Stop");
        const eLoc = p.exit_stop_name || ((p.last_latitude && p.last_longitude) ? `${p.last_latitude.toFixed(6)}, ${p.last_longitude.toFixed(6)}` : "In-Transit / Destination");
        const stopsCount = p.stops_traveled || 1;

        document.getElementById("modal-first-gps").innerHTML = `<span><i class="bi bi-geo-alt-fill text-primary"></i> ${bLoc}</span>`;
        document.getElementById("modal-last-gps").innerHTML = `<span><i class="bi bi-geo-alt-fill text-success"></i> ${eLoc}</span> <span class="badge bg-primary ms-1" style="font-size:10px;">${stopsCount} Stop(s) Traveled</span>`;

        // Render photo observations gallery (Only genuine face crops for this passenger)
        const obsContainer = document.getElementById("modal-observations-list");
        obsContainer.innerHTML = "";
        const faceObs = (p.observations || []).filter(o => o.has_face || (o.crop_url && o.crop_url.includes("face_")));
        if (faceObs.length > 0) {
            faceObs.forEach(o => {
                const item = document.createElement("div");
                item.style.cssText = "display:inline-block;margin-right:12px;margin-bottom:12px;text-align:center;";
                item.innerHTML = `
                    <img src="${o.crop_url}" class="passenger-thumb" style="width:64px;height:64px;object-fit:cover;border-radius:6px;border:2px solid #3b82f6;" alt="Face Crop" title="Obs #${o.id} (Angle ${o.camera_angle || 'A'})">
                    <div style="font-size:11px;font-weight:600;margin-top:2px;">Angle ${o.camera_angle || 'A'}</div>
                    <div style="font-size:10px;color:var(--text-muted);">${o.similarity_score ? Math.round(o.similarity_score*100) + '%' : 'Confirmed'}</div>
                `;
                obsContainer.appendChild(item);
            });
        } else {
            obsContainer.innerHTML = `<span style="color:var(--text-muted);font-size:12px;">No face crop observations recorded for this passenger.</span>`;
        }

        // Render timeline events
        const timelineList = document.getElementById("modal-timeline-list");
        timelineList.innerHTML = "";
        if (p.timeline_events && p.timeline_events.length > 0) {
            p.timeline_events.forEach(ev => {
                const item = document.createElement("div");
                item.className = "timeline-item";
                let markerColor = "#10b981";
                let badgeStyle = "background:#dcfce7;color:#15803d;";
                let eventTitle = ev.event_type.replace(/_/g, ' ');

                if (ev.event_type.includes("BOARD")) {
                    markerColor = "#3b82f6";
                    badgeStyle = "background:#dbeafe;color:#1d4ed8;";
                    eventTitle = "🟢 Boarded";
                } else if (ev.event_type.includes("EXIT")) {
                    markerColor = "#ef4444";
                    badgeStyle = "background:#fee2e2;color:#b91c1c;";
                    eventTitle = "🔴 Alighted (Journey Complete)";
                } else if (ev.event_type.includes("PRESENT")) {
                    markerColor = "#10b981";
                    badgeStyle = "background:#dcfce7;color:#15803d;";
                    eventTitle = "🔵 In Transit";
                }

                item.innerHTML = `
                    <div class="timeline-marker" style="background:${markerColor};width:10px;height:10px;border-radius:50%;margin-top:4px;"></div>
                    <div class="timeline-content" style="margin-left:14px;padding-bottom:12px;">
                        <div class="timeline-time" style="font-size:11px;color:var(--text-muted);font-weight:600;">${ev.event_time}</div>
                        <span class="badge-status" style="${badgeStyle};font-size:11px;padding:2px 8px;border-radius:4px;display:inline-block;margin:2px 0;">${eventTitle}</span>
                        <p style="font-size:12px;color:var(--text-main);margin:4px 0 0 0;font-weight:500;">${ev.notes || ''}</p>
                    </div>
                `;
                timelineList.appendChild(item);
            });
        }

        modal.classList.add("active");

    } catch (e) {
        console.error("Modal load error:", e);
    }
}

function closePassengerModal() {
    const modal = document.getElementById("passenger-modal");
    if (modal) modal.classList.remove("active");
    currentPassengerId = null;
}

async function submitManualReview(newStatus) {
    if (!currentPassengerId) return;
    const comment = prompt(`Enter operator notes/rationale for updating status to ${newStatus}:`, "Confirmed by bus operator inspection.");
    if (comment === null) return; // User cancelled

    try {
        const res = await fetch(`/api/passengers/${currentPassengerId}/review`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                status: newStatus,
                comment: comment
            })
        });

        if (res.ok) {
            showToast(`Status updated to ${newStatus}.`, "success");
            closePassengerModal();
            loadPassengers();
        } else {
            const err = await res.json();
            showToast(err.error || "Failed to update review status.", "error");
        }
    } catch (e) {
        showToast("Error updating passenger record.", "error");
    }
}
