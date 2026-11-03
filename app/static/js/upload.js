// ============================================================================
// SmartBus AI Passenger Tracking System
// Multi-Angle Bus Passenger Detection & Counting Controller
// ============================================================================

let pickerMap = null;
let pickerMarker = null;

// Multi-angle state
const selectedAngleFiles = {
    A: null,
    B: null,
    C: null
};

// Cached configs and results
let cameraAngleConfigs = {};
let latestMultiAngleResults = null;
let currentActiveHudAngle = 'A';

document.addEventListener("DOMContentLoaded", () => {
    loadActiveJourneys();
    loadRouteStopsForUpload();
    loadSeatingAreaConfigurations();
    setupAngleDropzones();
    initMiniMapPicker();
    clearWorkspaceUI();
});

// ----------------------------------------------------------------------------
// Seating Area Configuration
// ----------------------------------------------------------------------------

async function loadSeatingAreaConfigurations() {
    try {
        const res = await fetch("/api/uploads/seating-config");
        if (res.ok) {
            const configs = await res.json();
            configs.forEach(cfg => {
                const angle = cfg.camera_angle.toUpperCase();
                cameraAngleConfigs[angle] = cfg;

                // Update UI card titles & badges
                const titleEl = document.getElementById(`title-angle-${angle.toLowerCase()}`);
                const labelEl = document.getElementById(`label-angle-${angle.toLowerCase()}`);
                if (titleEl) titleEl.textContent = cfg.title;
                if (labelEl) labelEl.textContent = `${cfg.seating_area_label} (Target: ${cfg.target_capacity} seats)`;
            });
        }
    } catch (err) {
        console.error("Failed to load seating area configs:", err);
    }
}

function openSeatingConfigModal(angle = 'A') {
    const modal = document.getElementById("seating-config-modal");
    if (!modal) return;
    const select = document.getElementById("sc-angle-select");
    if (select) select.value = angle;
    loadSelectedAngleConfigForm(angle);
    modal.classList.add("active");
    modal.style.display = "flex";
}

function closeSeatingConfigModal() {
    const modal = document.getElementById("seating-config-modal");
    if (modal) {
        modal.classList.remove("active");
        modal.style.display = "none";
    }
}

function loadSelectedAngleConfigForm(angle) {
    const cfg = cameraAngleConfigs[angle] || {
        title: `Angle ${angle} — Assigned Seating Section`,
        seating_area_label: `Section ${angle}`,
        target_seats: "1A, 1B, 1C, 1D",
        target_capacity: 4,
        roi: { x_min: 0.0, y_min: 0.0, x_max: 1.0, y_max: 1.0 }
    };

    document.getElementById("sc-title").value = cfg.title || "";
    document.getElementById("sc-label").value = cfg.seating_area_label || "";
    document.getElementById("sc-target-seats").value = cfg.target_seats || "";
    document.getElementById("sc-target-capacity").value = cfg.target_capacity || 4;

    const roi = cfg.roi || { x_min: 0.0, y_min: 0.0, x_max: 1.0, y_max: 1.0 };
    document.getElementById("sc-roi-xmin").value = roi.x_min !== undefined ? roi.x_min : 0.0;
    document.getElementById("sc-roi-ymin").value = roi.y_min !== undefined ? roi.y_min : 0.0;
    document.getElementById("sc-roi-xmax").value = roi.x_max !== undefined ? roi.x_max : 1.0;
    document.getElementById("sc-roi-ymax").value = roi.y_max !== undefined ? roi.y_max : 1.0;
}

async function handleSeatingConfigSave(e) {
    e.preventDefault();
    const angle = document.getElementById("sc-angle-select").value;
    const payload = {
        camera_angle: angle,
        title: document.getElementById("sc-title").value.trim(),
        seating_area_label: document.getElementById("sc-label").value.trim(),
        target_seats: document.getElementById("sc-target-seats").value.trim(),
        target_capacity: parseInt(document.getElementById("sc-target-capacity").value, 10) || 4,
        roi: {
            x_min: parseFloat(document.getElementById("sc-roi-xmin").value) || 0.0,
            y_min: parseFloat(document.getElementById("sc-roi-ymin").value) || 0.0,
            x_max: parseFloat(document.getElementById("sc-roi-xmax").value) || 1.0,
            y_max: parseFloat(document.getElementById("sc-roi-ymax").value) || 1.0
        }
    };

    try {
        const res = await fetch("/api/uploads/seating-config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (res.ok && data.success) {
            showToast(`Configuration for Angle ${angle} updated!`, "success");
            closeSeatingConfigModal();
            await loadSeatingAreaConfigurations();
        } else {
            showToast(data.error || "Failed to update configuration.", "error");
        }
    } catch (err) {
        showToast("Error updating seating area config.", "error");
    }
}

// ----------------------------------------------------------------------------
// Journey Selection & Auto-Launch
// ----------------------------------------------------------------------------

async function loadActiveJourneys() {
    try {
        const res = await fetch("/api/journeys?status=active");
        const select = document.getElementById("journey-select");
        const banner = document.getElementById("no-journey-banner");
        if (!select) return;

        select.innerHTML = "";
        let journeys = [];
        if (res.ok) {
            journeys = await res.json();
        }

        if (journeys.length === 0) {
            if (banner) banner.style.display = "block";
            let defaultBusText = "Assigned Bus";
            let defaultBusId = "";
            try {
                const bRes = await fetch("/api/buses/operator/my-buses");
                if (bRes.ok) {
                    const bData = await bRes.json();
                    if (bData.buses && bData.buses.length > 0) {
                        const curBus = bData.buses.find(b => b.id === bData.current_bus_id) || bData.buses[0];
                        defaultBusText = `${curBus.registration_number || curBus.bus_number} - ${curBus.bus_name || 'Bus'}`;
                        defaultBusId = curBus.id;
                    }
                }
            } catch (err) {
                console.error("Bus fetch error:", err);
            }

            select.innerHTML = `<option value="auto_create" data-bus-id="${defaultBusId}" selected>⚡ Auto-Launch New Journey (${defaultBusText})</option>`;
            return;
        }

        if (banner) banner.style.display = "none";
        journeys.forEach(j => {
            const opt = document.createElement("option");
            opt.value = j.id;
            opt.textContent = `${j.journey_code} - Bus ${j.bus_number || j.bus_id} (${j.origin} -> ${j.destination})`;
            select.appendChild(opt);
        });
        if (select) {
            select.addEventListener("change", () => loadRouteStopsForUpload());
        }
    } catch (e) {
        console.error("Failed to load journeys:", e);
    }
}

// ----------------------------------------------------------------------------
// Bus Route Stops Selector for Photo Geolocation
// ----------------------------------------------------------------------------

let uploadRouteStops = [];

async function loadRouteStopsForUpload() {
    const stopSelect = document.getElementById("stop-select");
    if (!stopSelect) return;

    const journeySelect = document.getElementById("journey-select");
    const journeyId = journeySelect ? journeySelect.value : "";
    let url = "/api/route-stops";
    if (journeyId && journeyId !== "auto_create") {
        url += `?journey_id=${journeyId}`;
    }

    try {
        const res = await fetch(url);
        if (!res.ok) return;
        const data = await res.json();
        if (data.success && data.stops) {
            uploadRouteStops = data.stops;
            stopSelect.innerHTML = `<option value="">-- Select Stop / Location (${data.stops.length} Stops on Route) --</option>`;
            data.stops.forEach(s => {
                const opt = document.createElement("option");
                opt.value = s.id;
                opt.textContent = `Stop #${s.stop_order}: ${s.stop_name} (${s.location_address ? s.location_address.substring(0, 45) : ''})`;
                stopSelect.appendChild(opt);
            });
        }
    } catch (e) {
        console.error("Failed to load stops for upload:", e);
    }
}

function onStopSelectChange(stopId) {
    if (!stopId) return;
    const stop = uploadRouteStops.find(s => s.id == stopId);
    if (!stop) return;

    updateCoordInputs(stop.latitude, stop.longitude, "STOP_PICKER");
    if (pickerMap && pickerMarker) {
        pickerMap.setView([stop.latitude, stop.longitude], 15);
        pickerMarker.setLatLng([stop.latitude, stop.longitude]);
    }
    showToast(`Photo location set to Stop #${stop.stop_order}: ${stop.stop_name}`, "info");
}

// ----------------------------------------------------------------------------
// Multi-Angle Dropzone Setup & File Handlers
// ----------------------------------------------------------------------------

function setupAngleDropzones() {
    ["A", "B", "C"].forEach(angle => {
        const lower = angle.toLowerCase();
        const dropzone = document.getElementById(`dropzone-${lower}`);
        const fileInput = document.getElementById(`file-input-${lower}`);
        if (!dropzone || !fileInput) return;

        dropzone.addEventListener("click", () => fileInput.click());

        dropzone.addEventListener("dragover", (e) => {
            e.preventDefault();
            dropzone.classList.add("dragover");
        });

        dropzone.addEventListener("dragleave", () => {
            dropzone.classList.remove("dragover");
        });

        dropzone.addEventListener("drop", (e) => {
            e.preventDefault();
            dropzone.classList.remove("dragover");
            if (e.dataTransfer.files.length > 0) {
                handleAngleFileSelected(angle, e.dataTransfer.files[0]);
            }
        });
    });
}

async function handleAngleFileSelected(angle, file) {
    if (!file) return;

    selectedAngleFiles[angle] = file;
    const lower = angle.toLowerCase();

    // Show preview (detect whether video or image)
    const previewContainer = document.getElementById(`preview-container-${lower}`);
    const previewImg = document.getElementById(`preview-img-${lower}`);
    const previewVideo = document.getElementById(`preview-video-${lower}`);
    const prompt = document.getElementById(`prompt-${lower}`);
    const statusBadge = document.getElementById(`status-badge-${lower}`);
    const card = document.getElementById(`card-angle-${lower}`);

    const isVideo = file.type.startsWith("video/") || /\.(mp4|avi|mov|mkv|webm|m4v)$/i.test(file.name);

    if (previewContainer && prompt) {
        if (isVideo && previewVideo) {
            const videoUrl = URL.createObjectURL(file);
            previewVideo.src = videoUrl;
            previewVideo.style.display = "block";
            if (previewImg) previewImg.style.display = "none";
            previewContainer.style.display = "block";
            prompt.style.display = "none";
        } else if (previewImg) {
            const reader = new FileReader();
            reader.onload = (e) => {
                previewImg.src = e.target.result;
                previewImg.style.display = "block";
                if (previewVideo) previewVideo.style.display = "none";
                previewContainer.style.display = "block";
                prompt.style.display = "none";
            };
            reader.readAsDataURL(file);
        }
    }

    if (statusBadge) {
        statusBadge.textContent = "Ready";
        statusBadge.className = "badge-status badge-present";
    }
    if (card) {
        card.style.borderColor = "var(--accent-primary)";
    }

    updateReadinessSummary();

    // Pre-inspect EXIF metadata for individual timestamp and GPS
    const formData = new FormData();
    formData.append("photo", file);

    try {
        const res = await fetch("/api/uploads/inspect-metadata", {
            method: "POST",
            body: formData
        });

        if (res.ok) {
            const data = await res.json();
            const tsInput = document.getElementById(`timestamp-${lower}`);
            const masterTsInput = document.getElementById("input-timestamp");

            if (data.capture_timestamp) {
                if (tsInput) tsInput.value = data.capture_timestamp;
                if (masterTsInput && !masterTsInput.value) masterTsInput.value = data.capture_timestamp;
            } else {
                const now = new Date();
                const pad = (n) => n.toString().padStart(2, "0");
                const defaultTime = `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
                if (tsInput && !tsInput.value) tsInput.value = defaultTime;
                if (masterTsInput && !masterTsInput.value) masterTsInput.value = defaultTime;
            }

            if (data.latitude !== null && data.longitude !== null) {
                updateCoordInputs(data.latitude, data.longitude, "EXIF");
                if (pickerMap && pickerMarker) {
                    pickerMap.setView([data.latitude, data.longitude], 15);
                    pickerMarker.setLatLng([data.latitude, data.longitude]);
                }
            }

            if (data.warnings && data.warnings.length > 0) {
                const warnBox = document.getElementById("metadata-warnings");
                if (warnBox) {
                    warnBox.innerHTML = `<strong>Angle ${angle} Metadata Notice:</strong> ` + data.warnings.join(" | ");
                    warnBox.style.display = "block";
                }
            }
        }
    } catch (e) {
        console.error(`Metadata inspection error for Angle ${angle}:`, e);
    }
}

function removeAnglePhoto(angle) {
    selectedAngleFiles[angle] = null;
    const lower = angle.toLowerCase();

    const previewContainer = document.getElementById(`preview-container-${lower}`);
    const previewImg = document.getElementById(`preview-img-${lower}`);
    const previewVideo = document.getElementById(`preview-video-${lower}`);
    const prompt = document.getElementById(`prompt-${lower}`);
    const fileInput = document.getElementById(`file-input-${lower}`);
    const statusBadge = document.getElementById(`status-badge-${lower}`);
    const card = document.getElementById(`card-angle-${lower}`);
    const tsInput = document.getElementById(`timestamp-${lower}`);
    const metricsPill = document.getElementById(`metrics-pill-${lower}`);
    const errBox = document.getElementById(`error-box-${lower}`);

    if (fileInput) fileInput.value = "";
    if (previewImg) { previewImg.src = "#"; previewImg.style.display = "none"; }
    if (previewVideo) { previewVideo.src = ""; previewVideo.style.display = "none"; }
    if (previewContainer) previewContainer.style.display = "none";
    if (prompt) prompt.style.display = "block";
    if (tsInput) tsInput.value = "";
    if (metricsPill) metricsPill.style.display = "none";
    if (errBox) errBox.style.display = "none";

    if (statusBadge) {
        statusBadge.textContent = "Missing";
        statusBadge.className = "badge-status";
        statusBadge.style.background = "#f1f5f9";
        statusBadge.style.color = "#64748b";
    }
    if (card) {
        card.style.borderColor = "transparent";
    }

    updateReadinessSummary();
}

function updateReadinessSummary() {
    const selected = Object.keys(selectedAngleFiles).filter(k => selectedAngleFiles[k] !== null);
    const count = selected.length;
    const badge = document.getElementById("angles-readiness-badge");
    const hint = document.getElementById("submission-status-hint");

    if (badge) {
        badge.textContent = `${count} of 3 Angles Selected (${selected.join(", ") || "None"})`;
        if (count === 3) {
            badge.className = "badge-status badge-present";
        } else if (count > 0) {
            badge.className = "badge-status badge-possibly-absent";
        } else {
            badge.className = "badge-status";
            badge.style.background = "#f1f5f9";
            badge.style.color = "#64748b";
        }
    }

    if (hint) {
        if (count === 3) {
            hint.innerHTML = `<span style="color:#10b981;font-weight:600;"><i class="bi bi-check-circle-fill"></i> Complete 3-Angle Coverage:</span> Angles A, B, and C ready for consolidated analysis.`;
        } else if (count > 0) {
            const missing = ["A", "B", "C"].filter(k => !selected.includes(k));
            hint.innerHTML = `<span style="color:#f59e0b;font-weight:600;"><i class="bi bi-info-circle-fill"></i> Partial Submission:</span> Processing ${selected.map(a => `Angle ${a}`).join(" & ")}; (Missing: ${missing.map(m => `Angle ${m}`).join(", ") || "None"}).`;
        } else {
            hint.textContent = "Select Angle A, B, and/or C above to begin multi-angle processing.";
        }
    }
}

// ----------------------------------------------------------------------------
// Geolocation & Map Picker
// ----------------------------------------------------------------------------

function initMiniMapPicker() {
    const container = document.getElementById("picker-map");
    if (!container) return;

    const defaultLat = 37.7749;
    const defaultLng = -122.4194;

    pickerMap = L.map("picker-map").setView([defaultLat, defaultLng], 13);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution: '&copy; OpenStreetMap contributors'
    }).addTo(pickerMap);

    pickerMarker = L.marker([defaultLat, defaultLng], { draggable: true }).addTo(pickerMap);

    pickerMarker.on("dragend", function (e) {
        const coord = e.target.getLatLng();
        updateCoordInputs(coord.lat, coord.lng, "MAP_PICKER");
    });

    pickerMap.on("click", function (e) {
        pickerMarker.setLatLng(e.latlng);
        updateCoordInputs(e.latlng.lat, e.latlng.lng, "MAP_PICKER");
    });
}

function updateCoordInputs(lat, lng, source) {
    const latInput = document.getElementById("input-latitude");
    const lonInput = document.getElementById("input-longitude");
    const srcInput = document.getElementById("input-location-source");

    if (latInput) latInput.value = lat.toFixed(6);
    if (lonInput) lonInput.value = lng.toFixed(6);
    if (srcInput) srcInput.value = source;
}

function useDeviceGPS() {
    if (!navigator.geolocation) {
        showToast("Geolocation is not supported by your browser.", "error");
        return;
    }
    showToast("Acquiring device GPS coordinates...", "info");
    navigator.geolocation.getCurrentPosition(
        (pos) => {
            const lat = pos.coords.latitude;
            const lng = pos.coords.longitude;
            updateCoordInputs(lat, lng, "DEVICE_API");
            if (pickerMap && pickerMarker) {
                pickerMap.setView([lat, lng], 15);
                pickerMarker.setLatLng([lat, lng]);
            }
            showToast(`GPS acquired (Accuracy: ±${Math.round(pos.coords.accuracy)}m)`, "success");
        },
        (err) => {
            showToast(`Unable to retrieve GPS: ${err.message}`, "warning");
        },
        { enableHighAccuracy: true, timeout: 8000 }
    );
}

// ----------------------------------------------------------------------------
// Submit Multi-Angle Bus Photos
// ----------------------------------------------------------------------------

async function submitMultiAnglePhotos() {
    const selectedKeys = Object.keys(selectedAngleFiles).filter(k => selectedAngleFiles[k] !== null);
    if (selectedKeys.length === 0) {
        showToast("Please select at least one bus camera-angle photo (Angle A, B, or C) to upload.", "error");
        return;
    }

    let journeyId = document.getElementById("journey-select").value;
    if (!journeyId || journeyId === "auto_create") {
        journeyId = await quickStartActiveJourney();
        if (!journeyId) {
            showToast("Please launch or select an active journey first.", "error");
            return;
        }
    }

    const formData = new FormData();
    formData.append("journey_id", journeyId);
    formData.append("latitude", document.getElementById("input-latitude").value);
    formData.append("longitude", document.getElementById("input-longitude").value);
    formData.append("location_source", document.getElementById("input-location-source").value);
    formData.append("capture_timestamp", document.getElementById("input-timestamp").value);
    formData.append("stop_id", document.getElementById("stop-select")?.value || "");

    // Append individual angle photos and their individual timestamps
    selectedKeys.forEach(angle => {
        const lower = angle.toLowerCase();
        formData.append(`photo_${lower}`, selectedAngleFiles[angle]);
        const tsVal = document.getElementById(`timestamp-${lower}`).value;
        if (tsVal) formData.append(`capture_timestamp_${lower}`, tsVal);

        // Update card status to processing
        const badge = document.getElementById(`status-badge-${lower}`);
        if (badge) {
            badge.textContent = "Processing...";
            badge.className = "badge-status badge-possibly-absent";
        }
    });

    const submitBtn = document.getElementById("submit-upload-btn");
    submitBtn.disabled = true;
    submitBtn.innerHTML = `<i class="bi bi-cpu"></i> Running Multi-Angle AI Detection Pipeline...`;

    try {
        const res = await fetch("/api/uploads/multi-angle", {
            method: "POST",
            body: formData
        });

        const data = await res.json();
        submitBtn.disabled = false;
        submitBtn.innerHTML = `<i class="bi bi-cloud-arrow-up-fill"></i> Upload & Process Bus Angles`;

        if (res.ok && data.success) {
            latestMultiAngleResults = data;
            renderConsolidatedSummary(data);
            showToast(`Multi-angle analysis completed! Processed ${selectedKeys.length} angles.`, "success");
        } else {
            showToast(data.error || "Failed to process multi-angle upload.", "error");
        }
    } catch (err) {
        submitBtn.disabled = false;
        submitBtn.innerHTML = `<i class="bi bi-cloud-arrow-up-fill"></i> Upload & Process Bus Angles`;
        showToast("Network error uploading multi-angle photos.", "error");
        console.error(err);
    }
}

// ----------------------------------------------------------------------------
// Render Consolidated Multi-Angle Results & HUD
// ----------------------------------------------------------------------------

function renderConsolidatedSummary(data) {
    const summaryCard = document.getElementById("multi-angle-summary-card");
    const resultsCard = document.getElementById("upload-results-card");
    if (summaryCard) summaryCard.style.display = "block";
    if (resultsCard) resultsCard.style.display = "block";

    const counts = data.consolidated_counts || {};
    const summaries = data.angle_summaries || {};

    // Fill consolidated stat cards
    document.getElementById("stat-angle-a-count").textContent = counts.angle_a_count || 0;
    document.getElementById("stat-angle-b-count").textContent = counts.angle_b_count || 0;
    document.getElementById("stat-angle-c-count").textContent = counts.angle_c_count || 0;

    const sumA = summaries["A"] || {};
    const sumB = summaries["B"] || {};
    const sumC = summaries["C"] || {};

    document.getElementById("stat-angle-a-sub").textContent = `${sumA.high_confidence || 0} high, ${sumA.needs_review || 0} review`;
    document.getElementById("stat-angle-b-sub").textContent = `${sumB.high_confidence || 0} high, ${sumB.needs_review || 0} review`;
    document.getElementById("stat-angle-c-sub").textContent = `${sumC.high_confidence || 0} high, ${sumC.needs_review || 0} review`;

    document.getElementById("stat-total-raw").textContent = counts.total_raw_detections || 0;
    document.getElementById("stat-potential-dups").textContent = counts.potential_duplicates || 0;
    document.getElementById("stat-confirmed-unique").textContent = counts.confirmed_unique || 0;
    document.getElementById("stat-needs-review").textContent = counts.passengers_requiring_review || 0;
    document.getElementById("stat-provisional-unique").textContent = counts.provisional_unique_count || 0;

    const provBadge = document.getElementById("summary-provisional-badge");
    if (provBadge) {
        if (counts.is_provisional) {
            provBadge.textContent = "Provisional Count (Duplicate/Unidentified Review Pending)";
            provBadge.className = "badge-status badge-possibly-absent";
        } else {
            provBadge.textContent = "Confirmed Unique Count";
            provBadge.className = "badge-status badge-present";
        }
    }

    // Update each angle card badge & metrics
    ["A", "B", "C"].forEach(angle => {
        const lower = angle.toLowerCase();
        const aSum = summaries[angle];
        const statusBadge = document.getElementById(`status-badge-${lower}`);
        const pill = document.getElementById(`metrics-pill-${lower}`);
        const errBox = document.getElementById(`error-box-${lower}`);
        const detCountEl = document.getElementById(`count-detected-${lower}`);
        const uncCountEl = document.getElementById(`count-uncertain-${lower}`);

        if (aSum) {
            if (aSum.status === "processed") {
                if (statusBadge) {
                    statusBadge.textContent = "Processed";
                    statusBadge.className = "badge-status badge-present";
                }
                if (pill) pill.style.display = "flex";
                if (detCountEl) detCountEl.textContent = aSum.detected;
                if (uncCountEl) uncCountEl.textContent = aSum.uncertain;
                if (errBox) errBox.style.display = "none";
            } else if (aSum.status === "failed") {
                if (statusBadge) {
                    statusBadge.textContent = "Failed";
                    statusBadge.className = "badge-status badge-needs-review";
                }
                if (errBox) {
                    errBox.textContent = aSum.error || "Processing failed";
                    errBox.style.display = "block";
                }
            }
        }
    });

    // Default to first successful angle tab
    const firstSuccessful = Object.keys(summaries).find(k => summaries[k].status === "processed") || "A";
    switchHudAngleTab(firstSuccessful);
    summaryCard.scrollIntoView({ behavior: "smooth" });
}

// ----------------------------------------------------------------------------
// Interactive Manual Bounding Box Correction & Annotation Engine
// ----------------------------------------------------------------------------

let currentAnnotationTool = "draw"; // "draw" or "select"
let manualBoxesStore = { "A": [], "B": [], "C": [] };
let selectedManualBoxIndex = -1;
let manualBoxSeq = 1;
let activeDragAction = null; // "create" | "move" | "resize"
let activeResizeHandle = null;
let dragStartX = 0;
let dragStartY = 0;
let transientBox = null;
let currentUploadIds = { "A": null, "B": null, "C": null };

function setAnnotationTool(tool) {
    currentAnnotationTool = tool;
    const btnDraw = document.getElementById("btn-tool-draw");
    const btnSelect = document.getElementById("btn-tool-select");
    const modeBadge = document.getElementById("canvas-mode-badge");
    const hintText = document.getElementById("annotation-hint-text");
    const canvas = document.getElementById("annotation-canvas");

    if (btnDraw) btnDraw.className = (tool === "draw") ? "btn-primary" : "btn-secondary";
    if (btnSelect) btnSelect.className = (tool === "select") ? "btn-primary" : "btn-secondary";

    if (modeBadge) {
        modeBadge.textContent = tool === "draw" ? "Mode: Draw Box" : "Mode: Select / Edit";
        modeBadge.style.background = tool === "draw" ? "#cffafe" : "#e0e7ff";
        modeBadge.style.color = tool === "draw" ? "#0891b2" : "#4338ca";
    }

    if (hintText) {
        hintText.textContent = tool === "draw" 
            ? "Click and drag over missed passengers to draw a new bounding box."
            : "Click a box to select. Drag center to move, or drag corner handles to resize.";
    }

    if (canvas) {
        canvas.style.cursor = (tool === "draw") ? "crosshair" : "default";
    }
    renderAnnotationCanvas();
}

function initAnnotationCanvas(hudImg, summary) {
    const canvas = document.getElementById("annotation-canvas");
    const toolbar = document.getElementById("annotation-toolbar");
    if (!canvas || !hudImg) return;

    if (summary && summary.upload_id) {
        currentUploadIds[currentActiveHudAngle] = summary.upload_id;
    }

    if (toolbar) toolbar.style.display = "flex";

    // Set internal canvas resolution to natural image resolution
    canvas.width = hudImg.naturalWidth || hudImg.clientWidth || 800;
    canvas.height = hudImg.naturalHeight || hudImg.clientHeight || 600;
    canvas.style.display = "block";

    // Attach pointer listeners once
    if (!canvas._listenersAttached) {
        canvas._listenersAttached = true;
        canvas.addEventListener("pointerdown", handleCanvasPointerDown);
        canvas.addEventListener("pointermove", handleCanvasPointerMove);
        canvas.addEventListener("pointerup", handleCanvasPointerUp);
        canvas.addEventListener("pointercancel", () => {
            activeDragAction = null;
            transientBox = null;
            renderAnnotationCanvas();
        });
        window.addEventListener("keydown", (e) => {
            if ((e.key === "Delete" || e.key === "Backspace") && selectedManualBoxIndex >= 0) {
                // Only if not focused on an input element
                if (document.activeElement && (document.activeElement.tagName === "INPUT" || document.activeElement.tagName === "TEXTAREA")) return;
                deleteSelectedBox();
            }
        });
    }

    setAnnotationTool(currentAnnotationTool);
    renderAnnotationCanvas();
}

function getCanvasImageCoords(e, canvas) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    return {
        x: Math.round((e.clientX - rect.left) * scaleX),
        y: Math.round((e.clientY - rect.top) * scaleY)
    };
}

function handleCanvasPointerDown(e) {
    const canvas = document.getElementById("annotation-canvas");
    if (!canvas) return;
    const { x, y } = getCanvasImageCoords(e, canvas);
    const boxes = manualBoxesStore[currentActiveHudAngle] || [];

    if (currentAnnotationTool === "draw") {
        activeDragAction = "create";
        dragStartX = x;
        dragStartY = y;
        transientBox = { x, y, w: 0, h: 0 };
    } else {
        // Select / Edit Mode
        // 1. Check if clicked a resize handle of currently selected box
        if (selectedManualBoxIndex >= 0 && selectedManualBoxIndex < boxes.length) {
            const selBox = boxes[selectedManualBoxIndex];
            const handle = hitTestHandles(x, y, selBox);
            if (handle) {
                activeDragAction = "resize";
                activeResizeHandle = handle;
                dragStartX = x;
                dragStartY = y;
                return;
            }
        }

        // 2. Check if clicked inside any manual box
        let foundIdx = -1;
        for (let i = boxes.length - 1; i >= 0; i--) {
            const b = boxes[i];
            if (x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h) {
                foundIdx = i;
                break;
            }
        }

        if (foundIdx >= 0) {
            selectedManualBoxIndex = foundIdx;
            activeDragAction = "move";
            dragStartX = x;
            dragStartY = y;
            canvas.style.cursor = "move";
            highlightTableRow(boxes[foundIdx].label);
        } else {
            selectedManualBoxIndex = -1;
            activeDragAction = null;
        }
    }
    renderAnnotationCanvas();
}

function handleCanvasPointerMove(e) {
    const canvas = document.getElementById("annotation-canvas");
    if (!canvas) return;
    const { x, y } = getCanvasImageCoords(e, canvas);
    const boxes = manualBoxesStore[currentActiveHudAngle] || [];

    if (activeDragAction === "create" && transientBox) {
        transientBox.w = x - dragStartX;
        transientBox.h = y - dragStartY;
        renderAnnotationCanvas();
    } else if (activeDragAction === "move" && selectedManualBoxIndex >= 0) {
        const selBox = boxes[selectedManualBoxIndex];
        const dx = x - dragStartX;
        const dy = y - dragStartY;
        selBox.x = Math.max(0, Math.min(canvas.width - selBox.w, selBox.x + dx));
        selBox.y = Math.max(0, Math.min(canvas.height - selBox.h, selBox.y + dy));
        dragStartX = x;
        dragStartY = y;
        renderAnnotationCanvas();
    } else if (activeDragAction === "resize" && selectedManualBoxIndex >= 0 && activeResizeHandle) {
        resizeBoxWithHandle(boxes[selectedManualBoxIndex], activeResizeHandle, x, y, canvas);
        renderAnnotationCanvas();
    } else if (currentAnnotationTool === "select") {
        // Update cursor on hover over handles or boxes
        if (selectedManualBoxIndex >= 0 && selectedManualBoxIndex < boxes.length) {
            const handle = hitTestHandles(x, y, boxes[selectedManualBoxIndex]);
            if (handle) {
                canvas.style.cursor = getHandleCursor(handle);
                return;
            }
        }
        let overBox = false;
        for (let b of boxes) {
            if (x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h) {
                overBox = true;
                break;
            }
        }
        canvas.style.cursor = overBox ? "move" : "default";
    }
}

function handleCanvasPointerUp(e) {
    const canvas = document.getElementById("annotation-canvas");
    if (!canvas) return;
    const hudImg = document.getElementById("annotated-hud-image");
    const boxes = manualBoxesStore[currentActiveHudAngle] || [];

    if (activeDragAction === "create" && transientBox) {
        let x = Math.min(transientBox.x, transientBox.x + transientBox.w);
        let y = Math.min(transientBox.y, transientBox.y + transientBox.h);
        let w = Math.abs(transientBox.w);
        let h = Math.abs(transientBox.h);

        // Require minimum meaningful bounding box
        if (w >= 15 && h >= 15 && hudImg) {
            const label = `P_MANUAL_${manualBoxSeq++}`;
            const cropDataUrl = cropImageToDataUrl(hudImg, x, y, w, h);
            const newBox = {
                id: "man_" + Date.now(),
                label: label,
                x: Math.round(x),
                y: Math.round(y),
                w: Math.round(w),
                h: Math.round(h),
                cropDataUrl: cropDataUrl,
                isSaved: false,
                angle: currentActiveHudAngle
            };
            boxes.push(newBox);
            manualBoxesStore[currentActiveHudAngle] = boxes;
            selectedManualBoxIndex = boxes.length - 1;
            addManualBoxToTable(newBox, currentActiveHudAngle);
            showToast(`Added manual box for ${label}. Click "Save Correction" to persist.`, "info");
        }
    } else if ((activeDragAction === "move" || activeDragAction === "resize") && selectedManualBoxIndex >= 0 && hudImg) {
        // Re-crop thumbnail after move/resize
        const selBox = boxes[selectedManualBoxIndex];
        selBox.cropDataUrl = cropImageToDataUrl(hudImg, selBox.x, selBox.y, selBox.w, selBox.h);
        updateTableThumbnail(selBox.label, selBox.cropDataUrl);
    }

    activeDragAction = null;
    activeResizeHandle = null;
    transientBox = null;
    renderAnnotationCanvas();
}

function cropImageToDataUrl(img, x, y, w, h) {
    const offCanvas = document.createElement("canvas");
    offCanvas.width = Math.max(30, w);
    offCanvas.height = Math.max(30, h);
    const ctx = offCanvas.getContext("2d");
    try {
        ctx.drawImage(img, x, y, w, h, 0, 0, offCanvas.width, offCanvas.height);
        return offCanvas.toDataURL("image/jpeg", 0.9);
    } catch (e) {
        return "";
    }
}

function hitTestHandles(x, y, box) {
    const handleSize = 14;
    const handles = {
        tl: { x: box.x, y: box.y },
        tr: { x: box.x + box.w, y: box.y },
        bl: { x: box.x, y: box.y + box.h },
        br: { x: box.x + box.w, y: box.y + box.h },
        t:  { x: box.x + box.w / 2, y: box.y },
        b:  { x: box.x + box.w / 2, y: box.y + box.h },
        l:  { x: box.x, y: box.y + box.h / 2 },
        r:  { x: box.x + box.w, y: box.y + box.h / 2 }
    };

    for (let [k, pos] of Object.entries(handles)) {
        if (Math.abs(x - pos.x) <= handleSize && Math.abs(y - pos.y) <= handleSize) {
            return k;
        }
    }
    return null;
}

function getHandleCursor(handle) {
    if (handle === "tl" || handle === "br") return "nwse-resize";
    if (handle === "tr" || handle === "bl") return "nesw-resize";
    if (handle === "t" || handle === "b") return "ns-resize";
    return "ew-resize";
}

function resizeBoxWithHandle(box, handle, x, y, canvas) {
    const minSize = 15;
    if (handle.includes("r")) {
        box.w = Math.max(minSize, Math.min(canvas.width - box.x, x - box.x));
    }
    if (handle.includes("b")) {
        box.h = Math.max(minSize, Math.min(canvas.height - box.y, y - box.y));
    }
    if (handle.includes("l")) {
        const right = box.x + box.w;
        const newX = Math.max(0, Math.min(right - minSize, x));
        box.w = right - newX;
        box.x = newX;
    }
    if (handle.includes("t")) {
        const bottom = box.y + box.h;
        const newY = Math.max(0, Math.min(bottom - minSize, y));
        box.h = bottom - newY;
        box.y = newY;
    }
}

function renderAnnotationCanvas() {
    const canvas = document.getElementById("annotation-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    const boxes = manualBoxesStore[currentActiveHudAngle] || [];

    // Draw existing manual boxes
    boxes.forEach((b, idx) => {
        const isSelected = (idx === selectedManualBoxIndex);

        // Bounding box stroke & fill
        ctx.save();
        ctx.lineWidth = isSelected ? 3 : 2;
        ctx.strokeStyle = isSelected ? "#06b6d4" : "#10b981";
        ctx.fillStyle = isSelected ? "rgba(6, 182, 212, 0.2)" : "rgba(16, 185, 129, 0.15)";
        ctx.setLineDash(isSelected ? [6, 3] : []);
        ctx.fillRect(b.x, b.y, b.w, b.h);
        ctx.strokeRect(b.x, b.y, b.w, b.h);
        ctx.restore();

        // Label pill on top
        ctx.save();
        ctx.font = "bold 13px 'Segoe UI', system-ui, sans-serif";
        const labelText = `${b.label} (Manual)`;
        const textWidth = ctx.measureText(labelText).width;
        const pillHeight = 22;
        const pillY = Math.max(0, b.y - pillHeight - 2);

        ctx.fillStyle = isSelected ? "#06b6d4" : "#10b981";
        ctx.beginPath();
        ctx.roundRect(b.x, pillY, textWidth + 14, pillHeight, [4, 4, 0, 0]);
        ctx.fill();

        ctx.fillStyle = "#ffffff";
        ctx.fillText(labelText, b.x + 7, pillY + 15);
        ctx.restore();

        // If selected in select mode: draw 8 handles
        if (isSelected && currentAnnotationTool === "select") {
            drawResizeHandles(ctx, b);
        }
    });

    // Draw active transient rubberband box
    if (transientBox) {
        ctx.save();
        ctx.lineWidth = 2;
        ctx.strokeStyle = "#38bdf8";
        ctx.fillStyle = "rgba(56, 189, 248, 0.25)";
        ctx.setLineDash([5, 3]);
        let x = Math.min(transientBox.x, transientBox.x + transientBox.w);
        let y = Math.min(transientBox.y, transientBox.y + transientBox.h);
        let w = Math.abs(transientBox.w);
        let h = Math.abs(transientBox.h);
        ctx.fillRect(x, y, w, h);
        ctx.strokeRect(x, y, w, h);
        ctx.restore();
    }
}

function drawResizeHandles(ctx, box) {
    const sz = 8;
    const half = sz / 2;
    const handles = [
        { x: box.x, y: box.y },
        { x: box.x + box.w, y: box.y },
        { x: box.x, y: box.y + box.h },
        { x: box.x + box.w, y: box.y + box.h },
        { x: box.x + box.w / 2, y: box.y },
        { x: box.x + box.w / 2, y: box.y + box.h },
        { x: box.x, y: box.y + box.h / 2 },
        { x: box.x + box.w, y: box.y + box.h / 2 }
    ];

    ctx.save();
    ctx.fillStyle = "#ffffff";
    ctx.strokeStyle = "#0891b2";
    ctx.lineWidth = 2;
    handles.forEach(h => {
        ctx.beginPath();
        ctx.arc(h.x, h.y, half + 1, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
    });
    ctx.restore();
}

function addManualBoxToTable(box, angle) {
    const tbody = document.getElementById("detected-passengers-tbody");
    if (!tbody) return;

    // Remove empty placeholder row if present
    const emptyRow = tbody.querySelector("td[colspan='5']");
    if (emptyRow) {
        tbody.innerHTML = "";
    }

    const tr = document.createElement("tr");
    tr.id = `row-manual-${box.label}`;
    tr.style.background = "rgba(6, 182, 212, 0.08)";
    tr.style.borderLeft = "3px solid #06b6d4";

    tr.innerHTML = `
        <td>
            ${box.cropDataUrl ? `<img src="${box.cropDataUrl}" class="passenger-thumb" id="thumb-${box.label}" alt="${box.label}" style="border:1px solid #06b6d4;">` : `<div class="passenger-thumb"><i class="bi bi-person"></i></div>`}
        </td>
        <td>
            <strong>${box.label}</strong>
            <span style="display:block;font-size:10px;color:#0891b2;">Canvas Box</span>
        </td>
        <td>
            <span class="badge-conf-high" style="background:#cffafe;color:#0891b2;">Manual (100%)</span>
        </td>
        <td>
            <span class="badge-status badge-present" id="status-${box.label}">User-Added</span>
        </td>
        <td>
            <div style="display:flex;gap:4px;">
                <button type="button" class="btn-secondary" style="padding:4px 8px;font-size:11px;" onclick="selectBoxByLabel('${box.label}')" title="Select & Edit on Canvas">
                    <i class="bi bi-cursor-fill"></i> Select
                </button>
                <button type="button" class="btn-secondary" style="padding:4px 8px;font-size:11px;color:#ef4444;" onclick="deleteManualBoxByLabel('${box.label}')" title="Delete Box">
                    <i class="bi bi-trash"></i>
                </button>
            </div>
        </td>
    `;
    tbody.appendChild(tr);
}

function updateTableThumbnail(label, cropDataUrl) {
    const img = document.getElementById(`thumb-${label}`);
    if (img && cropDataUrl) {
        img.src = cropDataUrl;
    }
}

function highlightTableRow(label) {
    const row = document.getElementById(`row-manual-${label}`);
    if (row) {
        row.scrollIntoView({ behavior: "smooth", block: "nearest" });
        row.style.outline = "2px solid #06b6d4";
        setTimeout(() => { row.style.outline = "none"; }, 1500);
    }
}

function selectBoxByLabel(label) {
    const boxes = manualBoxesStore[currentActiveHudAngle] || [];
    const idx = boxes.findIndex(b => b.label === label);
    if (idx >= 0) {
        setAnnotationTool("select");
        selectedManualBoxIndex = idx;
        renderAnnotationCanvas();
        highlightTableRow(label);
    }
}

function deleteManualBoxByLabel(label) {
    const boxes = manualBoxesStore[currentActiveHudAngle] || [];
    const idx = boxes.findIndex(b => b.label === label);
    if (idx >= 0) {
        selectedManualBoxIndex = idx;
        deleteSelectedBox();
    }
}

async function deleteSelectedBox() {
    const boxes = manualBoxesStore[currentActiveHudAngle] || [];
    if (selectedManualBoxIndex < 0 || selectedManualBoxIndex >= boxes.length) {
        showToast("Please select a bounding box to delete.", "warning");
        return;
    }

    const box = boxes[selectedManualBoxIndex];
    if (box.obs_id) {
        // Was already saved to database -> call DELETE endpoint
        try {
            const res = await fetch(`/api/uploads/observations/${box.obs_id}`, { method: "DELETE" });
            const data = await res.json();
            if (res.ok && data.success) {
                showToast(data.message || `Deleted ${box.label}.`, "success");
            }
        } catch (e) {
            console.error("Failed to delete observation on server:", e);
        }
    }

    // Remove row from table
    const row = document.getElementById(`row-manual-${box.label}`);
    if (row) row.remove();

    boxes.splice(selectedManualBoxIndex, 1);
    manualBoxesStore[currentActiveHudAngle] = boxes;
    selectedManualBoxIndex = -1;
    renderAnnotationCanvas();
    showToast(`Removed box ${box.label}.`, "info");
}

function clearUnsavedManualBoxes() {
    const boxes = manualBoxesStore[currentActiveHudAngle] || [];
    const saved = boxes.filter(b => b.isSaved);
    const unsaved = boxes.filter(b => !b.isSaved);

    unsaved.forEach(b => {
        const row = document.getElementById(`row-manual-${b.label}`);
        if (row) row.remove();
    });

    manualBoxesStore[currentActiveHudAngle] = saved;
    selectedManualBoxIndex = -1;
    renderAnnotationCanvas();
    showToast("Cleared unsaved manual boxes.", "info");
}

async function saveManualCorrections() {
    const boxes = manualBoxesStore[currentActiveHudAngle] || [];
    const unsaved = boxes.filter(b => !b.isSaved);

    if (unsaved.length === 0) {
        showToast("No new manual corrections to save for this angle.", "info");
        return;
    }

    let uploadId = currentUploadIds[currentActiveHudAngle];
    if (!uploadId && latestMultiAngleResults && latestMultiAngleResults.angle_summaries) {
        const sum = latestMultiAngleResults.angle_summaries[currentActiveHudAngle];
        if (sum && sum.upload_id) uploadId = sum.upload_id;
    }

    if (!uploadId) {
        showToast("Upload ID not found for Angle " + currentActiveHudAngle + ". Please upload photos first.", "error");
        return;
    }

    const saveBtn = document.getElementById("btn-tool-save");
    if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.innerHTML = `<i class="bi bi-cpu"></i> Saving...`;
    }

    const payload = {
        upload_id: uploadId,
        camera_angle: currentActiveHudAngle,
        annotations: unsaved.map(b => ({
            label: b.label,
            bbox: [b.x, b.y, b.x + b.w, b.y + b.h],
            crop_data: b.cropDataUrl
        }))
    };

    try {
        const res = await fetch("/api/uploads/manual-annotation", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });

        const data = await res.json();
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.innerHTML = `<i class="bi bi-check-circle-fill"></i> Save Correction`;
        }

        if (res.ok && data.success) {
            // Mark boxes as saved and store returned observation IDs
            const created = data.created_observations || [];
            created.forEach(obs => {
                const match = boxes.find(b => b.label === obs.temp_detection_id);
                if (match) {
                    match.isSaved = true;
                    match.obs_id = obs.id;
                    const stBadge = document.getElementById(`status-${match.label}`);
                    if (stBadge) {
                        stBadge.textContent = "Saved";
                        stBadge.style.background = "#d1fae5";
                        stBadge.style.color = "#065f46";
                    }
                }
            });

            // Update Angle count pill and consolidated summary
            const pillCount = document.getElementById(`count-detected-${currentActiveHudAngle.toLowerCase()}`);
            if (pillCount && data.total_passengers_detected !== undefined) {
                pillCount.textContent = data.total_passengers_detected;
            }

            const rawEl = document.getElementById("stat-total-raw");
            if (rawEl) {
                const cur = parseInt(rawEl.textContent || "0");
                rawEl.textContent = cur + created.length;
            }

            renderAnnotationCanvas();
            showToast(`Saved ${created.length} manual passenger correction(s)!`, "success");
        } else {
            showToast(data.error || "Failed to save manual annotations.", "error");
        }
    } catch (err) {
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.innerHTML = `<i class="bi bi-check-circle-fill"></i> Save Correction`;
        }
        showToast("Network error saving manual annotations.", "error");
    }
}

function switchHudAngleTab(angle) {
    currentActiveHudAngle = angle;
    ["A", "B", "C"].forEach(a => {
        const btn = document.getElementById(`hud-tab-${a.toLowerCase()}`);
        if (btn) {
            btn.className = (a === angle) ? "btn-primary" : "btn-secondary";
        }
    });

    const angleNameEl = document.getElementById("active-table-angle-name");
    const badgeEl = document.getElementById("active-hud-badge");
    const titleEl = document.getElementById("active-hud-title");
    if (angleNameEl) angleNameEl.textContent = `Angle ${angle}`;
    if (badgeEl) badgeEl.textContent = `Angle ${angle}`;

    if (!latestMultiAngleResults || !latestMultiAngleResults.angle_summaries) return;
    const summary = latestMultiAngleResults.angle_summaries[angle];

    const hudImg = document.getElementById("annotated-hud-image");
    const placeholder = document.getElementById("hud-placeholder");
    const tbody = document.getElementById("detected-passengers-tbody");
    const canvas = document.getElementById("annotation-canvas");
    const toolbar = document.getElementById("annotation-toolbar");

    if (!summary || summary.status !== "processed") {
        if (hudImg) hudImg.style.display = "none";
        if (canvas) canvas.style.display = "none";
        if (toolbar) toolbar.style.display = "none";
        if (placeholder) {
            placeholder.style.display = "block";
            placeholder.innerHTML = `<i class="bi bi-exclamation-circle" style="font-size:32px;display:block;margin-bottom:8px;color:#ef4444;"></i> Angle ${angle} was not submitted or failed processing.`;
        }
        if (tbody) tbody.innerHTML = `<tr><td colspan="5" class="text-center py-4" style="color:var(--text-muted)">No detections for Angle ${angle}.</td></tr>`;
        return;
    }

    if (hudImg && summary.annotated_url) {
        hudImg.src = summary.annotated_url;
        hudImg.style.display = "block";
        if (placeholder) placeholder.style.display = "none";

        // Initialize annotation canvas when image is loaded
        if (hudImg.complete) {
            initAnnotationCanvas(hudImg, summary);
        } else {
            hudImg.onload = () => initAnnotationCanvas(hudImg, summary);
        }
    }

    if (titleEl) titleEl.textContent = `Annotated Passenger Detections (${summary.title || `Angle ${angle}`}):`;

    // Render detections table
    tbody.innerHTML = "";
    const detections = summary.detections || [];
    if (detections.length === 0 && (!manualBoxesStore[angle] || manualBoxesStore[angle].length === 0)) {
        tbody.innerHTML = `<tr><td colspan="5" class="text-center py-4" style="color:var(--text-muted)">No visible passengers detected in Angle ${angle}.</td></tr>`;
    } else {
        detections.forEach(det => {
            const tr = document.createElement("tr");

            let confBadge = `<span class="badge-conf-high">High (${Math.round((det.similarity_score || det.detection_confidence || 0) * 100)}%)</span>`;
            if (det.confidence_label === "Medium") {
                confBadge = `<span class="badge-conf-med">Medium (${Math.round((det.similarity_score || det.detection_confidence || 0) * 100)}%)</span>`;
            } else if (det.confidence_label === "Needs Review" || det.confidence_label === "Low") {
                confBadge = `<span class="badge-conf-review">${det.confidence_label} (${Math.round((det.detection_confidence || 0) * 100)}%)</span>`;
            }

            let statusBadge = `<span class="badge-status badge-present">Confirmed</span>`;
            if (det.is_potential_duplicate) {
                statusBadge = `<span class="badge-status badge-possibly-absent" title="Potential cross-angle duplicate">Possible Dup</span>`;
            } else if (det.review_status === "NEEDS_REVIEW") {
                statusBadge = `<span class="badge-status badge-needs-review">Review Req</span>`;
            } else if (det.review_status === "FALSE_POSITIVE") {
                statusBadge = `<span class="badge-status" style="background:#fee2e2;color:#b91c1c;">False Positive</span>`;
            }

            const idText = det.person_id && det.person_id !== "Unidentified" ? det.person_id : (det.temp_id || "Unidentified");

            tr.innerHTML = `
                <td>
                    ${det.crop_path ? `<img src="/data/${det.crop_path}" class="passenger-thumb" alt="${idText}">` : `<div class="passenger-thumb" style="display:flex;align-items:center;justify-content:center;"><i class="bi bi-person"></i></div>`}
                </td>
                <td><strong>${idText}</strong></td>
                <td>${confBadge}</td>
                <td>${statusBadge}</td>
                <td>
                    <button type="button" class="btn-secondary" style="padding:4px 8px;font-size:11px;" onclick="openDetectionReviewModal('${idText}', '${det.crop_path || ''}', '${det.review_status || 'CONFIRMED'}')">
                        <i class="bi bi-pencil-square"></i> Review
                    </button>
                </td>
            `;
            tbody.appendChild(tr);
        });

        // Also append any manual boxes for this angle
        (manualBoxesStore[angle] || []).forEach(box => {
            addManualBoxToTable(box, angle);
        });
    }
}


// ----------------------------------------------------------------------------
// Operator Detection Review Modal & Handlers
// ----------------------------------------------------------------------------

function openDetectionReviewModal(idText, cropPath, status) {
    const modal = document.getElementById("detection-review-modal");
    if (!modal) return;

    document.getElementById("drm-label").textContent = `Detection: ${idText}`;
    document.getElementById("drm-obs-id").value = idText;
    const imgEl = document.getElementById("drm-crop-img");
    if (cropPath) {
        imgEl.src = `/data/${cropPath}`;
        imgEl.style.display = "block";
    } else {
        imgEl.style.display = "none";
    }

    const badgeEl = document.getElementById("drm-status-badge");
    badgeEl.textContent = status;

    modal.classList.add("active");
    modal.style.display = "flex";
}

function closeDetectionReviewModal() {
    const modal = document.getElementById("detection-review-modal");
    if (modal) {
        modal.classList.remove("active");
        modal.style.display = "none";
    }
}

function toggleLinkPassengerField(action) {
    const cont = document.getElementById("drm-link-container");
    if (cont) {
        cont.style.display = (action === "LINK_PASSENGER") ? "block" : "none";
    }
}

async function handleDetectionReviewSubmit(e) {
    e.preventDefault();
    const idText = document.getElementById("drm-obs-id").value;
    const action = document.getElementById("drm-action-select").value;
    const targetId = document.getElementById("drm-target-id").value.trim();
    const comment = document.getElementById("drm-comment").value.trim();

    showToast(`Correction recorded: Marked ${idText} as ${action}.`, "success");
    closeDetectionReviewModal();

    // Refresh HUD table display if observation in active view
    if (latestMultiAngleResults && latestMultiAngleResults.angle_summaries) {
        const sum = latestMultiAngleResults.angle_summaries[currentActiveHudAngle];
        if (sum && sum.detections) {
            const det = sum.detections.find(d => d.person_id === idText || d.temp_id === idText);
            if (det) {
                det.review_status = action === "CONFIRM" ? "CONFIRMED" : (action === "FALSE_POSITIVE" ? "FALSE_POSITIVE" : "NEEDS_REVIEW");
                if (action === "LINK_PASSENGER" && targetId) det.person_id = targetId;
                det.is_potential_duplicate = false;
                switchHudAngleTab(currentActiveHudAngle);
            }
        }
    }
}

// ----------------------------------------------------------------------------
// Quick Start Journey & Modal
// ----------------------------------------------------------------------------

async function quickStartActiveJourney() {
    showToast("Launching active journey for bus...", "info");
    try {
        let busId = null;
        let origin = "Terminal";
        let destination = "Central Depot";

        try {
            const bRes = await fetch("/api/buses/operator/my-buses");
            if (bRes.ok) {
                const bData = await bRes.json();
                if (bData.buses && bData.buses.length > 0) {
                    const b = bData.buses.find(x => x.id === bData.current_bus_id) || bData.buses[0];
                    busId = b.id;
                    if (b.starting_point) origin = b.starting_point;
                    if (b.destination) destination = b.destination;
                }
            }
        } catch (_) {}

        const res = await fetch("/api/journeys", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ bus_id: busId, origin: origin, destination: destination })
        });
        const data = await res.json();
        if (res.ok && data.journey) {
            showToast(`Launched Journey ${data.journey.journey_code}!`, "success");
            await loadActiveJourneys();
            const select = document.getElementById("journey-select");
            if (select) select.value = data.journey.id;
            return data.journey.id;
        } else {
            showToast(data.error || "Failed to start journey.", "error");
            return null;
        }
    } catch (e) {
        showToast("Error launching journey.", "error");
        return null;
    }
}

async function openQuickNewJourneyModal() {
    const modal = document.getElementById("quick-journey-modal");
    if (!modal) return;
    try {
        const res = await fetch("/api/buses");
        if (res.ok) {
            const buses = await res.json();
            const sel = document.getElementById("qj-bus-select");
            if (sel) {
                sel.innerHTML = buses.map(b => `<option value="${b.id}">${b.registration_number || b.bus_number} - ${b.bus_name || 'Bus'} (${b.route_name || 'Route'})</option>`).join("");
            }
        }
    } catch (_) {}
    modal.classList.add("active");
    modal.style.display = "flex";
}

function closeQuickNewJourneyModal() {
    const modal = document.getElementById("quick-journey-modal");
    if (modal) {
        modal.classList.remove("active");
        modal.style.display = "none";
    }
}

async function handleQuickJourneySubmit(e) {
    e.preventDefault();
    const busId = document.getElementById("qj-bus-select").value;
    const origin = document.getElementById("qj-origin").value.trim();
    const destination = document.getElementById("qj-destination").value.trim();

    try {
        const res = await fetch("/api/journeys", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ bus_id: busId, origin: origin, destination: destination })
        });
        const data = await res.json();
        if (res.ok && data.journey) {
            showToast(`Launched & selected Journey ${data.journey.journey_code}!`, "success");
            closeQuickNewJourneyModal();
            await loadActiveJourneys();
            const sel = document.getElementById("journey-select");
            if (sel) sel.value = data.journey.id;
        } else {
            showToast(data.error || "Failed to create journey.", "error");
        }
    } catch (err) {
        showToast("Error creating journey.", "error");
    }
}

function downloadAnalysisReport(format) {
    const sel = document.getElementById("journey-select");
    const jId = (sel && sel.value) ? sel.value : "";
    
    let url = "";
    if (jId) {
        url = `/api/reports/journey/${jId}/${format}`;
    } else {
        url = `/api/reports/export.${format}`;
    }
    
    window.open(url, "_blank");
}


// ============================================================================
// AUTOMATED MULTI-ANGLE LOCAL FOLDER & TIMETABLE PROCESSING
// ============================================================================

const selectedFolderPaths = { A: null, B: null, C: null };
let currentTimetableId = null;
let currentTimetableFile = null;
let currentJobId = null;
let jobPollInterval = null;
let currentStopsList = [];
let activeStopNavIndex = 0;

// Native/Local Folder Selection
async function selectFolderDialog(angle) {
    const lower = angle.toLowerCase();
    const errBox = document.getElementById(`folder-error-${lower}`);
    if (errBox) errBox.style.display = "none";

    try {
        // Try backend native Windows folder picker
        const res = await fetch("/api/automated/pick-native-folder", { method: "POST" });
        if (res.ok) {
            const data = await res.json();
            if (data.success && data.folder_path) {
                selectedFolderPaths[angle] = data.folder_path;
                await scanFolder(angle, data.folder_path);
                return;
            } else if (data.cancelled) {
                return;
            }
        }
    } catch (e) {
        console.warn("Native folder picker unavailable:", e);
    }

    // Fallback: prompt operator for local directory path
    const pathInput = prompt(`Enter local Windows directory path for Angle ${angle} clips (e.g. D:\\SmartBus\\Journey01\\Angle_${angle}):`, selectedFolderPaths[angle] || "");
    if (pathInput && pathInput.trim()) {
        selectedFolderPaths[angle] = pathInput.trim();
        await scanFolder(angle, pathInput.trim());
    }
}

async function scanFolder(angle, folderPath) {
    const lower = angle.toLowerCase();
    const statusBadge = document.getElementById(`folder-status-${lower}`);
    const pathDisplay = document.getElementById(`folder-path-display-${lower}`);
    const clipsCountEl = document.getElementById(`folder-clips-count-${lower}`);
    const imagesCountEl = document.getElementById(`folder-images-count-${lower}`);
    const errBox = document.getElementById(`folder-error-${lower}`);

    if (statusBadge) {
        statusBadge.textContent = "Scanning...";
        statusBadge.className = "badge-status badge-possibly-absent";
    }

    const journeySelect = document.getElementById("journey-select");
    const journeyId = journeySelect ? journeySelect.value : null;

    try {
        const res = await fetch("/api/automated/scan-folder", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                camera_angle: angle,
                folder_path: folderPath,
                journey_id: (journeyId && journeyId !== "auto_create") ? parseInt(journeyId) : null
            })
        });

        const data = await res.json();
        if (res.ok && data.success) {
            if (pathDisplay) pathDisplay.innerHTML = `Path: <strong>${data.folder.folder_path}</strong>`;
            if (clipsCountEl) clipsCountEl.textContent = data.clips_count;
            if (imagesCountEl) imagesCountEl.textContent = data.images_count;
            if (statusBadge) {
                statusBadge.textContent = `${data.clips_count} Clips, ${data.images_count} Imgs`;
                statusBadge.className = "badge-status badge-present";
            }
            showToast(`Scanned Angle ${angle} folder: found ${data.clips_count} clips and ${data.images_count} images.`, "success");
        } else {
            if (statusBadge) {
                statusBadge.textContent = "Error";
                statusBadge.className = "badge-status";
                statusBadge.style.background = "#fee2e2";
                statusBadge.style.color = "#b91c1c";
            }
            if (errBox) {
                errBox.textContent = data.error || "Failed to scan folder.";
                errBox.style.display = "block";
            }
        }
    } catch (err) {
        if (statusBadge) statusBadge.textContent = "Error";
        if (errBox) {
            errBox.textContent = "Network error connecting to local folder scanner.";
            errBox.style.display = "block";
        }
    }
}

function rescanFolder(angle) {
    if (selectedFolderPaths[angle]) {
        scanFolder(angle, selectedFolderPaths[angle]);
    } else {
        selectFolderDialog(angle);
    }
}

function clearFolderSelection(angle) {
    selectedFolderPaths[angle] = null;
    const lower = angle.toLowerCase();
    const statusBadge = document.getElementById(`folder-status-${lower}`);
    const pathDisplay = document.getElementById(`folder-path-display-${lower}`);
    const clipsCountEl = document.getElementById(`folder-clips-count-${lower}`);
    const imagesCountEl = document.getElementById(`folder-images-count-${lower}`);
    const errBox = document.getElementById(`folder-error-${lower}`);

    if (statusBadge) {
        statusBadge.textContent = "No Folder";
        statusBadge.className = "badge-status";
        statusBadge.style.background = "#f1f5f9";
        statusBadge.style.color = "#64748b";
    }
    if (pathDisplay) pathDisplay.innerHTML = `Path: <span style="font-style:italic;">None selected</span>`;
    if (clipsCountEl) clipsCountEl.textContent = "0";
    if (imagesCountEl) imagesCountEl.textContent = "0";
    if (errBox) errBox.style.display = "none";
}

// Timetable File Handlers
function handleTimetableFileSelected(file) {
    if (!file) return;
    currentTimetableFile = file;

    const titleEl = document.getElementById("timetable-file-title");
    const metaEl = document.getElementById("timetable-file-meta");
    const removeBtn = document.getElementById("btn-remove-tt");
    const badge = document.getElementById("timetable-readiness-badge");

    if (titleEl) titleEl.textContent = file.name;
    if (metaEl) {
        metaEl.innerHTML = `Size: ${(file.size / 1024).toFixed(1)} KB | Ready to validate`;
        metaEl.style.display = "block";
    }
    if (removeBtn) removeBtn.style.display = "inline-block";
    if (badge) {
        badge.textContent = "File Selected";
        badge.className = "badge-status badge-possibly-absent";
    }

    // Auto-trigger validation for seamless user experience
    uploadAndParseTimetable();
}

function removeTimetableFile() {
    currentTimetableFile = null;
    currentTimetableId = null;
    document.getElementById("timetable-file-input").value = "";
    document.getElementById("timetable-file-title").textContent = "Excel / CSV Timetable";
    document.getElementById("timetable-file-meta").style.display = "none";
    document.getElementById("btn-remove-tt").style.display = "none";
    document.getElementById("btn-match-clips").style.display = "none";
    const badge = document.getElementById("timetable-readiness-badge");
    if (badge) {
        badge.textContent = "No Timetable Uploaded";
        badge.className = "badge-status";
        badge.style.background = "#f1f5f9";
        badge.style.color = "#64748b";
    }
    document.getElementById("stops-table-tbody").innerHTML = `<tr><td colspan="11" class="text-center py-4" style="color:var(--text-muted);">Upload a timetable and scan source clip folders to view stop matches.</td></tr>`;
}

function clearWorkspaceUI() {
    ["a", "b", "c"].forEach(angle => {
        const upper = angle.toUpperCase();
        if (typeof selectedFolderPaths !== "undefined") {
            selectedFolderPaths[upper] = null;
        }
        if (typeof selectedAngleFiles !== "undefined") {
            selectedAngleFiles[upper] = null;
        }

        const pathDisplay = document.getElementById(`folder-path-display-${angle}`);
        if (pathDisplay) pathDisplay.innerHTML = 'Path: <em>No folder selected</em>';

        const clipsCountEl = document.getElementById(`folder-clips-count-${angle}`);
        if (clipsCountEl) clipsCountEl.textContent = "0";

        const imagesCountEl = document.getElementById(`folder-images-count-${angle}`);
        if (imagesCountEl) imagesCountEl.textContent = "0";

        const statusBadge = document.getElementById(`folder-status-${angle}`);
        if (statusBadge) {
            statusBadge.textContent = "No Folder";
            statusBadge.className = "badge-status";
            statusBadge.style.background = "#f1f5f9";
            statusBadge.style.color = "#64748b";
        }

        const errEl = document.getElementById(`folder-error-${angle}`);
        if (errEl) {
            errEl.textContent = "";
            errEl.style.display = "none";
        }
    });

    currentTimetableFile = null;
    currentTimetableId = null;
    currentStopsList = [];

    const ttInput = document.getElementById("timetable-file-input");
    if (ttInput) ttInput.value = "";

    const titleEl = document.getElementById("timetable-file-title");
    if (titleEl) titleEl.textContent = "Excel / CSV Timetable";

    const metaEl = document.getElementById("timetable-file-meta");
    if (metaEl) metaEl.style.display = "none";

    const btnRemoveTT = document.getElementById("btn-remove-tt");
    if (btnRemoveTT) btnRemoveTT.style.display = "none";

    const badge = document.getElementById("timetable-readiness-badge");
    if (badge) {
        badge.textContent = "No Timetable Uploaded";
        badge.className = "badge-status";
        badge.style.background = "#f1f5f9";
        badge.style.color = "#64748b";
    }

    const sDateInput = document.getElementById("tt-service-date");
    if (sDateInput) sDateInput.value = "";

    const btnMatch = document.getElementById("btn-match-clips");
    if (btnMatch) btnMatch.style.display = "none";

    const btnStart = document.getElementById("btn-start-automated-processing");
    if (btnStart) btnStart.disabled = true;

    // Reset stats
    const statIds = [
        "stat-job-total", "stat-job-processed", "stat-job-remaining",
        "stat-job-fully", "stat-job-partially", "stat-job-not", "stat-job-failed",
        "stat-angle-a-count", "stat-angle-b-count", "stat-angle-c-count",
        "stat-total-raw", "stat-potential-dups", "stat-confirmed-unique",
        "stat-needs-review", "stat-provisional-unique"
    ];
    statIds.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = "0";
    });

    const summaryCard = document.getElementById("multi-angle-summary-card");
    if (summaryCard) summaryCard.style.display = "none";

    const tbody = document.getElementById("stops-table-tbody");
    if (tbody) {
        tbody.innerHTML = `<tr><td colspan="11" class="text-center py-4" style="color:var(--text-muted);">Upload a timetable and scan source clip folders to view stop matches.</td></tr>`;
    }
}

async function loadActiveSessionState() {
    try {
        const res = await fetch("/api/automated/active-session-state");
        if (!res.ok) return;
        const data = await res.json();
        if (!data.success) return;

        // Restore folders
        if (data.folders) {
            ["A", "B", "C"].forEach(angle => {
                const f = data.folders[angle];
                if (f) {
                    selectedFolderPaths[angle] = f.folder_path;
                    const lower = angle.toLowerCase();
                    const pathDisplay = document.getElementById(`folder-path-display-${lower}`);
                    const clipsCountEl = document.getElementById(`folder-clips-count-${lower}`);
                    const imagesCountEl = document.getElementById(`folder-images-count-${lower}`);
                    const statusBadge = document.getElementById(`folder-status-${lower}`);

                    if (pathDisplay) pathDisplay.innerHTML = `Path: <strong>${f.folder_path}</strong>`;
                    if (clipsCountEl) clipsCountEl.textContent = f.clips_count || 0;
                    if (imagesCountEl) imagesCountEl.textContent = f.images_count || 0;
                    if (statusBadge) {
                        statusBadge.textContent = `${f.clips_count || 0} Clips, ${f.images_count || 0} Imgs`;
                        statusBadge.className = "badge-status badge-present";
                    }
                }
            });
        }

        // Restore timetable
        if (data.timetable) {
            currentTimetableId = data.timetable.id;
            currentStopsList = data.stops || [];

            const titleEl = document.getElementById("timetable-file-title");
            if (titleEl) titleEl.textContent = data.timetable.filename || "Excel / CSV Timetable";
            const badge = document.getElementById("timetable-readiness-badge");
            if (badge) {
                badge.textContent = `${data.timetable.total_stops} Stops Validated`;
                badge.className = "badge-status badge-present";
            }
            const sDateInput = document.getElementById("tt-service-date");
            if (sDateInput && data.timetable.service_date) {
                sDateInput.value = data.timetable.service_date;
            }
            const btnMatch = document.getElementById("btn-match-clips");
            if (btnMatch) btnMatch.style.display = "inline-block";

            renderStopsTable(currentStopsList);
        }
    } catch (err) {
        console.warn("Failed to load active session state:", err);
    }
}

async function uploadAndParseTimetable() {
    if (!currentTimetableFile) {
        showToast("Please choose an Excel (.xlsx, .xls) or CSV timetable file first.", "error");
        return;
    }

    const journeySelect = document.getElementById("journey-select");
    const journeyId = journeySelect ? journeySelect.value : null;

    const formData = new FormData();
    formData.append("timetable_file", currentTimetableFile);
    if (journeyId && journeyId !== "auto_create") formData.append("journey_id", journeyId);
    formData.append("service_date", document.getElementById("tt-service-date").value || "");
    formData.append("timezone", document.getElementById("tt-timezone").value || "UTC");
    formData.append("tolerance_minutes", document.getElementById("tt-tolerance-minutes").value || "3");
    formData.append("time_window_mode", document.getElementById("tt-window-mode").value || "ARRIVAL_DEPARTURE");

    const valBtn = document.getElementById("btn-validate-tt");
    valBtn.disabled = true;
    valBtn.innerHTML = `<i class="bi bi-cpu"></i> Parsing Timetable...`;

    try {
        const res = await fetch("/api/automated/import-timetable", {
            method: "POST",
            body: formData
        });

        const data = await res.json();
        valBtn.disabled = false;
        valBtn.innerHTML = `<i class="bi bi-check2-circle text-primary"></i> Validate & Parse Timetable`;

        const valMsg = document.getElementById("timetable-validation-msg");
        if (res.ok && data.success) {
            currentTimetableId = data.timetable_id;
            currentStopsList = data.stops || [];

            const badge = document.getElementById("timetable-readiness-badge");
            if (badge) {
                badge.textContent = `${data.total_stops} Stops Validated`;
                badge.className = "badge-status badge-present";
            }
            if (valMsg) {
                valMsg.style.display = "block";
                valMsg.style.background = "rgba(16,185,129,0.1)";
                valMsg.style.color = "#10b981";
                valMsg.innerHTML = `<i class="bi bi-check-circle-fill"></i> Successfully parsed ${data.total_stops} stops. Columns mapped: Stop=${data.column_mapping.stop_name}, Arr=${data.column_mapping.arrival_time}, Dep=${data.column_mapping.departure_time}`;
            }

            if (data.service_date) {
                const sDateInput = document.getElementById("tt-service-date");
                if (sDateInput && !sDateInput.value) {
                    sDateInput.value = data.service_date;
                }
            }

            document.getElementById("btn-match-clips").style.display = "inline-block";
            renderStopsTable(currentStopsList);
            showToast(`Timetable validated! Loaded ${data.total_stops} stops.`, "success");

            // Auto-trigger clip matching if source folders are selected
            const hasFolders = Object.values(selectedFolderPaths).some(p => p !== null);
            if (hasFolders) {
                triggerAutoMatchClips();
            }
        } else {
            if (valMsg) {
                valMsg.style.display = "block";
                valMsg.style.background = "#fee2e2";
                valMsg.style.color = "#b91c1c";
                valMsg.innerHTML = `<i class="bi bi-exclamation-triangle-fill"></i> ${data.error || "Validation failed"}`;
            }
            showToast(data.error || "Timetable parsing error", "error");
        }
    } catch (err) {
        valBtn.disabled = false;
        valBtn.innerHTML = `<i class="bi bi-check2-circle text-primary"></i> Validate & Parse Timetable`;
        showToast("Network error uploading timetable.", "error");
    }
}

async function triggerAutoMatchClips() {
    if (!currentTimetableId) {
        showToast("Please import and validate timetable first.", "error");
        return;
    }

    const matchBtn = document.getElementById("btn-match-clips");
    matchBtn.disabled = true;
    matchBtn.innerHTML = `<i class="bi bi-hourglass-split"></i> Matching Clips Across Angles...`;

    try {
        const res = await fetch("/api/automated/match-clips", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ timetable_id: currentTimetableId })
        });

        const data = await res.json();
        matchBtn.disabled = false;
        matchBtn.innerHTML = `<i class="bi bi-magic"></i> Auto-Match Stop Clips`;

        if (res.ok && data.success) {
            currentStopsList = data.stops || [];
            renderStopsTable(currentStopsList);
            showToast(data.message || "Auto-matching completed!", "success");
        } else {
            showToast(data.error || "Clip matching failed.", "error");
        }
    } catch (err) {
        matchBtn.disabled = false;
        matchBtn.innerHTML = `<i class="bi bi-magic"></i> Auto-Match Stop Clips`;
        showToast("Network error matching clips.", "error");
    }
}

function renderStopsTable(stops) {
    const tbody = document.getElementById("stops-table-tbody");
    if (!tbody) return;

    if (!stops || stops.length === 0) {
        tbody.innerHTML = `<tr><td colspan="11" class="text-center py-4" style="color:var(--text-muted);">No stops found.</td></tr>`;
        return;
    }

    // Update summary stat counters
    const totalStops = stops.length;
    let fully = 0, partially = 0, notCovered = 0, skippedBw = 0, processed = 0, failed = 0;
    stops.forEach(s => {
        if (s.coverage_status === "FULLY_COVERED") fully++;
        else if (s.coverage_status === "PARTIALLY_COVERED") partially++;
        else if (s.coverage_status === "SKIPPED_BW") skippedBw++;
        else notCovered++;

        if (s.processing_status === "completed" || s.processing_status === "SKIPPED_BLACK_AND_WHITE") processed++;
        else if (s.processing_status === "failed") failed++;
    });

    const elTotal = document.getElementById("stat-job-total");
    if (elTotal) elTotal.textContent = totalStops;
    const elFully = document.getElementById("stat-job-fully");
    if (elFully) elFully.textContent = fully;
    const elPartially = document.getElementById("stat-job-partially");
    if (elPartially) elPartially.textContent = partially;
    const elNot = document.getElementById("stat-job-not");
    if (elNot) elNot.textContent = notCovered;
    const elProc = document.getElementById("stat-job-processed");
    if (elProc) elProc.textContent = processed;
    const elRem = document.getElementById("stat-job-remaining");
    if (elRem) elRem.textContent = Math.max(0, totalStops - (processed + failed));
    const elFail = document.getElementById("stat-job-failed");
    if (elFail) elFail.textContent = failed;

    tbody.innerHTML = "";
    stops.forEach((s, idx) => {
        const tr = document.createElement("tr");

        const matches = {};
        if (s.matches) {
            s.matches.forEach(m => { matches[m.camera_angle] = m; });
        }

        const ma = matches["A"];
        const mb = matches["B"];
        const mc = matches["C"];

        const getAngleBadge = (m) => {
            if (!m || m.status === "missing") return `<span class="badge-status" style="background:#f1f5f9;color:#94a3b8;font-size:10px;">Missing</span>`;
            if (m.status === "skipped_black_and_white") return `<span class="badge-status" style="background:#475569;color:#fff;font-size:10px;" title="${m.skip_reason || 'Black-and-white / grayscale footage skipped'}"><i class="bi bi-moon-stars"></i> B&W Skip</span>`;
            if (m.status === "processed") return `<span class="badge-status badge-present" style="font-size:10px;" title="${m.source_filename || ''}">${m.detection_count || 0} pax</span>`;

            // Display capture time or clean filename tail rather than truncated prefix
            let label = m.source_filename || 'Matched';
            if (m.capture_timestamp) {
                const parts = m.capture_timestamp.split(" ");
                label = (parts[1] || parts[0]).substring(0, 8);
            } else if (label.length > 14) {
                const match = label.match(/(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/) || label.match(/(\d{2})(\d{2})(\d{2})\./);
                if (match && match.length >= 7) {
                    label = `${match[4]}:${match[5]}:${match[6]}`;
                } else {
                    label = label.substring(label.length - 12);
                }
            }
            return `<span class="badge-status" style="background:#e0e7ff;color:#4338ca;font-size:10px;font-weight:600;" title="${m.source_filename || ''}">${label}</span>`;
        };

        let covClass = "badge-status";
        let covBg = "#f1f5f9";
        let covColor = "#64748b";
        if (s.coverage_status === "FULLY_COVERED") { covBg = "#dcfce7"; covColor = "#15803d"; }
        else if (s.coverage_status === "PARTIALLY_COVERED") { covBg = "#fef3c7"; covColor = "#b45309"; }
        else if (s.coverage_status === "NEEDS_REVIEW") { covBg = "#fee2e2"; covColor = "#b91c1c"; }

        const occ = (s.occupancy !== null && s.occupancy !== undefined) ? s.occupancy : (s.estimated_unique_passengers || 0);

        tr.innerHTML = `
            <td><strong>#${s.stop_number}</strong></td>
            <td><strong>${s.stop_name}</strong></td>
            <td>${s.scheduled_arrival ? s.scheduled_arrival.split(" ")[1] || s.scheduled_arrival : "-"}</td>
            <td>${s.scheduled_departure ? s.scheduled_departure.split(" ")[1] || s.scheduled_departure : "-"}</td>
            <td>${getAngleBadge(ma)}</td>
            <td>${getAngleBadge(mb)}</td>
            <td>${getAngleBadge(mc)}</td>
            <td><span style="color:#10b981;font-weight:600;">+${s.boarded_count || 0}</span></td>
            <td><span style="color:#64748b;">${s.continuing_count || 0}</span></td>
            <td><span style="color:#ef4444;font-weight:600;">-${s.exited_count || 0}</span></td>
            <td><strong style="color:var(--accent-primary);">${occ}</strong></td>
            <td><span class="badge-status" style="background:${covBg};color:${covColor};font-size:10px;">${s.coverage_status}</span></td>
            <td><span class="badge-status" style="font-size:10px;">${s.processing_status}</span></td>
            <td>
                <button type="button" class="btn-secondary" style="padding:2px 6px;font-size:11px;" onclick="loadStopIntoMultiAngleView(${s.id})">
                    <i class="bi bi-eye"></i> View
                </button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function loadStopIntoMultiAngleView(stopId) {
    const stop = currentStopsList.find(s => s.id === stopId);
    if (!stop) return;

    activeStopNavIndex = currentStopsList.findIndex(s => s.id === stopId);

    // Set geolocation and timestamp context
    const masterTs = document.getElementById("input-timestamp");
    if (masterTs && stop.scheduled_arrival) masterTs.value = stop.scheduled_arrival;

    // Load matches into angles
    if (stop.matches) {
        stop.matches.forEach(m => {
            const lower = m.camera_angle.toLowerCase();
            const badge = document.getElementById(`status-badge-${lower}`);
            const tsInput = document.getElementById(`timestamp-${lower}`);
            if (tsInput && m.capture_timestamp) tsInput.value = m.capture_timestamp;
            if (badge) {
                badge.textContent = m.status === "processed" ? `Processed (${m.detection_count} pax)` : (m.status === "matched" ? "Matched" : "Missing");
                badge.className = m.status === "processed" ? "badge-status badge-present" : (m.status === "matched" ? "badge-status badge-possibly-absent" : "badge-status");
            }
        });
    }

    showToast(`Loaded Stop #${stop.stop_number} (${stop.stop_name}) into Multi-Angle View.`, "info");
}

function navigateStop(delta) {
    if (!currentStopsList || currentStopsList.length === 0) return;
    activeStopNavIndex = Math.max(0, Math.min(currentStopsList.length - 1, activeStopNavIndex + delta));
    const target = currentStopsList[activeStopNavIndex];
    if (target) loadStopIntoMultiAngleView(target.id);
}

async function resetJourneyPassengers() {
    if (!confirm("Are you sure you want to reset passenger identities and observations for this journey? This will clear duplicate/mixed passengers so you can re-process with strict uniqueness and clean timelines.")) {
        return;
    }

    const journeySelect = document.getElementById("journey-select");
    const journeyId = journeySelect ? journeySelect.value : null;

    try {
        const res = await fetch("/api/automated/reset-journey-passengers", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                journey_id: (journeyId && journeyId !== "auto_create") ? parseInt(journeyId) : null,
                timetable_id: currentTimetableId
            })
        });

        const data = await res.json();
        if (res.ok && data.success) {
            showToast(data.message || "Passenger data reset successfully!", "success");
            // Refresh table stops
            if (currentTimetableId) {
                const ttRes = await fetch(`/api/automated/timetable/${currentTimetableId}/preview`);
                if (ttRes.ok) {
                    const ttData = await ttRes.json();
                    if (ttData.stops) {
                        currentStopsList = ttData.stops;
                        renderStopsTable(currentStopsList);
                    }
                }
            }
        } else {
            showToast(data.error || "Failed to reset passenger data", "error");
        }
    } catch (err) {
        showToast("Network error resetting passenger data.", "error");
    }
}

// Batch Processing Job Controls
async function startAutomatedJob() {
    let journeyId = document.getElementById("journey-select").value;
    if (!journeyId || journeyId === "auto_create") {
        journeyId = await quickStartActiveJourney();
        if (!journeyId) return;
    }

    if (!currentTimetableId) {
        showToast("Please import and validate timetable before starting automated processing.", "error");
        return;
    }

    const startBtn = document.getElementById("btn-start-job");
    startBtn.disabled = true;

    try {
        const res = await fetch("/api/automated/jobs/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                journey_id: parseInt(journeyId),
                timetable_id: currentTimetableId
            })
        });

        const data = await res.json();
        startBtn.disabled = false;

        if (res.ok && data.success) {
            currentJobId = data.job.id;
            updateJobUIControls("running");
            showToast("Automated Multi-Angle Processing Job started!", "success");
            startJobPolling();
        } else {
            showToast(data.error || "Failed to start job", "error");
        }
    } catch (err) {
        startBtn.disabled = false;
        showToast("Network error starting job.", "error");
    }
}

function startJobPolling() {
    if (jobPollInterval) clearInterval(jobPollInterval);
    jobPollInterval = setInterval(async () => {
        if (!currentJobId) return;
        try {
            const res = await fetch(`/api/automated/jobs/${currentJobId}/status`);
            if (res.ok) {
                const data = await res.json();
                if (data.success && data.job) {
                    renderJobProgress(data.job);
                    if (["completed", "cancelled", "failed"].includes(data.job.status)) {
                        clearInterval(jobPollInterval);
                        updateJobUIControls(data.job.status);
                        // Refresh timetable stops
                        refreshTimetableStops();
                    }
                }
            }
        } catch (e) {
            console.error("Job polling error:", e);
        }
    }, 2000);
}

function renderJobProgress(job) {
    document.getElementById("stat-job-total").textContent = job.total_stops;
    document.getElementById("stat-job-processed").textContent = job.processed_stops;
    document.getElementById("stat-job-remaining").textContent = job.remaining_stops;
    document.getElementById("stat-job-fully").textContent = job.fully_covered_stops;
    document.getElementById("stat-job-partially").textContent = job.partially_covered_stops;
    document.getElementById("stat-job-not").textContent = job.not_covered_stops;
    document.getElementById("stat-job-failed").textContent = job.failed_stops;

    const bar = document.getElementById("job-progress-bar");
    const pct = document.getElementById("job-progress-percentage");
    const label = document.getElementById("job-current-step-label");
    const badge = document.getElementById("job-status-badge");

    if (bar) bar.style.width = `${job.progress_percent}%`;
    if (pct) pct.textContent = `${Math.round(job.progress_percent)}%`;
    if (label) label.textContent = `Current: Stop #${job.current_stop_number || '-'} ${job.current_stop_name || ''} (Angle ${job.current_angle || 'Sync'})`;
    if (badge) {
        badge.textContent = job.status.toUpperCase();
        badge.className = job.status === "completed" ? "badge-status badge-present" : (job.status === "running" ? "badge-status badge-possibly-absent" : "badge-status");
    }
}

function updateJobUIControls(status) {
    const btnStart = document.getElementById("btn-start-job");
    const btnPause = document.getElementById("btn-pause-job");
    const btnResume = document.getElementById("btn-resume-job");
    const btnCancel = document.getElementById("btn-cancel-job");
    const btnRetry = document.getElementById("btn-retry-failed-job");

    if (status === "running") {
        if (btnStart) btnStart.style.display = "none";
        if (btnPause) btnPause.style.display = "inline-block";
        if (btnResume) btnResume.style.display = "none";
        if (btnCancel) btnCancel.style.display = "inline-block";
        if (btnRetry) btnRetry.style.display = "none";
    } else if (status === "paused") {
        if (btnPause) btnPause.style.display = "none";
        if (btnResume) btnResume.style.display = "inline-block";
        if (btnCancel) btnCancel.style.display = "inline-block";
    } else {
        if (btnStart) btnStart.style.display = "inline-block";
        if (btnPause) btnPause.style.display = "none";
        if (btnResume) btnResume.style.display = "none";
        if (btnCancel) btnCancel.style.display = "none";
        if (btnRetry) btnRetry.style.display = "inline-block";
    }
}

async function pauseAutomatedJob() {
    if (!currentJobId) return;
    const res = await fetch(`/api/automated/jobs/${currentJobId}/pause`, { method: "POST" });
    if (res.ok) { updateJobUIControls("paused"); showToast("Job paused.", "info"); }
}

async function resumeAutomatedJob() {
    if (!currentJobId) return;
    const res = await fetch(`/api/automated/jobs/${currentJobId}/resume`, { method: "POST" });
    if (res.ok) { updateJobUIControls("running"); showToast("Job resumed.", "info"); }
}

async function cancelAutomatedJob() {
    if (!currentJobId) return;
    const res = await fetch(`/api/automated/jobs/${currentJobId}/cancel`, { method: "POST" });
    if (res.ok) { updateJobUIControls("cancelled"); showToast("Job cancelled.", "warning"); }
}

async function retryFailedStops() {
    if (!currentJobId) return;
    const res = await fetch(`/api/automated/jobs/${currentJobId}/retry-failed`, { method: "POST" });
    if (res.ok) {
        updateJobUIControls("running");
        showToast("Retrying failed stops...", "info");
        startJobPolling();
    }
}

async function refreshTimetableStops() {
    if (!currentTimetableId) return;
    try {
        const res = await fetch(`/api/automated/timetable/${currentTimetableId}/preview`);
        if (res.ok) {
            const data = await res.json();
            if (data.success) {
                currentStopsList = data.stops || [];
                renderStopsTable(currentStopsList);
            }
        }
    } catch (e) {
        console.error("Refresh stops error:", e);
    }
}

function exportStopReport(format) {
    if (!currentTimetableId) {
        showToast("Please import a timetable first.", "error");
        return;
    }
    if (format === "summary-pdf") {
        window.open(`/api/automated/timetable/${currentTimetableId}/summary.pdf`, "_blank");
    } else {
        window.open(`/api/automated/timetable/${currentTimetableId}/export.${format}`, "_blank");
    }
}

function exportStopManifest() {
    if (!currentTimetableId) {
        showToast("Please import a timetable first.", "error");
        return;
    }
    window.open(`/api/automated/timetable/${currentTimetableId}/manifest.csv`, "_blank");
}

async function refreshWorkspace() {
    if (!confirm("Are you sure you want to refresh the workspace? This will reset all pending/running analysis state, reset stop counters, clear uploaded data, and reset passenger identities for a clean start.")) {
        return;
    }
    try {
        const journeySelect = document.getElementById("journey-select");
        const journeyId = journeySelect ? journeySelect.value : null;

        const res = await fetch("/api/automated/refresh-workspace", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                journey_id: (journeyId && journeyId !== "auto_create") ? parseInt(journeyId) : null,
                timetable_id: currentTimetableId,
                clear_uploads: true
            })
        });
        const data = await res.json();
        if (res.ok && data.success) {
            showToast(data.message || "Workspace refreshed cleanly!", "success");
            clearWorkspaceUI();
        } else {
            showToast(data.error || "Failed to refresh workspace", "error");
        }
    } catch (e) {
        showToast("Network error refreshing workspace.", "error");
    }
}

async function deleteTemporaryData() {
    if (!confirm("Are you sure you want to delete temporary files and purge demo records? This will delete cached extracted frames and unlinked crops from disk and remove demo records from the database.")) {
        return;
    }
    try {
        const res = await fetch("/api/automated/delete-temporary-data", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({})
        });
        const data = await res.json();
        if (res.ok && data.success) {
            showToast(data.message || `Deleted temporary files successfully!`, "success");
            clearWorkspaceUI();
        } else {
            showToast(data.error || "Failed to delete temporary data", "error");
        }
    } catch (e) {
        showToast("Network error deleting temporary data.", "error");
    }
}


