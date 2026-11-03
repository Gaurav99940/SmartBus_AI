// ============================================================================
// Operator Dashboard JavaScript
// Assigned bus state, Geolocation tracking, GPS reporting & telemetry
// ============================================================================

let currentBusId = null;
let currentBusData = null;
let watchId = null;
let gpsIntervalTimer = null;
let lastReportedLocation = null;

document.addEventListener("DOMContentLoaded", () => {
    loadOperatorBus();

    // Check if auto-setup was requested in URL query
    const params = new URLSearchParams(window.location.search);
    if (params.get("setup") === "1") {
        setTimeout(openRegisterBusModal, 300);
    }
});

async function loadOperatorBus() {
    try {
        const res = await fetch("/api/buses/operator/my-buses");
        if (!res.ok) return;
        const data = await res.json();
        
        currentBusId = data.current_bus_id;
        const buses = data.buses || [];

        if (buses.length === 0 || !currentBusId) {
            // No bus assigned yet: show registration modal
            document.getElementById("op-bus-name").textContent = "No Bus Assigned Yet";
            document.getElementById("op-bus-route").textContent = "Please register or select a bus you are authorized to operate.";
            document.getElementById("op-bus-status-badge").textContent = "Unassigned";
            document.getElementById("op-bus-status-badge").className = "badge-status badge-needs-review";
            openRegisterBusModal();
            return;
        }

        currentBusData = buses.find(b => b.id === currentBusId) || buses[0];
        currentBusId = currentBusData.id;

        // Render bus header
        document.getElementById("op-bus-name").textContent = `${currentBusData.registration_number} - ${currentBusData.bus_name}`;
        document.getElementById("op-bus-route").textContent = `Route: ${currentBusData.route_name || (currentBusData.starting_point + ' ➔ ' + currentBusData.destination)}`;
        
        const statusBadge = document.getElementById("op-bus-status-badge");
        statusBadge.textContent = currentBusData.bus_status || "Active";
        statusBadge.className = currentBusData.bus_status === "Active" ? "badge-status badge-present" : "badge-status badge-possibly-absent";

        // Render active journey if available
        if (currentBusData.active_journey) {
            const aj = currentBusData.active_journey;
            document.getElementById("op-journey-code").textContent = aj.journey_code;
            document.getElementById("op-journey-terminals").textContent = `${aj.origin} ➔ ${aj.destination}`;
            document.getElementById("op-journey-status").textContent = aj.status.toUpperCase();
            document.getElementById("op-present-count").textContent = currentBusData.confirmed_present_count || 0;
            document.getElementById("op-total-count").textContent = currentBusData.total_journey_passengers || 0;
        } else {
            document.getElementById("op-journey-code").textContent = "No Active Journey";
            document.getElementById("op-journey-terminals").textContent = "Depot / Idle";
            document.getElementById("op-journey-status").textContent = "Idle";
        }

        // Check if latest location exists on bus record
        if (currentBusData.latest_latitude !== null && currentBusData.latest_longitude !== null) {
            updateTelemetryDisplay({
                latitude: currentBusData.latest_latitude,
                longitude: currentBusData.latest_longitude,
                accuracy: currentBusData.latest_gps_accuracy,
                location_source: currentBusData.latest_location_source || "DEVICE_API",
                location_time: currentBusData.latest_location_time,
                freshness: currentBusData.location_freshness
            });
        }

        // Fetch passengers and render 3D cabin
        try {
            const busRes = await fetch(`/api/buses/${currentBusId}`);
            if (busRes.ok) {
                const bDetails = await busRes.json();
                renderOperator3DCabin(bDetails.passengers || [], currentBusData.capacity || 40);
            }
        } catch (e) {
            console.error("Failed to fetch bus passengers for 3D view:", e);
        }

    } catch (e) {
        console.error("Failed to load operator bus:", e);
    }
}

// ----------------------------------------------------------------------------
// Geolocation Tracking (PART 2)
// ----------------------------------------------------------------------------

function startOperatorGPS() {
    if (!navigator.geolocation) {
        showToast("Geolocation is not supported by your browser.", "error");
        setGpsStatusBadge("Unavailable", "badge-needs-review");
        return;
    }

    const btn = document.getElementById("btn-enable-gps");
    btn.disabled = true;
    btn.innerHTML = `<i class="bi bi-hourglass-split"></i> Acquiring Location...`;

    showToast("Requesting device GPS location permission...", "info");

    const onLocationSuccess = async (pos) => {
        btn.disabled = false;
        btn.innerHTML = `<i class="bi bi-check-circle-fill"></i> Location Active`;
        document.getElementById("gps-request-banner").style.display = "none";

        const lat = pos.coords.latitude;
        const lon = pos.coords.longitude;
        const acc = pos.coords.accuracy;

        await sendLocationUpdate(lat, lon, acc, "DEVICE_API");
    };

    const onLocationError = (err) => {
        btn.disabled = false;
        btn.innerHTML = `<i class="bi bi-crosshair"></i> Enable Location`;

        let msg = "Could not retrieve GPS coordinates.";
        let badgeState = "Offline";

        if (err.code === 1) { // PERMISSION_DENIED
            msg = "Location permission was denied. You can enter location manually.";
            badgeState = "Permission Denied";
            setGpsStatusBadge(badgeState, "badge-needs-review");
            document.getElementById("disp-gps-freshness").textContent = badgeState;
        } else if (err.code === 2) { // POSITION_UNAVAILABLE
            msg = "Location position unavailable. Check GPS sensor.";
            badgeState = "Unavailable";
            setGpsStatusBadge(badgeState, "badge-possibly-absent");
        } else if (err.code === 3) { // TIMEOUT
            msg = "Location request timed out.";
            badgeState = "Timeout";
            setGpsStatusBadge(badgeState, "badge-possibly-absent");
        }

        showToast(msg, "warning");
    };

    // Request immediate position
    navigator.geolocation.getCurrentPosition(onLocationSuccess, onLocationError, {
        enableHighAccuracy: true,
        timeout: 10000,
        maximumAge: 15000
    });

    // Setup periodic position reporting every 30 seconds
    if (gpsIntervalTimer) clearInterval(gpsIntervalTimer);
    gpsIntervalTimer = setInterval(() => {
        navigator.geolocation.getCurrentPosition(
            async (p) => {
                await sendLocationUpdate(p.coords.latitude, p.coords.longitude, p.coords.accuracy, "DEVICE_API");
            },
            () => {},
            { enableHighAccuracy: true, timeout: 8000, maximumAge: 20000 }
        );
    }, 30000);
}

async function sendLocationUpdate(lat, lon, accuracy, source) {
    if (!currentBusId) {
        showToast("Please assign a bus before sending location updates.", "warning");
        return;
    }

    try {
        const res = await fetch("/api/buses/operator/location", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                latitude: lat,
                longitude: lon,
                accuracy: accuracy,
                location_source: source,
                bus_id: currentBusId
            })
        });

        if (res.ok) {
            const data = await res.json();
            updateTelemetryDisplay(data.location);
        } else {
            const err = await res.json();
            console.error("GPS reporting error:", err);
        }
    } catch (e) {
        console.error("Network error reporting location:", e);
    }
}

function updateTelemetryDisplay(loc) {
    if (!loc) return;
    lastReportedLocation = loc;

    document.getElementById("disp-gps-lat").textContent = loc.latitude !== null ? loc.latitude.toFixed(6) : "-";
    document.getElementById("disp-gps-lon").textContent = loc.longitude !== null ? loc.longitude.toFixed(6) : "-";
    document.getElementById("disp-gps-accuracy").textContent = loc.accuracy ? `±${Math.round(loc.accuracy)} m` : "Normal";
    document.getElementById("disp-gps-source").textContent = loc.location_source || "DEVICE_API";
    document.getElementById("disp-gps-freshness").textContent = loc.freshness || "Live";

    const syncEl = document.getElementById("gps-last-sync");
    if (syncEl) syncEl.textContent = `Last update: ${loc.location_time || 'Just now'}`;

    // Freshness badge
    const badge = document.getElementById("gps-status-badge");
    if (loc.freshness === "Live") {
        setGpsStatusBadge("Live GPS Active", "badge-present");
    } else if (loc.freshness === "Stale") {
        setGpsStatusBadge("Location Stale", "badge-possibly-absent");
    } else if (loc.freshness === "Manual Location") {
        setGpsStatusBadge("Manual Location", "badge-conf-med");
    } else {
        setGpsStatusBadge("Offline", "badge-exit-confirmed");
    }
}

function setGpsStatusBadge(text, className) {
    const badge = document.getElementById("gps-status-badge");
    if (badge) {
        badge.textContent = text;
        badge.className = `badge-status ${className}`;
    }
}

// ----------------------------------------------------------------------------
// Manual Location Entry
// ----------------------------------------------------------------------------

function openManualLocationModal() {
    document.getElementById("manual-location-modal").classList.add("active");
}

function closeManualLocationModal() {
    document.getElementById("manual-location-modal").classList.remove("active");
}

async function handleManualLocationSubmit(e) {
    e.preventDefault();
    const lat = parseFloat(document.getElementById("manual-lat").value);
    const lon = parseFloat(document.getElementById("manual-lon").value);

    if (isNaN(lat) || isNaN(lon)) {
        showToast("Please enter valid latitude and longitude numbers.", "error");
        return;
    }

    await sendLocationUpdate(lat, lon, null, "MANUAL_ENTRY");
    closeManualLocationModal();
    showToast("Manual location recorded successfully.", "success");
    document.getElementById("gps-request-banner").style.display = "none";
}

// ----------------------------------------------------------------------------
// Switch Bus Modal (PART 1)
// ----------------------------------------------------------------------------

async function openSwitchBusModal() {
    try {
        const res = await fetch("/api/buses");
        if (!res.ok) return;
        const allBuses = await res.json();
        const select = document.getElementById("switch-bus-select");
        select.innerHTML = "";

        allBuses.forEach(b => {
            const opt = document.createElement("option");
            opt.value = b.id;
            opt.textContent = `${b.registration_number || b.bus_number} - ${b.bus_name || 'Bus'} (${b.route_name || 'Route'})`;
            if (b.id === currentBusId) opt.selected = true;
            select.appendChild(opt);
        });

        document.getElementById("switch-bus-modal").classList.add("active");
    } catch (e) {
        showToast("Failed to load buses for switching.", "error");
    }
}

function closeSwitchBusModal() {
    document.getElementById("switch-bus-modal").classList.remove("active");
}

async function confirmSwitchBus() {
    const targetBusId = document.getElementById("switch-bus-select").value;
    if (!targetBusId) return;

    try {
        const res = await fetch(`/api/buses/${targetBusId}/select`, { method: "POST" });
        if (res.ok) {
            showToast("Assigned bus switched successfully.", "success");
            closeSwitchBusModal();
            loadOperatorBus();
        } else {
            showToast("Failed to switch bus.", "error");
        }
    } catch (e) {
        showToast("Error switching bus.", "error");
    }
}

// ----------------------------------------------------------------------------
// Register New Bus Modal (PART 1)
// ----------------------------------------------------------------------------

function openRegisterBusModal() {
    document.getElementById("register-bus-modal").classList.add("active");
}

function closeRegisterBusModal() {
    document.getElementById("register-bus-modal").classList.remove("active");
}

async function handleRegisterBus(e) {
    e.preventDefault();
    const btn = document.getElementById("btn-save-bus");
    btn.disabled = true;

    const payload = {
        registration_number: document.getElementById("reg-bus-number").value.trim(),
        bus_number: document.getElementById("reg-bus-id").value.trim(),
        bus_name: document.getElementById("reg-bus-name").value.trim(),
        bus_type: document.getElementById("reg-bus-type").value,
        capacity: parseInt(document.getElementById("reg-bus-capacity").value),
        route_number: document.getElementById("reg-route-number").value.trim(),
        bus_status: document.getElementById("reg-bus-status").value,
        starting_point: document.getElementById("reg-starting-point").value.trim(),
        destination: document.getElementById("reg-destination").value.trim(),
        operator_name: document.getElementById("reg-operator-name").value.trim(),
        operator_contact: document.getElementById("reg-operator-contact").value.trim()
    };

    try {
        const res = await fetch("/api/buses", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });

        const data = await res.json();
        btn.disabled = false;

        if (res.ok) {
            showToast(data.message || "Bus registered and assigned successfully!", "success");
            closeRegisterBusModal();
            loadOperatorBus();
        } else {
            showToast(data.error || "Failed to register bus.", "error");
        }
    } catch (err) {
        btn.disabled = false;
        showToast("Error saving bus registration.", "error");
    }
}

// ============================================================================
// Operator 3D Bus Cabin & Interior Seat Occupancy Logic
// ============================================================================

let isOpCabin3D = true;
let opCabinRotateX = 50;
let opCabinRotateZ = -20;

function renderOperator3DCabin(passengers, capacity = 24) {
    const grid = document.getElementById("op-cabin-seating-grid");
    const rearBench = document.getElementById("op-cabin-rear-bench");
    const pill = document.getElementById("op-occupancy-pill");
    if (!grid) return;

    grid.innerHTML = "";
    if (rearBench) rearBench.innerHTML = "";

    const seatMap = {};
    (passengers || []).forEach(p => {
        const seat = p.current_seat || "1A";
        seatMap[seat] = p;
    });

    const confirmedCount = (passengers || []).filter(p => p.status === "PRESENT").length;
    const occupancyPct = Math.round((confirmedCount / 24) * 100);
    if (pill) {
        pill.textContent = `${confirmedCount} / 24 Visible Seats Occupied (${occupancyPct}%)`;
    }

    // Rows 1 to 6 matching CCTV bus interior view
    for (let row = 1; row <= 6; row++) {
        const rowEl = document.createElement("div");
        rowEl.className = "bus-seat-row";

        // Left: Window A, Aisle B
        ["A", "B"].forEach(col => {
            const seatCode = `${row}${col}`;
            rowEl.appendChild(createOpSeatNode(seatCode, seatMap[seatCode]));
        });

        // Center Aisle pathway
        const aisle = document.createElement("div");
        aisle.className = "bus-aisle-path";
        aisle.textContent = `ROW ${row}`;
        rowEl.appendChild(aisle);

        // Right: Aisle C, Window D
        ["C", "D"].forEach(col => {
            const seatCode = `${row}${col}`;
            rowEl.appendChild(createOpSeatNode(seatCode, seatMap[seatCode]));
        });

        grid.appendChild(rowEl);
    }
}

function createOpSeatNode(seatCode, passenger) {
    const node = document.createElement("div");
    node.dataset.seat = seatCode;

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
        node.title = `Seat ${seatCode} (Empty)`;
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

        const charCode = passenger.person_id ? passenger.person_id.charCodeAt(passenger.person_id.length - 1) : 0;
        const colorIdx = charCode % 5;

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

        node.title = `${passenger.person_id} • Status: ${statusText} • Seat: ${seatCode}`;
    }

    return node;
}

function toggleOperatorCabinView() {
    const stage = document.getElementById("op-cabin-stage");
    const btn = document.getElementById("btn-op-toggle-view");
    if (!stage) return;

    if (isOpCabin3D) {
        stage.style.transform = "rotateX(0deg) rotateZ(0deg)";
        btn.innerHTML = `<i class="bi bi-badge-3d"></i> 3D Perspective`;
        isOpCabin3D = false;
    } else {
        stage.style.transform = `rotateX(${opCabinRotateX}deg) rotateZ(${opCabinRotateZ}deg)`;
        btn.innerHTML = `<i class="bi bi-layers"></i> 2D Top View`;
        isOpCabin3D = true;
    }
}

function rotateOperatorCabin(delta) {
    const stage = document.getElementById("op-cabin-stage");
    if (!stage) return;
    opCabinRotateZ += delta;
    stage.style.transform = `rotateX(${opCabinRotateX}deg) rotateZ(${opCabinRotateZ}deg)`;
    isOpCabin3D = true;
    const btn = document.getElementById("btn-op-toggle-view");
    if (btn) btn.innerHTML = `<i class="bi bi-layers"></i> 2D Top View`;
}

function resetOperatorCabinView() {
    const stage = document.getElementById("op-cabin-stage");
    if (!stage) return;
    opCabinRotateX = 50;
    opCabinRotateZ = -20;
    stage.style.transform = `rotateX(${opCabinRotateX}deg) rotateZ(${opCabinRotateZ}deg)`;
    isOpCabin3D = true;
    const btn = document.getElementById("btn-op-toggle-view");
    if (btn) btn.innerHTML = `<i class="bi bi-layers"></i> 2D Top View`;
}

// ============================================================================
// Operator Storage Analysis & Historical Data Cleanup Handlers
// ============================================================================

async function openOperatorCleanupModal() {
    const modal = document.getElementById("operator-cleanup-modal");
    if (!modal) return;
    modal.classList.add("active");
    modal.style.display = "flex";
    modal.style.opacity = "1";
    modal.style.visibility = "visible";
    loadOperatorStorageAnalysis();
}

function closeOperatorCleanupModal() {
    const modal = document.getElementById("operator-cleanup-modal");
    if (modal) {
        modal.classList.remove("active");
        modal.style.display = "none";
        modal.style.opacity = "0";
        modal.style.visibility = "hidden";
    }
}

async function loadOperatorStorageAnalysis() {
    try {
        const res = await fetch("/api/settings/data-analysis");
        if (!res.ok) return;
        const data = await res.json();

        const s = data.storage;
        const setVal = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.textContent = val;
        };

        setVal("op-storage-uploads-mb", `${s.uploads_dir_mb} MB`);
        setVal("op-storage-uploads-cnt", `${s.uploads_file_count} photos`);
        setVal("op-storage-crops-mb", `${s.crops_dir_mb} MB`);
        setVal("op-storage-crops-cnt", `${s.crops_file_count} crops`);
        setVal("op-storage-db-mb", `${s.database_file_mb} MB`);
        setVal("op-storage-total-mb", `${s.total_storage_mb} MB`);

        const tbody = document.getElementById("op-breakdown-tbody");
        if (!tbody) return;
        tbody.innerHTML = "";

        if (!data.journeys || data.journeys.length === 0) {
            tbody.innerHTML = `<tr><td colspan="7" class="text-center py-3" style="color:var(--text-muted)">No journeys recorded in database.</td></tr>`;
            return;
        }

        data.journeys.forEach(j => {
            const tr = document.createElement("tr");
            tr.innerHTML = `
                <td><strong>${j.journey_code}</strong></td>
                <td>${j.bus_number}</td>
                <td>${j.route}</td>
                <td><span class="badge-status ${j.status === 'completed' ? 'badge-exit-confirmed' : 'badge-present'}">${j.status}</span></td>
                <td>${j.uploads_count}</td>
                <td>${j.passengers_count}</td>
                <td>
                    <button class="btn-secondary" style="padding:3px 8px;font-size:11px;color:#ef4444;" onclick="deleteOperatorSingleJourney(${j.id}, '${j.journey_code}')">
                        <i class="bi bi-trash3 text-danger"></i> Delete
                    </button>
                </td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.error("Storage analysis load error:", e);
    }
}

async function deleteOperatorSingleJourney(journeyId, journeyCode) {
    if (!confirm(`Are you sure you want to permanently delete Journey ${journeyCode} and all its uploaded photos, face crops, and passenger history? This cannot be undone.`)) {
        return;
    }

    try {
        const res = await fetch(`/api/journeys/${journeyId}`, { method: "DELETE" });
        const data = await res.json();
        if (res.ok) {
            showToast(data.message || `Journey ${journeyCode} deleted.`, "success");
            loadOperatorStorageAnalysis();
            loadOperatorBus();
        } else {
            showToast(data.error || "Failed to delete journey.", "error");
        }
    } catch (err) {
        showToast("Error deleting journey.", "error");
    }
}

async function executeOperatorDataCleanup() {
    const select = document.getElementById("op-cleanup-select");
    const cleanupType = select.value;
    const selectedOpt = select.options[select.selectedIndex];
    const days = selectedOpt.dataset.days ? parseInt(selectedOpt.dataset.days) : 30;
    const deleteFiles = document.getElementById("op-delete-files").checked;
    const deleteAudits = document.getElementById("op-delete-audits").checked;

    if (!confirm("Are you sure you want to permanently delete the selected historical journey and photo data? This will reclaim physical disk space.")) {
        return;
    }

    const btn = document.getElementById("btn-run-op-cleanup");
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner-border spinner-border-sm"></span> Purging...`;

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
        const alertBox = document.getElementById("op-cleanup-alert");
        if (alertBox) {
            alertBox.style.display = "block";
            if (res.ok) {
                alertBox.style.background = "#dcfce7";
                alertBox.style.color = "#15803d";
                alertBox.innerHTML = `<strong><i class="bi bi-check-circle-fill"></i> Cleanup Succeeded:</strong> ${data.message}`;
            } else {
                alertBox.style.background = "#fee2e2";
                alertBox.style.color = "#b91c1c";
                alertBox.textContent = data.error || "Failed to execute cleanup.";
            }
        }

        if (res.ok) {
            showToast(data.message, "success");
            loadOperatorStorageAnalysis();
            loadOperatorBus();
        }
    } catch (e) {
        showToast("Error executing data cleanup", "error");
    } finally {
        btn.disabled = false;
        btn.innerHTML = `<i class="bi bi-trash3"></i> Delete History`;
    }
}


