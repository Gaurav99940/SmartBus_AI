/**
 * route_timeline.js
 * Controller for Bus Speed (0-15 km/h) Timeline, Route Stops, and
 * Individual Passenger Green/Red Trajectory Visualization.
 */

let allRouteStops = [];
let allPassengerTrajectories = [];
let activePassengerId = null;
let timelineMap = null;
let traveledPolyline = null;
let nonTraveledPolylines = [];
let stopMarkers = [];
let currentSelectedBusId = null;
let currentUserRole = "operator";

document.addEventListener("DOMContentLoaded", () => {
    initRouteMap();
    checkUserAndLoadBuses();
});

// ----------------------------------------------------------------------------
// Map Initialization
// ----------------------------------------------------------------------------

function initRouteMap() {
    const mapEl = document.getElementById("route-map");
    if (!mapEl || timelineMap) return;

    // Default center near Kaushambi / Sahibabad / Vaishali, Ghaziabad
    timelineMap = L.map("route-map").setView([28.6430, 77.3300], 14);

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        maxZoom: 19
    }).addTo(timelineMap);
}

// ----------------------------------------------------------------------------
// Role Inspection & Admin Multi-Bus Fleet Switching
// ----------------------------------------------------------------------------

async function checkUserAndLoadBuses() {
    try {
        const meRes = await fetch("/api/auth/me");
        if (meRes.ok) {
            const me = await meRes.json();
            currentUserRole = me.role;
            if (me.current_bus_id) {
                currentSelectedBusId = me.current_bus_id;
            }
        }
    } catch (e) {
        console.error("Auth check failed:", e);
    }

    const params = new URLSearchParams(window.location.search);
    if (params.get("bus_id")) {
        currentSelectedBusId = parseInt(params.get("bus_id"), 10);
    }

    if (currentUserRole === "admin") {
        try {
            const bRes = await fetch("/api/buses");
            if (bRes.ok) {
                const buses = await bRes.json();
                const switcher = document.getElementById("admin-bus-switcher");
                if (switcher && buses.length > 0) {
                    switcher.innerHTML = buses.map(b => `
                        <option value="${b.id}" ${b.id === currentSelectedBusId ? 'selected' : ''}>
                            ${b.registration_number || b.bus_number} — ${b.bus_name || 'Fleet Vehicle'} (${b.route_name || 'Corridor'})
                        </option>
                    `).join("");

                    if (!currentSelectedBusId || !buses.some(b => b.id === currentSelectedBusId)) {
                        currentSelectedBusId = buses[0].id;
                        switcher.value = currentSelectedBusId;
                    }
                }
            }
            // Load admin consolidated fleet monitoring table & UP routes
            loadFleetSummaryTable();
            loadUPRoutesModalData();
        } catch (e) {
            console.error("Fleet bus load failed:", e);
        }
    }

    await loadRouteData(currentSelectedBusId);
}

function onAdminBusSwitch(busId) {
    if (!busId) return;
    currentSelectedBusId = parseInt(busId, 10);
    const switcher = document.getElementById("admin-bus-switcher");
    if (switcher && switcher.value != currentSelectedBusId) {
        switcher.value = currentSelectedBusId;
    }
    const url = new URL(window.location);
    url.searchParams.set("bus_id", busId);
    window.history.pushState({}, "", url);
    highlightFleetTableRow(currentSelectedBusId);
    loadRouteData(currentSelectedBusId);
}

// ----------------------------------------------------------------------------
// Load Route Stops & Passenger Trajectories
// ----------------------------------------------------------------------------

async function loadRouteData(busId = null) {
    const targetBusId = busId || currentSelectedBusId;
    const queryParam = targetBusId ? `?bus_id=${targetBusId}` : "";

    try {
        const [stopsRes, trajRes] = await Promise.all([
            fetch(`/api/route-stops${queryParam}`),
            fetch(`/api/route-stops/passenger-trajectories${queryParam}`)
        ]);

        const stopsData = await stopsRes.json();
        const trajData = await trajRes.json();

        if (stopsData.success) {
            allRouteStops = stopsData.stops || [];
            currentSelectedBusId = stopsData.bus_id || targetBusId;
            renderRouteSummary(stopsData.route_summary || {}, stopsData);
            renderTopBusRouteLine();
            populateInsertPositionDropdown();

            // Update header indicators
            const opLabel = document.getElementById("operator-bus-label");
            if (opLabel) {
                opLabel.textContent = `${stopsData.bus_registration || 'Bus'} (${stopsData.bus_name || 'Standard Route'})`;
            }
            const adminPill = document.getElementById("admin-bus-info-pill");
            if (adminPill) {
                adminPill.innerHTML = `
                    <span style="color:#10b981;"><i class="bi bi-geo-alt-fill"></i> ${stopsData.bus_registration || ''}</span>
                    &bull; ${stopsData.count || 0} Configured Stops
                `;
            }
        }

        if (trajData.success) {
            allPassengerTrajectories = trajData.passengers || [];
            document.getElementById("stat-passenger-count").textContent = allPassengerTrajectories.length;
            renderPassengerRouteLinesList();

            // Default to first passenger or existing selected
            if (allPassengerTrajectories.length > 0) {
                const targetId = activePassengerId || allPassengerTrajectories[0].id;
                selectPassenger(targetId);
            } else {
                updateVisualTrack(null);
                renderStopsTable(null);
                drawRouteOnMap(null);
            }
        }

        // Invalidate map size to ensure tiles render cleanly
        if (timelineMap) {
            setTimeout(() => { timelineMap.invalidateSize(); }, 350);
        }
    } catch (err) {
        console.error("Failed to load route telemetry:", err);
        showToast("Error loading route stops and telemetry data.", "error");
    }
}

function renderRouteSummary(summary) {
    if (summary.total_stops) {
        document.getElementById("stat-total-stops").textContent = summary.total_stops;
        const sub = document.getElementById("stops-count-sub");
        if (sub) sub.textContent = `${summary.total_stops} Sequential Points`;
    }
    if (summary.average_speed_kmh !== undefined) {
        document.getElementById("stat-avg-speed").innerHTML = `${summary.average_speed_kmh} <span style="font-size:14px;font-weight:500;">km/h</span>`;
    }
    if (summary.max_speed_kmh !== undefined) {
        document.getElementById("stat-max-speed").innerHTML = `${summary.max_speed_kmh} <span style="font-size:14px;font-weight:500;">km/h</span>`;
    }
    if (summary.origin && summary.destination) {
        const titleEl = document.getElementById("route-title");
        if (titleEl) {
            titleEl.innerHTML = `Route: ${summary.origin} &rarr; ${summary.destination}`;
        }
    }
}

// ----------------------------------------------------------------------------
// 1. TOP MASTER BUS ROUTE LINE (Top p bus ki route line)
// ----------------------------------------------------------------------------

function renderTopBusRouteLine() {
    const container = document.getElementById("top-master-route-track");
    if (!container) return;

    if (!allRouteStops || allRouteStops.length === 0) {
        container.innerHTML = `<div style="color:var(--text-muted);font-size:13px;padding:20px;">No route stops defined.</div>`;
        return;
    }

    const totalStops = allRouteStops.length;
    // Each stop gets min-width 130px for clear horizontal spread
    const trackWidth = Math.max(1600, totalStops * 140);
    container.style.minWidth = `${trackWidth}px`;

    let html = `
        <!-- Continuous Route Line Behind Dots -->
        <div style="position:absolute;top:38px;left:70px;right:70px;height:6px;background:linear-gradient(90deg, #10b981 0%, #0ea5e9 40%, #6366f1 80%, #3b82f6 100%);border-radius:4px;z-index:1;box-shadow:0 2px 6px rgba(16,185,129,0.3);"></div>
    `;

    allRouteStops.forEach((stop, idx) => {
        const isFirst = (idx === 0);
        const isLast = (idx === totalStops - 1);
        const speedVal = stop.speed_kmh || 0;

        let speedBadgeBg = "#dbeafe";
        let speedBadgeColor = "#1e40af";
        if (speedVal > 10) {
            speedBadgeBg = "#d1fae5";
            speedBadgeColor = "#065f46";
        } else if (speedVal > 5) {
            speedBadgeBg = "#fef3c7";
            speedBadgeColor = "#92400e";
        }

        let terminalBadge = "";
        if (isFirst) {
            terminalBadge = `<span style="position:absolute;top:-22px;background:#10b981;color:#fff;font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;white-space:nowrap;box-shadow:0 2px 4px rgba(0,0,0,0.15);"><i class="bi bi-play-circle-fill"></i> ORIGIN</span>`;
        } else if (isLast) {
            terminalBadge = `<span style="position:absolute;top:-22px;background:#6366f1;color:#fff;font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;white-space:nowrap;box-shadow:0 2px 4px rgba(0,0,0,0.15);"><i class="bi bi-flag-fill"></i> TERMINAL</span>`;
        } else if (stop.is_major_stop) {
            terminalBadge = `<span style="position:absolute;top:-22px;background:#f59e0b;color:#fff;font-size:9px;font-weight:700;padding:1px 6px;border-radius:8px;white-space:nowrap;">STATION</span>`;
        }

        html += `
            <div style="flex:1;min-width:130px;display:flex;flex-direction:column;align-items:center;position:relative;z-index:2;cursor:pointer;" onclick="focusStopOnMap(${stop.id})" title="Click to view Stop #${stop.stop_order} on Map">
                ${terminalBadge}
                
                <!-- Stop Node Circle -->
                <div style="width:34px;height:34px;border-radius:50%;background:#ffffff;border:3px solid ${isFirst ? '#10b981' : (isLast ? '#6366f1' : '#0ea5e9')};display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;color:var(--text-main);box-shadow:0 2px 6px rgba(0,0,0,0.12);transition:transform 0.2s;" onmouseenter="this.style.transform='scale(1.15)'" onmouseleave="this.style.transform='scale(1)'">
                    ${stop.stop_order}
                </div>

                <!-- Bus Speed Telemetry Badge -->
                <div style="margin-top:8px;background:${speedBadgeBg};color:${speedBadgeColor};font-size:11px;font-weight:700;padding:2px 8px;border-radius:12px;white-space:nowrap;">
                    <i class="bi bi-speedometer2"></i> ${speedVal} km/h
                </div>

                <!-- Stop Name & Details -->
                <div style="font-size:12px;font-weight:700;color:var(--text-main);margin-top:6px;text-align:center;max-width:120px;word-break:break-word;line-height:1.2;">
                    ${stop.stop_name}
                </div>
                <div style="font-size:10px;color:var(--text-muted);margin-top:3px;text-align:center;">
                    ${stop.time_window ? stop.time_window.split('|')[1] || stop.time_window : ''}
                </div>
            </div>
        `;
    });

    container.innerHTML = html;
}

// ----------------------------------------------------------------------------
// 2. PASSENGER PHOTOS & FRONT ROUTE LINES (Kha Tak Travel Kiya Hai)
// ----------------------------------------------------------------------------

function renderPassengerRouteLinesList() {
    const container = document.getElementById("passenger-route-lines-list");
    if (!container) return;

    if (!allPassengerTrajectories || allPassengerTrajectories.length === 0) {
        container.innerHTML = `
            <div style="color:var(--text-muted);font-size:13px;padding:20px;text-align:center;">
                <i class="bi bi-info-circle"></i> No passenger trajectories found for this journey. Upload bus interior photos to auto-track passengers.
            </div>
        `;
        return;
    }

    container.innerHTML = "";
    const totalStops = allRouteStops.length;

    allPassengerTrajectories.forEach(p => {
        const isSelected = (p.id === activePassengerId);
        const traveledSet = new Set(p.traveled_stop_ids);

        const rowCard = document.createElement("div");
        rowCard.className = "card";
        rowCard.id = `p-trajectory-row-${p.id}`;
        rowCard.style.padding = "16px 20px";
        rowCard.style.marginBottom = "0";
        rowCard.style.border = isSelected ? "2px solid #10b981" : "1px solid var(--border-color)";
        rowCard.style.background = isSelected ? "rgba(16, 185, 129, 0.04)" : "var(--bg-card)";
        rowCard.style.boxShadow = isSelected ? "0 4px 12px rgba(16, 185, 129, 0.15)" : "none";
        rowCard.style.transition = "all 0.25s ease";
        rowCard.style.cursor = "pointer";

        rowCard.onclick = () => selectPassenger(p.id);

        const avatarImg = p.thumbnail_path 
            ? `<img src="/data/${p.thumbnail_path}" style="width:68px;height:68px;border-radius:12px;object-fit:cover;border:3px solid ${isSelected ? '#10b981' : '#cbd5e1'};box-shadow:0 3px 8px rgba(0,0,0,0.15);" alt="${p.person_id}">`
            : `<div style="width:68px;height:68px;border-radius:12px;background:linear-gradient(135deg, #10b981 0%, #0ea5e9 100%);color:#fff;display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:800;border:3px solid ${isSelected ? '#10b981' : '#cbd5e1'};">${p.person_id ? p.person_id.substring(0,2) : 'P'}</div>`;

        // Generate the horizontal route line for this passenger
        const trackWidth = Math.max(1400, totalStops * 110);
        let stopsNodesHtml = "";

        allRouteStops.forEach((stop, idx) => {
            const isPresent = traveledSet.has(stop.id);
            const isBoarding = (stop.id === p.boarding_stop.id);
            const isExit = (stop.id === p.exit_stop.id);

            let nodeBadge = "";
            if (isPresent) {
                if (isBoarding) {
                    nodeBadge = `<span style="position:absolute;top:-22px;background:#10b981;color:#fff;font-size:9px;font-weight:700;padding:2px 6px;border-radius:8px;white-space:nowrap;box-shadow:0 2px 4px rgba(0,0,0,0.2);"><i class="bi bi-box-arrow-in-right"></i> Present (Boarded)</span>`;
                } else {
                    nodeBadge = `<span style="position:absolute;top:-22px;background:#10b981;color:#fff;font-size:9px;font-weight:700;padding:2px 6px;border-radius:8px;white-space:nowrap;box-shadow:0 2px 4px rgba(0,0,0,0.2);"><i class="bi bi-check-circle-fill"></i> Present</span>`;
                }
            } else {
                nodeBadge = `<span style="position:absolute;top:-22px;background:#ef4444;color:#fff;font-size:8px;font-weight:700;padding:1px 5px;border-radius:6px;white-space:nowrap;box-shadow:0 1px 3px rgba(239,68,68,0.3);">Absent &times;</span>`;
            }

            const nodeBg = isPresent ? "#10b981" : "#fee2e2";
            const nodeBorder = isPresent ? "3px solid #059669" : "2px solid #ef4444";
            const nodeTextColor = isPresent ? "#ffffff" : "#b91c1c";
            const nodeIcon = isPresent ? stop.stop_order : `&times;`;
            const statusLabel = isPresent ? "PRESENT" : "ABSENT";
            const statusColor = isPresent ? "#10b981" : "#ef4444";

            stopsNodesHtml += `
                <div style="flex:1;min-width:100px;display:flex;flex-direction:column;align-items:center;position:relative;z-index:2;" title="Stop #${stop.stop_order}: ${stop.stop_name} (${isPresent ? 'PRESENT AT THIS STOP' : 'ABSENT (Red Mark)'})">
                    ${nodeBadge}
                    
                    <div style="width:26px;height:26px;border-radius:50%;background:${nodeBg};border:${nodeBorder};color:${nodeTextColor};display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:800;box-shadow:${isPresent ? '0 0 10px rgba(16,185,129,0.6)' : '0 1px 4px rgba(239,68,68,0.2)'};">
                        ${nodeIcon}
                    </div>

                    <div style="font-size:11px;font-weight:${isPresent ? '700' : '500'};color:${isPresent ? 'var(--text-main)' : 'var(--text-muted)'};margin-top:5px;text-align:center;max-width:95px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                        ${stop.stop_order}. ${stop.stop_name}
                    </div>

                    <div style="font-size:9px;font-weight:700;color:${statusColor};margin-top:2px;">
                        ${statusLabel}
                    </div>
                </div>
            `;
        });

        // Calculate boarded and exit stop positions for connecting lines
        const boardIdx = Math.max(0, allRouteStops.findIndex(s => s.id === p.boarding_stop.id));
        const exitIdx = Math.max(boardIdx, allRouteStops.findIndex(s => s.id === p.exit_stop.id));
        
        const stopSlotWidth = trackWidth / totalStops;
        const greenLeft = Math.round(boardIdx * stopSlotWidth + (stopSlotWidth / 2));
        const greenWidth = Math.max(20, Math.round((exitIdx - boardIdx) * stopSlotWidth));

        rowCard.innerHTML = `
            <div style="display:flex;gap:20px;align-items:center;flex-wrap:nowrap;">
                <!-- Left: Passenger Photo & Profile -->
                <div style="flex:0 0 280px;min-width:270px;display:flex;align-items:center;gap:14px;border-right:2px solid var(--border-color);padding-right:16px;">
                    ${avatarImg}
                    <div style="flex:1;">
                        <div style="display:flex;align-items:center;gap:8px;margin-bottom:2px;">
                            <h4 style="margin:0;font-size:16px;font-weight:800;color:var(--text-main);">${p.person_id}</h4>
                            <span style="font-size:11px;font-weight:700;padding:2px 7px;border-radius:4px;background:#e0e7ff;color:#4338ca;">
                                Seat ${p.current_seat || 'N/A'}
                            </span>
                        </div>
                        
                        <div style="margin-bottom:6px;">
                            <span class="badge-status ${p.status === 'PRESENT' ? 'badge-present' : 'badge-exit-confirmed'}" style="font-size:10px;padding:2px 8px;">
                                ${p.status === 'PRESENT' ? '<i class="bi bi-person-check-fill"></i> PRESENT' : '<i class="bi bi-box-arrow-right"></i> EXITED'}
                            </span>
                        </div>

                        <div style="font-size:11px;line-height:1.45;">
                            <div style="color:#10b981;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                                <i class="bi bi-geo-alt-fill"></i> Boarded: Stop #${p.boarding_stop.order} (${p.boarding_stop.name})
                            </div>
                            <div style="color:#0ea5e9;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px;">
                                <i class="bi bi-flag-fill"></i> Exit: Stop #${p.exit_stop.order} (${p.exit_stop.name})
                            </div>
                            <div style="margin-top:6px;display:flex;flex-direction:column;gap:3px;">
                                <span style="background:#d1fae5;color:#065f46;font-size:11px;font-weight:800;padding:3px 10px;border-radius:12px;display:inline-block;border:1px solid #a7f3d0;">
                                    <i class="bi bi-signpost-fill"></i> Traveled: ${p.traveled_stops_count} of ${totalStops} Stops (${p.travel_percentage}% Covered)
                                </span>
                                <div style="font-size:10px;color:var(--text-muted);font-weight:600;margin-top:1px;">
                                    Coverage: Stop #${p.boarding_stop.order} &rarr; Stop #${p.exit_stop.order}
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Right: Front Route Line (Kha Tak Travel Kiya Hai) -->
                <div style="flex:1;overflow-x:auto;padding:8px 0;">
                    <div style="min-width:${trackWidth}px;position:relative;padding-top:22px;display:flex;align-items:flex-start;">
                        <!-- Full Red Line (Base Route - Not Traveled) -->
                        <div style="position:absolute;top:33px;left:40px;right:40px;height:4px;background:#fca5a5;border-radius:2px;z-index:1;"></div>

                        <!-- Highlighted Green Segment (Traveled Route) -->
                        <div style="position:absolute;top:32px;left:${greenLeft}px;width:${greenWidth}px;height:6px;background:#10b981;border-radius:3px;z-index:1;box-shadow:0 0 10px rgba(16,185,129,0.8);"></div>

                        <!-- All Stop Nodes -->
                        ${stopsNodesHtml}
                    </div>
                </div>
            </div>
        `;

        container.appendChild(rowCard);
    });
}

function selectPassenger(passengerId) {
    activePassengerId = passengerId;
    const p = allPassengerTrajectories.find(item => item.id === passengerId);
    if (!p) return;

    // Update active style on all passenger trajectory rows
    allPassengerTrajectories.forEach(item => {
        const row = document.getElementById(`p-trajectory-row-${item.id}`);
        if (row) {
            const isSel = (item.id === passengerId);
            row.style.border = isSel ? "2px solid #10b981" : "1px solid var(--border-color)";
            row.style.background = isSel ? "rgba(16, 185, 129, 0.04)" : "var(--bg-card)";
            row.style.boxShadow = isSel ? "0 4px 12px rgba(16, 185, 129, 0.15)" : "none";
        }
    });

    // Update Active Passenger Banner
    const titleEl = document.getElementById("active-p-title");
    const boardEl = document.getElementById("active-p-boarding");
    const exitEl = document.getElementById("active-p-exit");
    const pctEl = document.getElementById("active-p-traveled-pct");
    const barEl = document.getElementById("active-p-progress-bar");
    const avatarWrap = document.getElementById("active-p-avatar-wrap");

    if (titleEl) titleEl.textContent = `Passenger ${p.person_id} • Assigned Seat ${p.current_seat || '1A'}`;
    if (boardEl) boardEl.textContent = `Stop #${p.boarding_stop.order} • ${p.boarding_stop.name} (${p.boarding_stop.time || ''})`;
    if (exitEl) exitEl.textContent = `Stop #${p.exit_stop.order} • ${p.exit_stop.name} (${p.exit_stop.time || ''})`;
    if (pctEl) pctEl.textContent = `${p.travel_percentage}% (${p.traveled_stops_count}/${allRouteStops.length} Stops)`;
    if (barEl) barEl.style.width = `${p.travel_percentage}%`;

    if (avatarWrap) {
        avatarWrap.innerHTML = p.thumbnail_path 
            ? `<img src="/data/${p.thumbnail_path}" class="passenger-thumb" style="width:50px;height:50px;border-radius:50%;border:2px solid #10b981;" alt="${p.person_id}">`
            : `<div class="passenger-thumb" style="width:50px;height:50px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:#10b981;color:#fff;font-size:18px;font-weight:700;">${p.person_id ? p.person_id.substring(0,2) : 'P'}</div>`;
    }

    // Redraw map with Green vs Red route lines for this passenger
    drawRouteOnMap(p);

    // Update visual stop progress track and table
    updateVisualTrack(p);
    renderStopsTable(p);
}

function focusStopOnMap(stopId) {
    const s = allRouteStops.find(item => item.id === stopId);
    if (!s || !timelineMap) return;
    timelineMap.setView([s.latitude, s.longitude], 16, { animate: true });
}

// ----------------------------------------------------------------------------
// Leaflet Map: Green (Traveled) vs Red (Not Traveled) Polyline
// ----------------------------------------------------------------------------

function drawRouteOnMap(passenger) {
    if (!timelineMap || allRouteStops.length === 0) return;

    // Clear existing polylines & markers
    if (traveledPolyline) {
        timelineMap.removeLayer(traveledPolyline);
        traveledPolyline = null;
    }
    nonTraveledPolylines.forEach(layer => timelineMap.removeLayer(layer));
    nonTraveledPolylines = [];

    stopMarkers.forEach(m => timelineMap.removeLayer(m));
    stopMarkers = [];

    const traveledStopIds = passenger ? new Set(passenger.traveled_stop_ids) : new Set(allRouteStops.map(s => s.id));
    const allLatLngs = allRouteStops.map(s => [s.latitude, s.longitude]);

    // Split stops into contiguous segments of Traveled (Green) and Non-traveled (Red)
    let currentSegment = [];
    let currentIsTraveled = null;

    allRouteStops.forEach((stop, idx) => {
        const isTraveled = traveledStopIds.has(stop.id);
        const latlng = [stop.latitude, stop.longitude];

        if (currentIsTraveled === null) {
            currentIsTraveled = isTraveled;
            currentSegment.push(latlng);
        } else if (currentIsTraveled === isTraveled) {
            currentSegment.push(latlng);
        } else {
            // Include boundary point so segments connect seamlessly
            currentSegment.push(latlng);
            renderPolylineSegment(currentSegment, currentIsTraveled);
            currentSegment = [latlng];
            currentIsTraveled = isTraveled;
        }
    });

    if (currentSegment.length > 1) {
        renderPolylineSegment(currentSegment, currentIsTraveled);
    }

    // Add Stop Markers with custom icons & popups
    allRouteStops.forEach(stop => {
        const isTraveled = traveledStopIds.has(stop.id);
        const isBoarding = passenger && (stop.id === passenger.boarding_stop.id);
        const isExit = passenger && (stop.id === passenger.exit_stop.id);

        let markerColor = isTraveled ? "#10b981" : "#ef4444";
        let radius = stop.is_major_stop ? 8 : 6;

        if (isBoarding || isExit) {
            radius = 10;
        }

        const marker = L.circleMarker([stop.latitude, stop.longitude], {
            radius: radius,
            color: markerColor,
            fillColor: markerColor,
            fillOpacity: 0.9,
            weight: (isBoarding || isExit) ? 3 : 1
        }).addTo(timelineMap);

        let badgeStatusHtml = isTraveled 
            ? `<span style="display:inline-block;padding:2px 8px;border-radius:4px;background:#10b981;color:#fff;font-size:11px;font-weight:700;"><i class="bi bi-check-circle-fill"></i> PRESENT AT THIS STOP</span>`
            : `<span style="display:inline-block;padding:2px 8px;border-radius:4px;background:#fee2e2;color:#b91c1c;font-size:11px;font-weight:700;"><i class="bi bi-x-circle-fill"></i> ABSENT (Not at this stop)</span>`;

        if (isBoarding) {
            badgeStatusHtml = `<span style="display:inline-block;padding:2px 8px;border-radius:4px;background:#10b981;color:#fff;font-size:11px;font-weight:700;">&#128205; PRESENT (Boarded Here)</span>`;
        } else if (isExit) {
            badgeStatusHtml = `<span style="display:inline-block;padding:2px 8px;border-radius:4px;background:#0ea5e9;color:#fff;font-size:11px;font-weight:700;">&#127937; PRESENT (Exit / Current)</span>`;
        }

        marker.bindPopup(`
            <div style="font-family:'Segoe UI',sans-serif;font-size:12px;min-width:200px;">
                <div style="font-weight:700;font-size:14px;color:#0f172a;margin-bottom:4px;">
                    Stop #${stop.stop_order}: ${stop.stop_name}
                </div>
                <div style="margin-bottom:6px;">${badgeStatusHtml}</div>
                <div style="color:#64748b;margin-bottom:4px;"><strong>Speed:</strong> ${stop.speed_kmh} km/h</div>
                <div style="color:#64748b;margin-bottom:6px;"><strong>Time:</strong> ${stop.time_window || 'N/A'}</div>
                <div style="font-size:11px;color:#334155;">${stop.location_address}</div>
            </div>
        `);

        stopMarkers.push(marker);
    });

    if (allLatLngs.length > 0) {
        timelineMap.fitBounds(allLatLngs, { padding: [30, 30] });
    }
}

function renderPolylineSegment(latlngs, isTraveled) {
    if (isTraveled) {
        const poly = L.polyline(latlngs, {
            color: "#10b981",
            weight: 6,
            opacity: 0.9,
            lineJoin: "round"
        }).addTo(timelineMap);
        traveledPolyline = poly;
    } else {
        const poly = L.polyline(latlngs, {
            color: "#ef4444",
            weight: 4,
            opacity: 0.8,
            dashArray: "6, 8",
            lineJoin: "round"
        }).addTo(timelineMap);
        nonTraveledPolylines.push(poly);
    }
}

// ----------------------------------------------------------------------------
// Visual Sequential Stop Progress Track
// ----------------------------------------------------------------------------

function updateVisualTrack(passenger) {
    const container = document.getElementById("stops-visual-track");
    if (!container) return;

    if (allRouteStops.length === 0) {
        container.innerHTML = `<div style="color:var(--text-muted);padding:20px;">No stops recorded.</div>`;
        return;
    }

    const traveledSet = passenger ? new Set(passenger.traveled_stop_ids) : new Set(allRouteStops.map(s => s.id));

    let html = `<div style="position:relative;padding-left:26px;">`;

    allRouteStops.forEach((stop, idx) => {
        const isTraveled = traveledSet.has(stop.id);
        const isBoarding = passenger && (stop.id === passenger.boarding_stop.id);
        const isExit = passenger && (stop.id === passenger.exit_stop.id);

        const nodeColor = isTraveled ? "#10b981" : "#ef4444";
        const bgColor = isTraveled ? "rgba(16, 185, 129, 0.08)" : "transparent";

        let specialTag = "";
        if (isBoarding) {
            specialTag = `<span class="badge-status badge-present" style="background:#10b981;color:#fff;font-size:10px;margin-left:6px;"><i class="bi bi-box-arrow-in-right"></i> PRESENT (Boarded)</span>`;
        } else if (isExit) {
            specialTag = `<span class="badge-status" style="background:#0ea5e9;color:#fff;font-size:10px;margin-left:6px;"><i class="bi bi-flag-fill"></i> PRESENT (Exit)</span>`;
        } else if (isTraveled) {
            specialTag = `<span class="badge-status badge-present" style="background:#10b981;color:#fff;font-size:10px;margin-left:6px;"><i class="bi bi-check-circle-fill"></i> PRESENT</span>`;
        } else {
            specialTag = `<span class="badge-status" style="background:#fee2e2;color:#b91c1c;font-size:10px;margin-left:6px;"><i class="bi bi-x-circle-fill"></i> ABSENT</span>`;
        }

        html += `
            <div style="position:relative;margin-bottom:14px;padding:8px 10px;border-radius:var(--radius-sm);background:${bgColor};">
                <!-- Vertical Line Segment -->
                ${idx < allRouteStops.length - 1 ? `<div style="position:absolute;left:-17px;top:18px;bottom:-18px;width:3px;background:${isTraveled ? '#10b981' : '#fca5a5'};"></div>` : ''}
                <!-- Node Circle -->
                <div style="position:absolute;left:-23px;top:10px;width:14px;height:14px;border-radius:50%;background:#fff;border:3px solid ${nodeColor};"></div>

                <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
                    <div>
                        <strong style="font-size:13px;color:var(--text-main);">#${stop.stop_order} ${stop.stop_name}</strong>
                        ${specialTag}
                    </div>
                    <span class="badge-status" style="background:#f1f5f9;color:#334155;font-size:11px;">
                        <i class="bi bi-speedometer"></i> ${stop.speed_kmh} km/h
                    </span>
                </div>
                <div style="font-size:11px;color:var(--text-muted);margin-top:2px;">
                    ${stop.time_window || ''}
                </div>
            </div>
        `;
    });

    html += `</div>`;
    container.innerHTML = html;
}

// ----------------------------------------------------------------------------
// Speed Timeline & Locations List Table
// ----------------------------------------------------------------------------

function renderStopsTable(passenger) {
    const tbody = document.getElementById("stops-table-tbody");
    if (!tbody) return;

    if (allRouteStops.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" class="text-center py-4" style="color:var(--text-muted);">No stops registered.</td></tr>`;
        return;
    }

    const traveledSet = passenger ? new Set(passenger.traveled_stop_ids) : new Set(allRouteStops.map(s => s.id));
    tbody.innerHTML = "";

    allRouteStops.forEach(stop => {
        const isTraveled = traveledSet.has(stop.id);
        const isBoarding = passenger && (stop.id === passenger.boarding_stop.id);
        const isExit = passenger && (stop.id === passenger.exit_stop.id);

        const tr = document.createElement("tr");
        if (isBoarding) {
            tr.style.background = "rgba(16, 185, 129, 0.12)";
        } else if (isExit) {
            tr.style.background = "rgba(14, 165, 233, 0.12)";
        } else if (isTraveled) {
            tr.style.background = "rgba(16, 185, 129, 0.04)";
        }

        // Speed progress bar (0 - 15 km/h)
        const speedVal = stop.speed_kmh || 0;
        const speedPct = Math.min(100, Math.round((speedVal / 15.0) * 100));

        let trajectoryBadge = `<span class="badge-status badge-present" style="background:#10b981;color:#fff;font-weight:700;"><i class="bi bi-check-circle-fill"></i> PRESENT</span>`;
        if (isBoarding) {
            trajectoryBadge = `<span class="badge-status" style="background:#10b981;color:#fff;font-weight:700;"><i class="bi bi-box-arrow-in-right"></i> PRESENT (Boarded)</span>`;
        } else if (isExit) {
            trajectoryBadge = `<span class="badge-status" style="background:#0ea5e9;color:#fff;font-weight:700;"><i class="bi bi-flag-fill"></i> PRESENT (Alighted)</span>`;
        } else if (!isTraveled) {
            trajectoryBadge = `<span class="badge-status" style="background:#fee2e2;color:#b91c1c;font-weight:700;"><i class="bi bi-x-circle-fill"></i> ABSENT &times;</span>`;
        }

        tr.innerHTML = `
            <td><strong>#${stop.stop_order}</strong></td>
            <td style="font-size:12px;color:var(--text-main);white-space:nowrap;">
                <i class="bi bi-clock"></i> ${stop.time_window || 'N/A'}
            </td>
            <td>
                <div style="display:flex;align-items:center;gap:8px;">
                    <strong style="width:45px;font-size:13px;color:var(--text-main);">${speedVal} km/h</strong>
                    <div style="flex:1;height:7px;background:#e2e8f0;border-radius:4px;overflow:hidden;min-width:60px;">
                        <div style="width:${speedPct}%;height:100%;background:${speedVal > 10 ? '#10b981' : (speedVal > 5 ? '#0ea5e9' : '#f59e0b')};border-radius:4px;"></div>
                    </div>
                </div>
            </td>
            <td>
                <strong style="font-size:13px;color:var(--text-main);">${stop.stop_name}</strong>
                ${stop.is_major_stop ? `<span class="badge-status" style="background:#fef3c7;color:#b45309;font-size:10px;margin-left:6px;">Station</span>` : ''}
                <div style="font-size:11px;color:var(--text-muted);margin-top:2px;max-width:440px;">
                    ${stop.location_address}
                </div>
            </td>
            <td>${trajectoryBadge}</td>
            <td>
                <button type="button" class="btn-secondary" style="padding:4px 8px;font-size:11px;color:#ef4444;" onclick="deleteStop(${stop.id}, '${stop.stop_name}')" title="Delete Stop">
                    <i class="bi bi-trash"></i>
                </button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function filterStopsTable() {
    const input = document.getElementById("filter-stop-input");
    const q = (input ? input.value : "").toLowerCase();
    const rows = document.querySelectorAll("#stops-table-tbody tr");
    rows.forEach(r => {
        const text = r.textContent.toLowerCase();
        r.style.display = text.includes(q) ? "" : "none";
    });
}

// ----------------------------------------------------------------------------
// Manually Add Route Stop Modal Handlers
// ----------------------------------------------------------------------------

function openAddStopModal() {
    const modal = document.getElementById("add-stop-modal");
    if (!modal) return;
    populateInsertPositionDropdown();
    modal.classList.add("active");
    modal.style.display = "flex";
}

function closeAddStopModal() {
    const modal = document.getElementById("add-stop-modal");
    if (modal) {
        modal.classList.remove("active");
        modal.style.display = "none";
    }
}

function populateInsertPositionDropdown() {
    const sel = document.getElementById("new-stop-insert-pos");
    if (!sel) return;
    sel.innerHTML = `<option value="">Append to End of Route</option>`;
    allRouteStops.forEach(s => {
        sel.innerHTML += `<option value="${s.stop_order}">After Stop #${s.stop_order}: ${s.stop_name}</option>`;
    });
}

async function handleAddStopSubmit(e) {
    e.preventDefault();
    const saveBtn = document.getElementById("btn-save-new-stop");
    if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.innerHTML = `<i class="bi bi-cpu"></i> Adding Stop...`;
    }

    const payload = {
        stop_name: document.getElementById("new-stop-name").value.trim(),
        location_address: document.getElementById("new-stop-address").value.trim(),
        time_window: document.getElementById("new-stop-time").value.trim(),
        speed_kmh: parseFloat(document.getElementById("new-stop-speed").value || "0"),
        latitude: parseFloat(document.getElementById("new-stop-lat").value || "28.6472"),
        longitude: parseFloat(document.getElementById("new-stop-lon").value || "77.3200"),
        is_major_stop: document.getElementById("new-stop-major").checked,
        insert_after_order: document.getElementById("new-stop-insert-pos").value ? parseInt(document.getElementById("new-stop-insert-pos").value) : null
    };

    try {
        const res = await fetch("/api/route-stops", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.innerHTML = `<i class="bi bi-check-circle-fill"></i> Add Stop to Route`;
        }

        if (res.ok && data.success) {
            showToast(data.message || "Route stop added successfully!", "success");
            closeAddStopModal();
            document.getElementById("add-stop-form").reset();
            await loadRouteData();
        } else {
            showToast(data.error || "Failed to add route stop.", "error");
        }
    } catch (err) {
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.innerHTML = `<i class="bi bi-check-circle-fill"></i> Add Stop to Route`;
        }
        showToast("Network error adding route stop.", "error");
    }
}

async function deleteStop(stopId, stopName) {
    if (!confirm(`Are you sure you want to remove stop "${stopName}" from the route?`)) {
        return;
    }
    try {
        const res = await fetch(`/api/route-stops/${stopId}`, { method: "DELETE" });
        const data = await res.json();
        if (res.ok && data.success) {
            showToast(data.message || `Deleted stop "${stopName}".`, "success");
            await loadRouteData();
        } else {
            showToast(data.error || "Failed to delete stop.", "error");
        }
    } catch (err) {
        showToast("Error deleting stop.", "error");
    }
}

// ----------------------------------------------------------------------------
// Admin Consolidated Fleet Route Monitoring & UP Routes Directory
// ----------------------------------------------------------------------------

let allFleetSummaryData = [];
let allModalUPRoutes = [];

async function loadFleetSummaryTable() {
    const tbody = document.getElementById("admin-fleet-summary-tbody");
    if (!tbody) return;
    tbody.innerHTML = `<tr><td colspan="7" class="text-center py-3" style="color:var(--text-muted);"><i class="bi bi-arrow-repeat spin"></i> Loading fleet route summary...</td></tr>`;

    try {
        const res = await fetch("/api/route-stops/admin/fleet-summary");
        if (!res.ok) throw new Error("Failed to load fleet summary");
        const data = await res.json();
        allFleetSummaryData = data.fleet || [];
        renderFleetSummaryTable(allFleetSummaryData);
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="7" class="text-center py-3 text-danger">Error loading fleet summary: ${e.message}</td></tr>`;
    }
}

function renderFleetSummaryTable(fleet) {
    const tbody = document.getElementById("admin-fleet-summary-tbody");
    if (!tbody) return;

    if (!fleet || fleet.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="text-center py-3" style="color:var(--text-muted);">No buses registered yet.</td></tr>`;
        return;
    }

    tbody.innerHTML = fleet.map(b => {
        const isCurrent = (currentSelectedBusId === b.id);
        const freshnessBadge = b.freshness === "Live"
            ? '<span class="badge-status badge-present" style="font-size:10px;"><i class="bi bi-broadcast"></i> Live GPS</span>'
            : (b.freshness === "Stale"
                ? '<span class="badge-status badge-possibly-absent" style="font-size:10px;"><i class="bi bi-clock-history"></i> Stale</span>'
                : '<span class="badge-status badge-exit-confirmed" style="font-size:10px;"><i class="bi bi-circle"></i> Offline</span>');

        return `
            <tr id="fleet-row-${b.id}" style="${isCurrent ? 'background:rgba(37,99,235,0.08);border-left:3px solid var(--accent-primary);' : ''}">
                <td>
                    <div style="font-weight:700;color:var(--text-main);">${escapeHtml(b.registration_number || b.bus_number)}</div>
                    <div style="font-size:10px;color:var(--text-muted);">${escapeHtml(b.bus_name || 'Fleet Vehicle')} &bull; ${escapeHtml(b.bus_number)}</div>
                </td>
                <td>
                    <div style="font-weight:600;color:var(--text-main);">${escapeHtml(b.operator_name || 'Unassigned')}</div>
                    <div style="font-size:11px;color:var(--text-muted);">${escapeHtml(b.operator_contact || 'No Contact')}</div>
                </td>
                <td>
                    <div style="font-weight:600;color:var(--accent-primary);">${b.route_number ? `<span class="badge-status" style="background:#dbeafe;color:#1e40af;font-size:10px;margin-right:4px;">${escapeHtml(b.route_number)}</span>` : ''}${escapeHtml(b.route_name || 'Unassigned Corridor')}</div>
                </td>
                <td>
                    <div style="font-size:11px;color:var(--text-main);">
                        ${escapeHtml(b.starting_point || 'Start')} 
                        <i class="bi bi-arrow-right text-muted"></i> 
                        ${escapeHtml(b.destination || 'End')}
                    </div>
                </td>
                <td>
                    <span class="badge-status" style="background:#e0f2fe;color:#0369a1;font-weight:700;">
                        ${b.stops_count} Stops
                    </span>
                </td>
                <td>
                    ${freshnessBadge}
                    <div style="font-size:10px;color:var(--text-muted);margin-top:2px;">${b.bus_status || 'Active'}</div>
                </td>
                <td style="text-align:right;">
                    ${isCurrent ? `
                        <button class="btn-primary" style="font-size:11px;padding:3px 8px;" disabled>
                            <i class="bi bi-check2-circle"></i> Viewing
                        </button>
                    ` : `
                        <button class="btn-secondary" style="font-size:11px;padding:3px 8px;font-weight:600;" onclick="onAdminBusSwitch(${b.id})">
                            <i class="bi bi-eye-fill"></i> Switch View
                        </button>
                    `}
                </td>
            </tr>
        `;
    }).join("");
}

function highlightFleetTableRow(busId) {
    if (!allFleetSummaryData || allFleetSummaryData.length === 0) return;
    renderFleetSummaryTable(allFleetSummaryData);
}

function scrollToFleetMonitoring() {
    const el = document.getElementById("admin-fleet-monitoring-card");
    if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
}

// ----------------------------------------------------------------------------
// Uttar Pradesh Master Routes Directory Modal (Admin)
// ----------------------------------------------------------------------------

function openUPRoutesDirectoryModal() {
    const modal = document.getElementById("up-routes-modal");
    if (!modal) return;
    modal.classList.add("active");
    modal.style.display = "flex";
    modal.style.opacity = "1";
    modal.style.visibility = "visible";
    modal.style.pointerEvents = "auto";
    loadUPRoutesModalData();
}

function closeUPRoutesDirectoryModal() {
    const modal = document.getElementById("up-routes-modal");
    if (!modal) return;
    modal.classList.remove("active");
    modal.style.display = "none";
    modal.style.opacity = "0";
    modal.style.visibility = "hidden";
    modal.style.pointerEvents = "none";
}

async function loadUPRoutesModalData() {
    const listEl = document.getElementById("up-routes-modal-list");
    if (!listEl) return;
    listEl.innerHTML = `<div class="text-center py-4" style="color:var(--text-muted);"><i class="bi bi-arrow-repeat spin"></i> Loading UP master transit corridors...</div>`;

    try {
        const res = await fetch("/api/route-stops/up-routes");
        if (!res.ok) throw new Error("Failed to load UP master routes");
        const data = await res.json();
        allModalUPRoutes = data.routes || [];
        renderUPRoutesModalList(allModalUPRoutes);
    } catch (e) {
        listEl.innerHTML = `<div class="text-center py-4 text-danger">Error loading UP master routes: ${e.message}</div>`;
    }
}

function renderUPRoutesModalList(routes) {
    const listEl = document.getElementById("up-routes-modal-list");
    if (!listEl) return;

    if (!routes || routes.length === 0) {
        listEl.innerHTML = `<div class="text-center py-4" style="color:var(--text-muted);">No UP transit routes found matching your criteria.</div>`;
        return;
    }

    listEl.innerHTML = routes.map(r => `
        <div style="background:#ffffff;border:1px solid var(--border-color);border-radius:6px;padding:12px 14px;box-shadow:var(--shadow-sm);">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px;margin-bottom:6px;">
                <div>
                    <span class="badge-status" style="background:#dbeafe;color:#1e40af;font-weight:700;font-size:11px;margin-right:6px;">
                        ${escapeHtml(r.route_code)}
                    </span>
                    <strong style="font-size:13px;color:var(--text-main);">${escapeHtml(r.route_name)}</strong>
                </div>
                <div style="font-size:11px;color:var(--text-muted);">
                    <span style="background:#f1f5f9;padding:2px 8px;border-radius:4px;border:1px solid #e2e8f0;font-weight:600;">
                        ${escapeHtml(r.region || 'Uttar Pradesh')}
                    </span>
                </div>
            </div>

            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;font-size:11px;color:var(--text-muted);">
                <div>
                    <i class="bi bi-geo-alt-fill text-danger"></i> <strong>${escapeHtml(r.origin)}</strong> 
                    &rarr; <i class="bi bi-flag-fill text-success"></i> <strong>${escapeHtml(r.destination)}</strong>
                </div>
                <div style="display:flex;gap:12px;align-items:center;">
                    <span><i class="bi bi-rulers"></i> ${r.total_distance_km} km</span>
                    <span><i class="bi bi-clock"></i> ~${r.estimated_duration_hours} hrs</span>
                    <span><i class="bi bi-speedometer2"></i> Avg ${r.avg_speed_kmh} km/h</span>
                    <span class="badge-status" style="background:#dcfce7;color:#15803d;font-weight:700;">
                        ${r.stops_count} Configured Stops
                    </span>
                </div>
            </div>
        </div>
    `).join("");
}

function filterUPRoutesModal() {
    const q = (document.getElementById("up-routes-search-input").value || "").toLowerCase().trim();
    if (!q) {
        renderUPRoutesModalList(allModalUPRoutes);
        return;
    }
    const filtered = allModalUPRoutes.filter(r =>
        (r.route_code && r.route_code.toLowerCase().includes(q)) ||
        (r.route_name && r.route_name.toLowerCase().includes(q)) ||
        (r.origin && r.origin.toLowerCase().includes(q)) ||
        (r.destination && r.destination.toLowerCase().includes(q)) ||
        (r.region && r.region.toLowerCase().includes(q))
    );
    renderUPRoutesModalList(filtered);
}

function toggleAddUPRouteForm() {
    const f = document.getElementById("new-up-route-form-container");
    if (!f) return;
    f.style.display = (f.style.display === "none" || !f.style.display) ? "block" : "none";
}

async function handleCreateUPRouteSubmit(e) {
    e.preventDefault();
    const btn = document.getElementById("btn-save-up-route");
    btn.disabled = true;
    btn.innerHTML = `<i class="bi bi-hourglass-split spin"></i> Saving...`;

    const payload = {
        route_code: document.getElementById("new-up-code").value.trim(),
        route_name: document.getElementById("new-up-name").value.trim(),
        origin: document.getElementById("new-up-origin").value.trim(),
        destination: document.getElementById("new-up-dest").value.trim(),
        region: document.getElementById("new-up-region").value.trim() || "Uttar Pradesh",
        total_distance_km: parseFloat(document.getElementById("new-up-dist").value) || 100.0,
        estimated_duration_hours: parseFloat(document.getElementById("new-up-dur").value) || 3.0,
        avg_speed_kmh: parseFloat(document.getElementById("new-up-spd").value) || 11.0,
        stops: []
    };

    try {
        const res = await fetch("/api/route-stops/up-routes", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        btn.disabled = false;
        btn.innerHTML = `Save UP Route`;

        if (res.ok && data.success) {
            showToast(data.message || "UP master route created successfully!", "success");
            toggleAddUPRouteForm();
            document.getElementById("new-up-route-form").reset();
            await loadUPRoutesModalData();
        } else {
            showToast(data.error || "Failed to create UP route.", "error");
        }
    } catch (err) {
        btn.disabled = false;
        btn.innerHTML = `Save UP Route`;
        showToast("Error creating route: " + err.message, "error");
    }
}


// ----------------------------------------------------------------------------
// Reports Download (CSV and PDF with Face Icons)
// ----------------------------------------------------------------------------

function downloadTimelineReport(format) {
    let url = "";
    if (allPassengerTrajectories && allPassengerTrajectories.length > 0) {
        const jId = allPassengerTrajectories[0].journey_id;
        if (jId) {
            url = `/api/reports/journey/${jId}/${format}`;
        }
    }
    if (!url) {
        url = `/api/reports/export.${format}`;
        if (currentSelectedBusId) {
            url += `?bus_id=${currentSelectedBusId}`;
        }
    }
    window.open(url, "_blank");
}

// ----------------------------------------------------------------------------
// Manual Stops Builder & Bulk Drop Box
// ----------------------------------------------------------------------------

function toggleQuickStopsBuilder() {
    const card = document.getElementById("quick-stops-builder-card");
    if (!card) return;
    if (card.style.display === "none" || !card.style.display) {
        card.style.display = "block";
        card.scrollIntoView({ behavior: "smooth", block: "start" });
        const tbody = document.getElementById("builder-stops-tbody");
        if (tbody && tbody.children.length === 0 && allRouteStops && allRouteStops.length > 0) {
            loadCurrentStopsIntoBuilder();
        } else if (tbody && tbody.children.length === 0) {
            addBuilderMultipleRows(3);
        }
    } else {
        card.style.display = "none";
    }
}

function switchBuilderTab(tab) {
    const listTabBtn = document.getElementById("tab-btn-list-form");
    const dropTabBtn = document.getElementById("tab-btn-drop-box");
    const listContent = document.getElementById("builder-tab-list-content");
    const dropContent = document.getElementById("builder-tab-drop-content");

    if (tab === "list") {
        if (listTabBtn) listTabBtn.className = "btn-primary";
        if (dropTabBtn) dropTabBtn.className = "btn-secondary";
        if (listContent) listContent.style.display = "block";
        if (dropContent) dropContent.style.display = "none";
    } else {
        if (listTabBtn) listTabBtn.className = "btn-secondary";
        if (dropTabBtn) dropTabBtn.className = "btn-primary";
        if (listContent) listContent.style.display = "none";
        if (dropContent) dropContent.style.display = "block";
    }
}

function loadCurrentStopsIntoBuilder() {
    clearBuilderRows();
    const nameInput = document.getElementById("builder-route-name");
    const origInput = document.getElementById("builder-origin");
    const destInput = document.getElementById("builder-destination");

    if (allRouteStops.length > 0) {
        if (nameInput) nameInput.value = document.getElementById("route-title")?.innerText.replace("Route: ", "") || "";
        if (origInput) origInput.value = allRouteStops[0].stop_name || "";
        if (destInput) destInput.value = allRouteStops[allRouteStops.length - 1].stop_name || "";

        allRouteStops.forEach(s => {
            addBuilderRow({
                name: s.stop_name,
                address: s.location_address,
                time: s.time_window || "",
                speed: s.speed_kmh || 8.0,
                major: s.is_major_stop || false
            });
        });
    }
}

function addBuilderRow(data = {}) {
    const tbody = document.getElementById("builder-stops-tbody");
    if (!tbody) return;

    const rowIdx = tbody.children.length + 1;
    const tr = document.createElement("tr");
    tr.className = "builder-stop-row";
    tr.innerHTML = `
        <td style="font-weight:700;color:var(--text-muted);text-align:center;" class="builder-row-num">${rowIdx}</td>
        <td>
            <input type="text" class="form-control form-control-sm b-stop-name" placeholder="e.g. Stop Name" value="${escapeHtml(data.name || '')}" style="font-size:11px;" onchange="updateBuilderOriginDest()">
        </td>
        <td>
            <input type="text" class="form-control form-control-sm b-stop-addr" placeholder="Location landmark / address" value="${escapeHtml(data.address || '')}" style="font-size:11px;">
        </td>
        <td>
            <input type="text" class="form-control form-control-sm b-stop-time" placeholder="06:00 PM" value="${escapeHtml(data.time || '')}" style="font-size:11px;">
        </td>
        <td>
            <input type="number" class="form-control form-control-sm b-stop-speed" min="0" max="120" step="0.5" value="${data.speed !== undefined ? data.speed : 8.0}" style="font-size:11px;">
        </td>
        <td style="text-align:center;">
            <input type="checkbox" class="b-stop-major" ${data.major ? 'checked' : ''} style="transform:scale(1.1);cursor:pointer;">
        </td>
        <td style="text-align:center;">
            <button type="button" class="btn-sm" style="background:transparent;border:none;color:#ef4444;cursor:pointer;padding:2px 6px;" onclick="removeBuilderRow(this)" title="Delete Row">
                <i class="bi bi-trash-fill"></i>
            </button>
        </td>
    `;
    tbody.appendChild(tr);
    updateBuilderCount();
    updateBuilderOriginDest();
}

function addBuilderMultipleRows(count = 5) {
    for (let i = 0; i < count; i++) {
        addBuilderRow();
    }
}

function removeBuilderRow(btn) {
    const row = btn.closest("tr");
    if (row) {
        row.remove();
        reindexBuilderRows();
        updateBuilderCount();
        updateBuilderOriginDest();
    }
}

function reindexBuilderRows() {
    const rows = document.querySelectorAll("#builder-stops-tbody .builder-stop-row");
    rows.forEach((r, idx) => {
        const numCell = r.querySelector(".builder-row-num");
        if (numCell) numCell.textContent = idx + 1;
    });
}

function clearBuilderRows() {
    const tbody = document.getElementById("builder-stops-tbody");
    if (tbody) tbody.innerHTML = "";
    updateBuilderCount();
    updateBuilderOriginDest();
}

function updateBuilderCount() {
    const tbody = document.getElementById("builder-stops-tbody");
    const count = tbody ? tbody.children.length : 0;
    const label = document.getElementById("builder-count-label");
    if (label) label.textContent = count;
}

function updateBuilderOriginDest() {
    const rows = document.querySelectorAll("#builder-stops-tbody .builder-stop-row");
    if (rows.length > 0) {
        const firstName = rows[0].querySelector(".b-stop-name")?.value.trim();
        const lastName = rows[rows.length - 1].querySelector(".b-stop-name")?.value.trim();
        const origInput = document.getElementById("builder-origin");
        const destInput = document.getElementById("builder-destination");
        if (origInput && !origInput.value && firstName) origInput.value = firstName;
        if (destInput && lastName) destInput.value = lastName;
    }
}

async function load179StopsIntoBuilder() {
    try {
        const res = await fetch("/api/route-stops/up-routes");
        if (!res.ok) throw new Error("Failed to load UP routes");
        const data = await res.json();
        const route179 = data.routes.find(r => r.route_code === "UP-RT-11" || r.stops_count >= 150) || data.routes[0];
        if (!route179 || !route179.stops || route179.stops.length === 0) {
            showToast("179-stops route data not found in library.", "warning");
            return;
        }

        clearBuilderRows();
        const nameInput = document.getElementById("builder-route-name");
        const origInput = document.getElementById("builder-origin");
        const destInput = document.getElementById("builder-destination");
        if (nameInput) nameInput.value = route179.route_name;
        if (origInput) origInput.value = route179.origin;
        if (destInput) destInput.value = route179.destination;

        route179.stops.forEach(s => {
            addBuilderRow({
                name: s.name,
                address: s.address,
                time: s.time,
                speed: s.speed,
                major: s.major
            });
        });

        switchBuilderTab("list");
        showToast(`Loaded all ${route179.stops.length} stops from ${route179.route_name}!`, "success");
    } catch (e) {
        showToast("Error loading 179 stops: " + e.message, "error");
    }
}

function parseDropBoxToList() {
    const textarea = document.getElementById("builder-drop-box-textarea");
    if (!textarea || !textarea.value.trim()) {
        showToast("Please enter or paste stops into the drop box first.", "warning");
        return;
    }

    const rawText = textarea.value.trim();
    const lines = rawText.split(/\r?\n/).map(l => l.trim()).filter(l => l.length > 0);

    if (lines.length === 0) {
        showToast("No valid lines detected in drop box.", "warning");
        return;
    }

    clearBuilderRows();
    let parsedCount = 0;

    lines.forEach((line, idx) => {
        let cleaned = line.replace(/^(stop\s*\d+[\s:.-]*|\d+[\s:.-]+)/i, "").trim();
        const parts = cleaned.split(",").map(p => p.trim());
        let name = parts[0] || `Stop #${idx + 1}`;
        let address = parts[1] || name;
        let time = parts[2] || "";
        let speed = 8.0;

        for (let p of parts) {
            const m = p.match(/(\d+(\.\d+)?)\s*(km\/h|kmh)?/i);
            if (m && p !== name) {
                speed = parseFloat(m[1]);
            }
            if (/am|pm/i.test(p) && p !== name) {
                time = p;
            }
        }

        const isMajor = /stand|terminal|depot|station|metro|crossing|bypass/i.test(name) || idx === 0 || idx === lines.length - 1;

        addBuilderRow({
            name: name,
            address: address,
            time: time,
            speed: speed,
            major: isMajor
        });
        parsedCount++;
    });

    switchBuilderTab("list");
    showToast(`Successfully parsed ${parsedCount} stops from drop box into list! Review and click "Save & Generate Stops Timeline".`, "success");
}

async function submitBuilderStops() {
    const tbody = document.getElementById("builder-stops-tbody");
    const rows = tbody ? tbody.querySelectorAll(".builder-stop-row") : [];

    if (rows.length === 0) {
        showToast("Please add at least one route stop before saving.", "warning");
        return;
    }

    const stopsPayload = [];
    rows.forEach((r, idx) => {
        const name = r.querySelector(".b-stop-name")?.value.trim() || `Stop #${idx + 1}`;
        const address = r.querySelector(".b-stop-addr")?.value.trim() || name;
        const time = r.querySelector(".b-stop-time")?.value.trim() || "";
        const speed = parseFloat(r.querySelector(".b-stop-speed")?.value) || 8.0;
        const major = r.querySelector(".b-stop-major")?.checked || false;

        stopsPayload.push({
            name: name,
            address: address,
            time: time,
            speed: speed,
            major: major
        });
    });

    const routeName = document.getElementById("builder-route-name")?.value.trim() || "";
    const origin = document.getElementById("builder-origin")?.value.trim() || stopsPayload[0].name;
    const dest = document.getElementById("builder-destination")?.value.trim() || stopsPayload[stopsPayload.length - 1].name;

    const btn = document.getElementById("btn-save-builder-stops");
    btn.disabled = true;
    btn.innerHTML = `<i class="bi bi-hourglass-split spin"></i> Generating Timeline...`;

    try {
        const res = await fetch("/api/route-stops/bulk-update", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                bus_id: currentSelectedBusId,
                route_name: routeName,
                origin: origin,
                destination: dest,
                stops: stopsPayload
            })
        });

        const data = await res.json();
        btn.disabled = false;
        btn.innerHTML = `<i class="bi bi-check-circle-fill"></i> Save & Generate Stops Timeline`;

        if (res.ok && data.success) {
            showToast(`Timeline generated! Saved ${data.stops_count} stops for Bus ${data.bus_number}.`, "success");
            await loadRouteData(currentSelectedBusId);
            const card = document.getElementById("quick-stops-builder-card");
            if (card) card.style.display = "none";
        } else {
            showToast(data.error || "Failed to update stops.", "error");
        }
    } catch (err) {
        btn.disabled = false;
        btn.innerHTML = `<i class="bi bi-check-circle-fill"></i> Save & Generate Stops Timeline`;
        showToast("Error updating stops: " + err.message, "error");
    }
}


