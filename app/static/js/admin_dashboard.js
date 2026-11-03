// ============================================================================
// Admin Bus Monitoring Dashboard JavaScript
// Dynamic bus filtering, 8 summary cards, Leaflet fleet map, real-time polling
// ============================================================================

let adminMap = null;
let adminMarkersLayer = null;
let currentSelectedBusId = "all";
let adminPollTimer = null;

document.addEventListener("DOMContentLoaded", () => {
    initAdminMap();
    initClock();
    loadBusDropdown().then(() => {
        loadAdminDashboardData();
    });

    // Auto-refresh every 20 seconds
    adminPollTimer = setInterval(() => {
        loadAdminDashboardData(true);
    }, 20000);
});

function initClock() {
    const clockEl = document.getElementById("live-clock");
    if (!clockEl) return;
    const update = () => {
        const now = new Date();
        clockEl.textContent = now.toLocaleDateString() + " " + now.toLocaleTimeString();
    };
    update();
    setInterval(update, 1000);
}

function initAdminMap() {
    const mapEl = document.getElementById("admin-map");
    if (!mapEl) return;

    // Default center (San Francisco transit corridor)
    adminMap = L.map("admin-map").setView([37.7749, -122.4194], 12);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution: '&copy; <a href="https://openstreetmap.org">OpenStreetMap</a> contributors'
    }).addTo(adminMap);

    adminMarkersLayer = L.layerGroup().addTo(adminMap);
}

async function loadBusDropdown() {
    try {
        const res = await fetch("/api/buses");
        if (!res.ok) return;
        const buses = await res.json();
        const select = document.getElementById("admin-bus-select");
        if (!select) return;

        select.innerHTML = `<option value="all">-- All Buses (Aggregated Fleet View) --</option>`;
        buses.forEach(b => {
            const opt = document.createElement("option");
            opt.value = b.id;
            opt.textContent = `${b.registration_number || b.bus_number} - ${b.bus_name || 'Bus'} (${b.route_name || 'Route'}) [${b.bus_status || 'Active'}]`;
            select.appendChild(opt);
        });
    } catch (e) {
        console.error("Failed to load buses dropdown:", e);
    }
}

function onAdminBusSelectionChange() {
    const select = document.getElementById("admin-bus-select");
    currentSelectedBusId = select.value;
    loadAdminDashboardData();
}

function resetToAllBuses() {
    const select = document.getElementById("admin-bus-select");
    if (select) select.value = "all";
    currentSelectedBusId = "all";
    loadAdminDashboardData();
}

function selectSpecificBus(busId) {
    const select = document.getElementById("admin-bus-select");
    if (select) select.value = busId;
    currentSelectedBusId = busId;
    loadAdminDashboardData();
    scrollToSection("selected-bus-panel");
}

function scrollToSection(sectionId) {
    const el = document.getElementById(sectionId);
    if (el) {
        el.scrollIntoView({ behavior: "smooth" });
    }
}

async function loadAdminDashboardData(isBackgroundPoll = false) {
    try {
        const url = new URL("/api/dashboard/stats", window.location.origin);
        if (currentSelectedBusId && currentSelectedBusId !== "all") {
            url.searchParams.append("bus_id", currentSelectedBusId);
        }

        const res = await fetch(url);
        if (!res.ok) return;
        const data = await res.json();

        // 1. Update 8 Summary Cards
        updateSummaryCards(data.summary_cards);

        // 2. Update Bus Filter Badge & Map Scope
        const filterBadge = document.getElementById("bus-filter-badge");
        const mapBadge = document.getElementById("map-scope-badge");
        if (data.selected_bus_details && data.selected_bus_details.bus) {
            const reg = data.selected_bus_details.bus.registration_number;
            if (filterBadge) filterBadge.textContent = `Bus: ${reg}`;
            if (mapBadge) mapBadge.textContent = `Bus: ${reg}`;
        } else {
            if (filterBadge) filterBadge.textContent = "All Buses Active";
            if (mapBadge) mapBadge.textContent = "All Buses";
        }

        // 3. Update Live Bus Map
        renderAdminMap(data.map_buses, data.selected_bus_id);

        // 4. Update Selected Bus Details Panel
        renderSelectedBusPanel(data.selected_bus_details);

        // 5. Update Active Buses & Journeys Table
        renderActiveBusesTable(data.active_buses);

        // 6. Update Recent Activity Feed
        renderRecentActivity(data.recent_activity);

    } catch (e) {
        console.error("Dashboard data load error:", e);
    }
}

function updateSummaryCards(cards) {
    if (!cards) return;
    document.getElementById("card-total-buses").textContent = cards.total_registered_buses;
    document.getElementById("card-active-buses").textContent = cards.active_buses_count;
    document.getElementById("card-recent-gps").textContent = cards.buses_with_recent_gps;
    document.getElementById("card-total-passengers").textContent = cards.total_passengers_identified;
    document.getElementById("card-confirmed-present").textContent = cards.currently_confirmed_present;
    document.getElementById("card-confirmed-boardings").textContent = cards.total_confirmed_boardings;
    document.getElementById("card-confirmed-exits").textContent = cards.total_confirmed_exits;
    document.getElementById("card-needs-verification").textContent = cards.needs_verification;
}

function renderAdminMap(buses, selectedBusId) {
    if (!adminMap || !adminMarkersLayer) return;
    adminMarkersLayer.clearLayers();

    const emptyOverlay = document.getElementById("admin-map-empty-state");
    const emptyText = document.getElementById("admin-map-empty-text");

    const validBuses = (buses || []).filter(b => b.latitude !== null && b.longitude !== null);

    if (validBuses.length === 0) {
        if (emptyOverlay) {
            emptyOverlay.style.display = "block";
            if (selectedBusId) {
                emptyText.textContent = "No valid GPS location is currently available for this bus.";
            } else {
                emptyText.textContent = "No buses in the fleet have reported valid GPS coordinates yet.";
            }
        }
        return;
    }

    if (emptyOverlay) emptyOverlay.style.display = "none";

    const latlngs = [];
    let focusedMarker = null;

    validBuses.forEach(b => {
        const latlng = [b.latitude, b.longitude];
        latlngs.push(latlng);

        // Style marker based on freshness: Live (green), Stale (amber), Offline (gray), Manual (blue)
        let markerBg = "#10b981"; // Live Emerald Green
        let pulseHtml = `<span style="position:absolute;width:100%;height:100%;border-radius:50%;background:#10b981;opacity:0.4;animation:pulseMarker 1.8s infinite;"></span>`;

        if (b.location_freshness === "Stale") {
            markerBg = "#f59e0b"; // Amber
            pulseHtml = "";
        } else if (b.location_freshness === "Offline") {
            markerBg = "#64748b"; // Slate Gray
            pulseHtml = "";
        } else if (b.location_freshness === "Manual Location") {
            markerBg = "#3b82f6"; // Blue
            pulseHtml = "";
        }

        const isCurrentSelected = (b.bus_id === selectedBusId);
        const borderStyle = isCurrentSelected ? "3px solid #ffff00" : "2px solid #ffffff";
        const iconSize = isCurrentSelected ? 38 : 32;

        const customIcon = L.divIcon({
            className: "admin-bus-map-icon",
            html: `
                <div style="position:relative;width:${iconSize}px;height:${iconSize}px;display:flex;align-items:center;justify-content:center;">
                    ${pulseHtml}
                    <div style="background:${markerBg};color:#ffffff;width:${iconSize}px;height:${iconSize}px;border-radius:50%;border:${borderStyle};box-shadow:0 3px 8px rgba(0,0,0,0.35);display:flex;align-items:center;justify-content:center;font-size:${iconSize*0.45}px;z-index:2;">
                        <i class="bi bi-bus-front"></i>
                    </div>
                </div>
            `,
            iconSize: [iconSize, iconSize],
            iconAnchor: [iconSize / 2, iconSize / 2]
        });

        const popupContent = `
            <div style="font-family:-apple-system, sans-serif; font-size:13px; line-height:1.45; min-width:210px;">
                <div style="display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #e2e8f0;padding-bottom:5px;margin-bottom:6px;">
                    <strong style="font-size:14px;color:#1e293b;">${b.registration_number}</strong>
                    <span style="font-size:11px;padding:2px 6px;border-radius:10px;background:${markerBg};color:#fff;font-weight:600;">${b.location_freshness}</span>
                </div>
                <div><strong>Bus Name:</strong> ${b.bus_name}</div>
                <div><strong>Route:</strong> ${b.route_name}</div>
                <div><strong>Operator:</strong> ${b.operator_name}</div>
                <div><strong>Journey:</strong> ${b.journey_code}</div>
                <div><strong>Confirmed Passengers:</strong> ${b.confirmed_passenger_count}</div>
                <div><strong>GPS Source:</strong> ${b.location_source} ${b.accuracy ? `(±${Math.round(b.accuracy)}m)` : ''}</div>
                <div style="font-size:11px;color:#64748b;margin-top:4px;">Last Update: ${b.location_time}</div>
                <div style="margin-top:8px;text-align:right;">
                    <button class="btn-primary" style="padding:4px 8px;font-size:11px;" onclick="selectSpecificBus(${b.bus_id})">
                        <i class="bi bi-eye"></i> View Bus Details
                    </button>
                </div>
            </div>
        `;

        const marker = L.marker(latlng, { icon: customIcon }).bindPopup(popupContent);
        adminMarkersLayer.addLayer(marker);

        if (isCurrentSelected) {
            focusedMarker = marker;
        }
    });

    if (focusedMarker) {
        adminMap.setView(focusedMarker.getLatLng(), 15);
        focusedMarker.openPopup();
    } else if (latlngs.length > 1) {
        adminMap.fitBounds(L.latLngBounds(latlngs), { padding: [40, 40] });
    } else if (latlngs.length === 1) {
        adminMap.setView(latlngs[0], 14);
    }
}

function renderSelectedBusPanel(details) {
    const panel = document.getElementById("selected-bus-panel");
    if (!panel) return;

    if (!details || !details.bus) {
        panel.style.display = "none";
        return;
    }

    panel.style.display = "block";
    const b = details.bus;
    const j = details.active_journey;

    document.getElementById("panel-bus-title").textContent = `${b.registration_number} - ${b.bus_name}`;
    document.getElementById("panel-bus-status-badge").textContent = b.bus_status || "Active";
    
    const freshnessBadge = document.getElementById("panel-bus-freshness-badge");
    freshnessBadge.textContent = b.location_freshness;
    if (b.location_freshness === "Live") {
        freshnessBadge.className = "badge-status badge-present";
    } else if (b.location_freshness === "Stale") {
        freshnessBadge.className = "badge-status badge-possibly-absent";
    } else if (b.location_freshness === "Manual Location") {
        freshnessBadge.className = "badge-status badge-conf-med";
    } else {
        freshnessBadge.className = "badge-status badge-exit-confirmed";
    }

    document.getElementById("panel-reg-num").textContent = b.registration_number;
    document.getElementById("panel-bus-type").textContent = `${b.bus_type} • Cap: ${b.capacity}`;
    document.getElementById("panel-route").textContent = b.route_name || "Standard Route";
    document.getElementById("panel-terminals").textContent = `${b.starting_point || 'Origin'} ➔ ${b.destination || 'Destination'}`;
    document.getElementById("panel-operator").textContent = b.operator_name || "Unassigned";
    document.getElementById("panel-operator-contact").textContent = b.operator_contact ? `Tel: ${b.operator_contact}` : "No contact";

    if (j) {
        document.getElementById("panel-journey-code").textContent = j.journey_code;
        document.getElementById("panel-journey-time").textContent = `Started: ${j.start_time || '-'}`;
    } else {
        document.getElementById("panel-journey-code").textContent = "No Active Journey";
        document.getElementById("panel-journey-time").textContent = "Idle at depot/stop";
    }

    if (b.latest_latitude !== null && b.latest_longitude !== null) {
        document.getElementById("panel-gps-coords").textContent = `${b.latest_latitude.toFixed(5)}, ${b.latest_longitude.toFixed(5)}`;
        document.getElementById("panel-gps-updated").textContent = `Updated: ${b.latest_location_time || '-'} (${b.latest_location_source || 'DEVICE_API'})`;
    } else {
        document.getElementById("panel-gps-coords").textContent = "No GPS Data";
        document.getElementById("panel-gps-updated").textContent = "Device location not reported";
    }

    // Render 3D Bus Interior Cabin
    render3DBusCabin(passengers, b.capacity || 40);

    // Render passengers table
    const tbody = document.getElementById("panel-passengers-tbody");
    tbody.innerHTML = "";

    if (passengers.length === 0) {
        tbody.innerHTML = `<tr><td colspan="9" class="text-center py-4" style="color:var(--text-muted)">No passenger identities recorded for this bus's active journey yet.</td></tr>`;
        return;
    }

    passengers.forEach(p => {
        const tr = document.createElement("tr");

        let statusClass = "badge-present";
        if (p.status === "POSSIBLY_ABSENT") statusClass = "badge-possibly-absent";
        else if (p.status === "EXIT_UNCONFIRMED") statusClass = "badge-exit-unconfirmed";
        else if (p.status === "EXIT_CONFIRMED") statusClass = "badge-exit-confirmed";
        else if (p.status === "NEEDS_REVIEW") statusClass = "badge-needs-review";

        tr.innerHTML = `
            <td><span class="badge-status badge-present" style="font-weight:800;font-size:12px;background:var(--accent-primary-light);color:var(--accent-primary);"><i class="bi bi-geo-fill"></i> ${p.current_seat || '-'}</span></td>
            <td><strong>${p.person_id}</strong></td>
            <td>${p.first_seen_time}</td>
            <td>${p.last_seen_time}</td>
            <td><span class="badge-status ${statusClass}">${p.status.replace('_', ' ')}</span></td>
            <td><span class="badge-status badge-present">${p.boarding_status}</span></td>
            <td><span class="badge-status ${p.exit_status === 'Confirmed' ? 'badge-exit-confirmed' : 'badge-possibly-absent'}">${p.exit_status}</span></td>
            <td><span class="badge-conf-high">${p.confidence}</span></td>
            <td>
                <div style="display:flex;gap:4px;">
                    <button class="btn-secondary" style="padding:4px 8px;font-size:12px;" onclick="openPassengerModal(${p.id})">
                        <i class="bi bi-clock-history"></i> Timeline
                    </button>
                    <button class="btn-secondary" style="padding:4px 6px;font-size:12px;" onclick="openSeatInspection('${p.current_seat}', ${JSON.stringify(p).replace(/"/g, '&quot;')})" title="Move Seat">
                        <i class="bi bi-arrow-left-right"></i>
                    </button>
                </div>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function renderActiveBusesTable(activeBuses) {
    const tbody = document.getElementById("active-buses-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";

    if (!activeBuses || activeBuses.length === 0) {
        tbody.innerHTML = `<tr><td colspan="9" class="text-center py-4" style="color:var(--text-muted)">No registered buses found in fleet.</td></tr>`;
        return;
    }

    activeBuses.forEach(b => {
        const tr = document.createElement("tr");

        let freshBadge = `<span class="badge-status badge-present">Live</span>`;
        if (b.latest_location_freshness === "Stale") freshBadge = `<span class="badge-status badge-possibly-absent">Stale</span>`;
        else if (b.latest_location_freshness === "Manual Location") freshBadge = `<span class="badge-status badge-conf-med">Manual</span>`;
        else if (b.latest_location_freshness === "Offline") freshBadge = `<span class="badge-status badge-exit-confirmed">Offline</span>`;

        tr.innerHTML = `
            <td><strong>${b.registration_number}</strong></td>
            <td>${b.bus_name} <br><small style="color:var(--text-muted);">${b.route_name}</small></td>
            <td>${b.operator_name}</td>
            <td><code>${b.journey_id}</code></td>
            <td>${b.latest_gps_update}</td>
            <td>${freshBadge}</td>
            <td><span class="badge-status badge-present">${b.confirmed_present_count}</span></td>
            <td><span class="badge-status badge-possibly-absent">${b.possible_absence_count}</span></td>
            <td>
                <button class="btn-primary" style="padding:4px 10px;font-size:12px;" onclick="selectSpecificBus(${b.id})">
                    <i class="bi bi-crosshair"></i> Monitor Bus
                </button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function renderRecentActivity(activity) {
    const tbody = document.getElementById("recent-activity-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";

    if (!activity || activity.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" class="text-center py-4" style="color:var(--text-muted)">No recent operational activity recorded.</td></tr>`;
        return;
    }

    activity.forEach(a => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
            <td style="white-space:nowrap;font-size:12px;">${a.timestamp}</td>
            <td><strong>${a.title}</strong></td>
            <td><span class="badge-status badge-conf-med">${a.bus_info}</span></td>
            <td style="font-size:13px;color:var(--text-muted);">${a.description}</td>
            <td><span class="badge-status ${a.badge_class}">Recorded</span></td>
        `;
        tbody.appendChild(tr);
    });
}

async function openPassengerModal(passengerId) {
    const modal = document.getElementById("passenger-modal");
    if (!modal) return;

    try {
        const res = await fetch(`/api/passengers/${passengerId}`);
        if (!res.ok) return;
        const p = await res.json();

        document.getElementById("modal-passenger-id").textContent = `${p.person_id} Journey Observations`;
        document.getElementById("modal-status-badge").textContent = p.status.replace("_", " ");
        document.getElementById("modal-first-seen").textContent = p.first_seen_time || "N/A";
        document.getElementById("modal-last-seen").textContent = p.last_seen_time || "N/A";
        document.getElementById("modal-first-gps").textContent = (p.first_latitude && p.first_longitude) ? `${p.first_latitude.toFixed(6)}, ${p.first_longitude.toFixed(6)}` : "No GPS recorded";
        document.getElementById("modal-last-gps").textContent = (p.last_latitude && p.last_longitude) ? `${p.last_latitude.toFixed(6)}, ${p.last_longitude.toFixed(6)}` : "No GPS recorded";

        const obsContainer = document.getElementById("modal-observations-list");
        obsContainer.innerHTML = "";
        const faceObs = (p.observations || []).filter(o => o.has_face || (o.crop_url && o.crop_url.includes("face_")));
        if (faceObs.length > 0) {
            faceObs.forEach(o => {
                const item = document.createElement("div");
                item.style.cssText = "display:inline-block;margin-right:12px;margin-bottom:12px;text-align:center;";
                item.innerHTML = `
                    <img src="${o.crop_url}" class="passenger-thumb" style="width:64px;height:64px;object-fit:cover;border-radius:6px;border:2px solid #3b82f6;" alt="Face Crop" title="Obs #${o.id} (Angle ${o.camera_angle || 'A'})">
                    <div style="font-size:11px;font-weight:600;margin-top:2px;">Photo #${o.upload_id}</div>
                    <div style="font-size:10px;color:var(--text-muted);">${o.similarity_score ? Math.round(o.similarity_score*100) + '%' : 'Confirmed'}</div>
                `;
                obsContainer.appendChild(item);
            });
        } else {
            obsContainer.innerHTML = `<span style="color:var(--text-muted);font-size:12px;">No face crop observations recorded for this passenger.</span>`;
        }

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
        console.error(e);
    }
}

function closePassengerModal() {
    const modal = document.getElementById("passenger-modal");
    if (modal) modal.classList.remove("active");
}

// ============================================================================
// 3D Bus Cabin & Interior Seat Occupancy Logic
// ============================================================================

let isCabin3D = true;
let cabinRotateX = 50;
let cabinRotateZ = -20;
let currentCabinPassengers = [];
let currentCabinCapacity = 40;
let activeInspectingPassenger = null;

function render3DBusCabin(passengers, capacity = 24) {
    currentCabinPassengers = passengers || [];
    currentCabinCapacity = 24; // 6 visible rows x 4 seats = 24 seats matching CCTV bus photo

    const grid = document.getElementById("cabin-seating-grid");
    const rearBench = document.getElementById("cabin-rear-bench");
    const pill = document.getElementById("cabin-occupancy-pill");
    if (!grid) return;

    grid.innerHTML = "";
    if (rearBench) rearBench.innerHTML = ""; // Bench hidden to match exact visible seats

    // Map passengers by seat (e.g. "1A": passenger)
    const seatMap = {};
    const occupiedSeats = new Set();
    currentCabinPassengers.forEach(p => {
        const seat = p.current_seat || "1A";
        seatMap[seat] = p;
        if (p.status === "PRESENT" || p.status === "POSSIBLY_ABSENT" || p.status === "EXIT_UNCONFIRMED") {
            occupiedSeats.add(seat);
        }
    });

    const confirmedCount = currentCabinPassengers.filter(p => p.status === "PRESENT").length;
    const occupancyPct = Math.round((confirmedCount / 24) * 100);
    if (pill) {
        pill.textContent = `${confirmedCount} / 24 Visible Seats Occupied (${occupancyPct}%)`;
    }

    // Rows 1 to 6 matching the exact CCTV bus interior perspective
    for (let row = 1; row <= 6; row++) {
        const rowEl = document.createElement("div");
        rowEl.className = "bus-seat-row";

        // Left side: Window A, Aisle B
        ["A", "B"].forEach(col => {
            const seatCode = `${row}${col}`;
            rowEl.appendChild(createSeatNode(seatCode, seatMap[seatCode]));
        });

        // Center Aisle pathway
        const aisle = document.createElement("div");
        aisle.className = "bus-aisle-path";
        aisle.textContent = `ROW ${row}`;
        rowEl.appendChild(aisle);

        // Right side: Aisle C, Window D
        ["C", "D"].forEach(col => {
            const seatCode = `${row}${col}`;
            rowEl.appendChild(createSeatNode(seatCode, seatMap[seatCode]));
        });

        grid.appendChild(rowEl);
    }
}

function createSeatNode(seatCode, passenger) {
    const node = document.createElement("div");
    node.dataset.seat = seatCode;

    // Check if seat is vacant or passenger has confirmed exit
    if (!passenger || passenger.status === "EXIT_CONFIRMED") {
        node.className = "bus-seat-node bus-seat-vacant";
        node.innerHTML = `
            <div class="seat-base-brown">
                <div class="seat-headrest-brown"><div class="seat-grab-handle"></div></div>
                <div class="seat-vacant-content">
                    <span class="seat-vacant-number">${seatCode}</span>
                    <span class="seat-vacant-label">EMPTY</span>
                </div>
            </div>
        `;
        node.title = `Seat ${seatCode} (Empty) - Click to assign passenger`;
        node.onclick = () => openSeatInspection(seatCode, null);
    } else {
        node.className = "bus-seat-node bus-seat-occupied";

        let statusClass = "status-present";
        let statusText = "Confirmed Present";
        if (passenger.status === "POSSIBLY_ABSENT") {
            statusClass = "status-possibly-absent";
            statusText = "Possibly Absent";
        } else if (passenger.status === "EXIT_UNCONFIRMED") {
            statusClass = "status-exit-unconfirmed";
            statusText = "Exit Unconfirmed";
        } else if (passenger.status === "NEEDS_REVIEW") {
            statusClass = "status-needs-review";
            statusText = "Needs Review";
        }

        // Distinct clothing color index per passenger for realistic cabin variety
        const charCode = passenger.person_id ? passenger.person_id.charCodeAt(passenger.person_id.length - 1) : 0;
        const colorIdx = charCode % 5;

        // Resolved face photo crop path
        const photoSrc = passenger.thumbnail_url || (passenger.thumbnail_path ? `/data/${passenger.thumbnail_path}` : null);

        const faceHtml = photoSrc ? `
            <img src="${photoSrc}" class="seated-person-face-img" alt="${passenger.person_id}" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
            <div class="seated-person-fallback-face" style="display:none;"><i class="bi bi-person-fill"></i></div>
        ` : `
            <div class="seated-person-fallback-face"><i class="bi bi-person-fill"></i></div>
        `;

        node.innerHTML = `
            <!-- High-Back Brown Leatherette Bus Seat -->
            <div class="seat-base-brown">
                <div class="seat-headrest-brown"><div class="seat-grab-handle"></div></div>
            </div>

            <!-- 3D Seated Person Body with Round Face Crop -->
            <div class="seated-person">
                <div class="seated-person-head">
                    <div class="seated-person-face-circle ${statusClass}" title="${passenger.person_id} (${statusText})">
                        ${faceHtml}
                    </div>
                </div>
                <div class="seated-person-torso color-${colorIdx}">
                    <div class="seated-person-arms"></div>
                    <span class="seated-person-pid">${passenger.person_id}</span>
                </div>
            </div>
        `;

        node.title = `${passenger.person_id} • Status: ${statusText} • Seat: ${seatCode} • Click to inspect`;
        node.onclick = () => openSeatInspection(seatCode, passenger);
    }

    return node;
}

function toggleCabin3DView() {
    const stage = document.getElementById("bus-cabin-stage");
    const btn = document.getElementById("btn-toggle-view");
    if (!stage) return;

    if (isCabin3D) {
        stage.style.transform = "rotateX(0deg) rotateZ(0deg)";
        btn.innerHTML = `<i class="bi bi-badge-3d"></i> 3D Perspective`;
        isCabin3D = false;
    } else {
        stage.style.transform = `rotateX(${cabinRotateX}deg) rotateZ(${cabinRotateZ}deg)`;
        btn.innerHTML = `<i class="bi bi-layers"></i> 2D Top View`;
        isCabin3D = true;
    }
}

function rotateCabin(delta) {
    const stage = document.getElementById("bus-cabin-stage");
    if (!stage) return;
    cabinRotateZ += delta;
    stage.style.transform = `rotateX(${cabinRotateX}deg) rotateZ(${cabinRotateZ}deg)`;
    isCabin3D = true;
    const btn = document.getElementById("btn-toggle-view");
    if (btn) btn.innerHTML = `<i class="bi bi-layers"></i> 2D Top View`;
}

function resetCabinView() {
    const stage = document.getElementById("bus-cabin-stage");
    if (!stage) return;
    cabinRotateX = 50;
    cabinRotateZ = -20;
    stage.style.transform = `rotateX(${cabinRotateX}deg) rotateZ(${cabinRotateZ}deg)`;
    isCabin3D = true;
    const btn = document.getElementById("btn-toggle-view");
    if (btn) btn.innerHTML = `<i class="bi bi-layers"></i> 2D Top View`;
}

function openSeatInspection(seatCode, passenger) {
    activeInspectingPassenger = passenger;
    const modal = document.getElementById("seat-detail-modal");
    if (!modal) return;

    const title = document.getElementById("seat-modal-title");
    const statusBadge = document.getElementById("seat-modal-status");
    const cropImg = document.getElementById("seat-modal-crop");
    const vacantIcon = document.getElementById("seat-modal-vacant-icon");
    const pidEl = document.getElementById("seat-modal-pid");
    const infoEl = document.getElementById("seat-modal-info");
    const reassignContainer = document.getElementById("seat-reassign-container");
    const timelineBtn = document.getElementById("seat-view-timeline-btn");

    title.textContent = `Seat ${seatCode} Inspection`;

    if (passenger && passenger.status !== "EXIT_CONFIRMED") {
        statusBadge.textContent = passenger.status;
        statusBadge.className = passenger.status === "PRESENT" ? "badge-status badge-present" : "badge-status badge-possibly-absent";

        if (passenger.thumbnail_url) {
            cropImg.src = passenger.thumbnail_url;
            cropImg.style.display = "block";
            vacantIcon.style.display = "none";
        } else {
            cropImg.style.display = "none";
            vacantIcon.style.display = "flex";
        }

        pidEl.textContent = `${passenger.person_id} • Seat ${seatCode}`;
        infoEl.innerHTML = `
            <div><strong>Status:</strong> ${passenger.status} (${passenger.confidence} Confidence)</div>
            <div><strong>First Seen:</strong> ${passenger.first_seen_time}</div>
            <div><strong>Last Seen:</strong> ${passenger.last_seen_time}</div>
        `;

        // Populate vacant seats in reassign dropdown
        populateVacantSeats(seatCode);
        reassignContainer.style.display = "block";
        timelineBtn.style.display = "inline-flex";
    } else {
        statusBadge.textContent = "VACANT";
        statusBadge.className = "badge-status badge-exit-confirmed";
        cropImg.style.display = "none";
        vacantIcon.style.display = "flex";
        pidEl.textContent = `Seat ${seatCode} is Vacant`;
        infoEl.textContent = "No passenger currently detected or assigned to this seat.";
        reassignContainer.style.display = "none";
        timelineBtn.style.display = "none";
    }

    modal.style.display = "flex";
}

function populateVacantSeats(currentSeat) {
    const sel = document.getElementById("seat-reassign-select");
    if (!sel) return;

    // Collect occupied seats
    const occupied = new Set();
    currentCabinPassengers.forEach(p => {
        if (p.status !== "EXIT_CONFIRMED") occupied.add(p.current_seat);
    });

    sel.innerHTML = "";
    // All visible seats: 1A..6D (24 seats matching CCTV view)
    const allSeats = [];
    for (let r = 1; r <= 6; r++) {
        ["A", "B", "C", "D"].forEach(c => allSeats.push(`${r}${c}`));
    }

    allSeats.forEach(s => {
        if (!occupied.has(s) || s === currentSeat) {
            const opt = document.createElement("option");
            opt.value = s;
            opt.textContent = `Seat ${s} ${s === currentSeat ? '(Current)' : '(Available)'}`;
            if (s === currentSeat) opt.selected = true;
            sel.appendChild(opt);
        }
    });
}

async function reassignPassengerSeat() {
    if (!activeInspectingPassenger) return;
    const newSeat = document.getElementById("seat-reassign-select").value;

    try {
        const res = await fetch(`/api/passengers/${activeInspectingPassenger.id}/seat`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ seat: newSeat })
        });
        const data = await res.json();
        if (res.ok) {
            closeSeatModal();
            showToast(`Passenger moved to Seat ${newSeat}`);
            loadAdminDashboardData();
        } else {
            showToast(data.error || "Failed to update seat.", "error");
        }
    } catch (e) {
        showToast("Error updating seat: " + e.message, "error");
    }
}

function openPassengerTimelineFromSeat() {
    if (activeInspectingPassenger) {
        closeSeatModal();
        openPassengerModal(activeInspectingPassenger.id);
    }
}

function closeSeatModal() {
    const modal = document.getElementById("seat-detail-modal");
    if (modal) modal.style.display = "none";
}

// ============================================================================
// Storage Analysis & Historical Data Cleanup Functions
// ============================================================================

function escapeHtml(str) {
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

async function openDataCleanupModal() {
    const modal = document.getElementById("data-cleanup-modal");
    if (!modal) return;
    modal.classList.add("active");
    modal.style.display = "flex";
    modal.style.opacity = "1";
    modal.style.visibility = "visible";
    document.getElementById("cleanup-alert").style.display = "none";

    try {
        const res = await fetch("/api/settings/data-analysis");
        if (!res.ok) throw new Error("Failed to load storage analysis");
        const data = await res.json();

        // Update storage gauges
        document.getElementById("storage-uploads-mb").textContent = `${data.storage.uploads_mb} MB`;
        document.getElementById("storage-uploads-cnt").textContent = `${data.counts.total_uploads} photos`;
        document.getElementById("storage-crops-mb").textContent = `${data.storage.crops_mb} MB`;
        document.getElementById("storage-crops-cnt").textContent = `${data.counts.total_observations} crops`;
        document.getElementById("storage-db-mb").textContent = `${data.storage.db_mb} MB`;
        document.getElementById("storage-total-mb").textContent = `${data.storage.total_mb} MB`;

        // Render journeys breakdown table
        const tbody = document.getElementById("cleanup-journeys-tbody");
        if (!data.journeys || data.journeys.length === 0) {
            tbody.innerHTML = `<tr><td colspan="8" class="text-center py-3" style="color:var(--text-muted)">No journeys found.</td></tr>`;
            return;
        }

        tbody.innerHTML = data.journeys.map(j => `
            <tr>
                <td><strong>${escapeHtml(j.journey_code)}</strong></td>
                <td>${escapeHtml(j.bus_number)}</td>
                <td><small>${escapeHtml(j.route)}</small></td>
                <td><span class="badge-status ${j.status === 'active' ? 'badge-present' : 'badge-exit-confirmed'}">${j.status}</span></td>
                <td>${j.photo_count}</td>
                <td>${j.passenger_count}</td>
                <td><strong>${j.size_mb} MB</strong></td>
                <td>
                    <button class="btn-secondary" style="padding:3px 8px;font-size:11px;color:#dc2626;" onclick="deleteSpecificJourney(${j.id}, '${escapeHtml(j.journey_code)}')">
                        <i class="bi bi-trash3 text-danger"></i> Delete
                    </button>
                </td>
            </tr>
        `).join("");
    } catch (e) {
        console.error("Storage analysis failed:", e);
    }
}

function closeDataCleanupModal() {
    const modal = document.getElementById("data-cleanup-modal");
    if (modal) {
        modal.classList.remove("active");
        modal.style.display = "none";
        modal.style.opacity = "0";
        modal.style.visibility = "hidden";
    }
}

function onCleanupCriteriaChange() {
    // Dynamic feedback
}

async function executeDataCleanup() {
    const alertBox = document.getElementById("cleanup-alert");
    const select = document.getElementById("cleanup-type-select");
    const selectedOption = select.options[select.selectedIndex];
    const cleanupType = select.value;
    const days = selectedOption.dataset.days ? parseInt(selectedOption.dataset.days) : 30;
    const deleteFiles = document.getElementById("cleanup-delete-files").checked;
    const deleteAudits = document.getElementById("cleanup-delete-audits").checked;

    if (!confirm(`Are you sure you want to permanently delete these historical transit records and free disk space?`)) {
        return;
    }

    const btn = document.getElementById("btn-run-cleanup");
    btn.disabled = true;
    btn.innerHTML = `<i class="bi bi-arrow-repeat spin"></i> Purging Data...`;

    try {
        const res = await fetch("/api/settings/data-cleanup", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                cleanup_type: cleanupType,
                days: days,
                delete_disk_files: deleteFiles,
                delete_audit_logs: deleteAudits
            })
        });
        const data = await res.json();
        if (res.ok) {
            alertBox.style.display = "block";
            alertBox.style.background = "var(--status-present-bg)";
            alertBox.style.color = "var(--status-present-text)";
            alertBox.innerHTML = `<strong>Cleanup Complete:</strong> ${data.message}`;
            showToast(`Deleted ${data.deleted_journeys} journeys, freed ${data.freed_mb} MB.`);
            openDataCleanupModal(); // Refresh modal data
            loadAdminDashboardData(); // Refresh dashboard
        } else {
            alertBox.style.display = "block";
            alertBox.style.background = "var(--status-needs-review-bg)";
            alertBox.style.color = "var(--status-needs-review-text)";
            alertBox.textContent = data.error || "Cleanup failed.";
        }
    } catch (e) {
        alertBox.style.display = "block";
        alertBox.style.background = "var(--status-needs-review-bg)";
        alertBox.style.color = "var(--status-needs-review-text)";
        alertBox.textContent = "Network error: " + e.message;
    } finally {
        btn.disabled = false;
        btn.innerHTML = `<i class="bi bi-trash3"></i> Purge Old Data`;
    }
}

async function deleteSpecificJourney(journeyId, journeyCode) {
    if (!confirm(`Delete historical journey ${journeyCode} and all its photos & passenger records?`)) {
        return;
    }

    try {
        const res = await fetch("/api/settings/data-cleanup", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                cleanup_type: "single_journey",
                journey_id: journeyId,
                delete_disk_files: true
            })
        });
        const data = await res.json();
        if (res.ok) {
            showToast(`Deleted journey ${journeyCode} (Freed ${data.freed_mb} MB)`);
            openDataCleanupModal();
            loadAdminDashboardData();
        } else {
            showToast(data.error || "Failed to delete journey.", "error");
        }
    } catch (e) {
        showToast("Error deleting journey: " + e.message, "error");
    }
}

