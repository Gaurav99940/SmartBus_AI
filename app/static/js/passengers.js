// SmartBus AI — Passenger Directory Date-Wise & Multi-Angle Controller
// Fully handles Date-Wise Reports, Angle-Wise Lists, Crop Gallery, Image Enlargement, and Manual Merge & Undo

let currentReportData = null;
let selectedPersonIds = new Set();
let selectedPersonCardsMap = new Map(); // personId -> { personId, canonicalId, cropUrl, angle, time }
let currentGalleryItems = []; // Ordered list of all gallery items currently displayed
let currentModalImageIndex = -1;
let currentZoomLevel = 1.0;

function formatPassengerPhotoUrl(pathOrUrl) {
    if (!pathOrUrl) return null;
    const s = String(pathOrUrl).trim();
    if (s.startsWith('http://') || s.startsWith('https://')) return s;
    if (s.startsWith('/data/http://') || s.startsWith('/data/https://')) return s.substring(6);
    if (s.startsWith('/')) return s;
    return `/data/${s}`;
}

document.addEventListener("DOMContentLoaded", () => {
    initFilters();
    loadDateWiseReport();
    setupKeyboardListeners();
});

// Setup ESC and Arrow key listeners for modal navigation
function setupKeyboardListeners() {
    document.addEventListener("keydown", (e) => {
        const enlargeModal = document.getElementById("image-enlarge-modal");
        if (enlargeModal && enlargeModal.classList.contains("active")) {
            if (e.key === "Escape") {
                closeImageEnlargeModal();
            } else if (e.key === "ArrowLeft") {
                navigateEnlargeModal(-1);
            } else if (e.key === "ArrowRight") {
                navigateEnlargeModal(1);
            }
        }

        const mergeModal = document.getElementById("merge-confirm-modal");
        if (mergeModal && mergeModal.classList.contains("active") && e.key === "Escape") {
            closeMergeModal();
        }

        const historyModal = document.getElementById("merge-history-modal");
        if (historyModal && historyModal.classList.contains("active") && e.key === "Escape") {
            closeMergeHistoryModal();
        }

        const timelineModal = document.getElementById("passenger-modal");
        if (timelineModal && timelineModal.classList.contains("active") && e.key === "Escape") {
            closePassengerModal();
        }
    });
}

function initFilters() {
    // Populate journeys or buses if available
    fetch("/api/journeys")
        .then(r => r.json())
        .then(journeys => {
            const select = document.getElementById("filter-journey");
            if (!select || !Array.isArray(journeys)) return;
            journeys.forEach(j => {
                const opt = document.createElement("option");
                opt.value = j.id;
                opt.textContent = `${j.journey_code} (${j.origin} -> ${j.destination})`;
                select.appendChild(opt);
            });
        })
        .catch(console.error);

    fetch("/api/buses")
        .then(r => r.json())
        .then(buses => {
            const select = document.getElementById("filter-bus");
            if (!select || !Array.isArray(buses)) return;
            buses.forEach(b => {
                const opt = document.createElement("option");
                opt.value = b.id;
                opt.textContent = `${b.bus_number} ${b.bus_name ? '— ' + b.bus_name : ''}`;
                select.appendChild(opt);
            });
        })
        .catch(console.error);
}

function getFilterParams() {
    const params = new URLSearchParams();
    const dateVal = document.getElementById("filter-date")?.value;
    const startDate = document.getElementById("filter-start-date")?.value;
    const endDate = document.getElementById("filter-end-date")?.value;
    const busId = document.getElementById("filter-bus")?.value;
    const journeyId = document.getElementById("filter-journey")?.value;
    const angle = document.getElementById("filter-angle")?.value;
    const searchPid = document.getElementById("search-person-id")?.value;
    const searchCanonical = document.getElementById("search-canonical-id")?.value;
    const filterStatus = document.getElementById("filter-status")?.value;

    if (dateVal) params.append("date", dateVal);
    if (startDate) params.append("start_date", startDate);
    if (endDate) params.append("end_date", endDate);
    if (busId) params.append("bus_id", busId);
    if (journeyId) params.append("journey_id", journeyId);
    if (angle) params.append("angle", angle);
    if (searchPid) params.append("person_id", searchPid.trim());
    if (searchCanonical) params.append("canonical_id", searchCanonical.trim());
    if (filterStatus) params.append("filter_status", filterStatus);

    return params;
}

async function loadDateWiseReport() {
    const container = document.getElementById("date-sections-container");
    if (!container) return;

    container.innerHTML = `
        <div style="text-align:center;padding:50px;color:var(--text-muted);">
            <div class="spinner-border text-primary" role="status" style="width:2.5rem;height:2.5rem;margin-bottom:12px;"></div>
            <div style="font-size:15px;font-weight:600;">Generating date-wise passenger report...</div>
            <div style="font-size:13px;margin-top:4px;">Retrieving multi-angle observations and verifying canonical identities</div>
        </div>
    `;

    const params = getFilterParams();

    try {
        const res = await fetch(`/api/passengers/date-wise-report?${params.toString()}`);
        if (!res.ok) {
            throw new Error(`Report API error: ${res.statusText}`);
        }
        const data = await res.json();
        currentReportData = data;

        // Populate available dates dropdown if empty
        const dateSelect = document.getElementById("filter-date");
        if (dateSelect && dateSelect.options.length <= 1 && data.available_dates) {
            data.available_dates.forEach(d => {
                const opt = document.createElement("option");
                opt.value = d;
                opt.textContent = formatDateLabel(d);
                dateSelect.appendChild(opt);
            });
        }

        renderTopMetrics(data);
        renderDateSections(data);

    } catch (err) {
        console.error("Error loading date-wise report:", err);
        container.innerHTML = `
            <div class="card" style="padding:24px;text-align:center;color:#ef4444;">
                <i class="bi bi-exclamation-triangle" style="font-size:32px;"></i>
                <div style="font-size:16px;font-weight:600;margin-top:8px;">Failed to load passenger report</div>
                <div style="font-size:13px;color:var(--text-muted);margin-top:4px;">${err.message}</div>
                <button class="btn-secondary" style="margin-top:16px;" onclick="loadDateWiseReport()">
                    <i class="bi bi-arrow-clockwise"></i> Try Again
                </button>
            </div>
        `;
    }
}

function formatDateLabel(isoDate) {
    try {
        const parts = isoDate.split("-");
        if (parts.length === 3) {
            const dt = new Date(parseInt(parts[0]), parseInt(parts[1]) - 1, parseInt(parts[2]));
            return dt.toLocaleDateString("en-US", { day: "numeric", month: "long", year: "numeric" });
        }
    } catch (e) {}
    return isoDate;
}

function renderTopMetrics(data) {
    const summary = data.overall_summary || { total_observations: 0, total_unique_passengers: 0 };
    const dateGroups = data.date_groups || [];

    let totalA = 0, totalB = 0, totalC = 0, totalMerged = 0, totalReview = 0;
    dateGroups.forEach(dg => {
        const s = dg.summary || {};
        totalA += s.angle_a_count || 0;
        totalB += s.angle_b_count || 0;
        totalC += s.angle_c_count || 0;
        totalMerged += s.merged_count || 0;
        totalReview += s.needs_review_count || 0;
    });

    const elTotalObs = document.getElementById("metric-total-observations");
    const elUnique = document.getElementById("metric-unique-passengers");
    const elAngleA = document.getElementById("metric-angle-a-count");
    const elAngleB = document.getElementById("metric-angle-b-count");
    const elAngleC = document.getElementById("metric-angle-c-count");
    const elMerged = document.getElementById("metric-merged-count");

    if (elTotalObs) elTotalObs.textContent = summary.total_observations || 0;
    if (elUnique) elUnique.textContent = summary.total_unique_passengers || 0;
    if (elAngleA) elAngleA.textContent = totalA;
    if (elAngleB) elAngleB.textContent = totalB;
    if (elAngleC) elAngleC.textContent = totalC;
    if (elMerged) elMerged.textContent = totalMerged;
}

function renderDateSections(data) {
    const container = document.getElementById("date-sections-container");
    if (!container) return;

    const dateGroups = data.date_groups || [];
    currentGalleryItems = [];

    if (dateGroups.length === 0) {
        container.innerHTML = `
            <div class="card" style="padding:40px;text-align:center;color:var(--text-muted);">
                <i class="bi bi-calendar-x" style="font-size:36px;margin-bottom:8px;display:block;"></i>
                <div style="font-size:16px;font-weight:600;">No passenger detections found for the selected filter criteria.</div>
                <div style="font-size:13px;margin-top:6px;">Try expanding your date range or clearing your search filters.</div>
                <button class="btn-secondary" style="margin-top:16px;" onclick="resetFilters()">
                    <i class="bi bi-arrow-counterclockwise"></i> Reset Filters
                </button>
            </div>
        `;
        return;
    }

    container.innerHTML = "";

    dateGroups.forEach((dateGroup, dateIdx) => {
        const s = dateGroup.summary;
        const sectionId = `date-section-${dateGroup.date.replace(/[^a-zA-Z0-9]/g, '_')}`;

        // Flatten items for modal navigation
        (dateGroup.gallery_crops || []).forEach(item => {
            currentGalleryItems.push(item);
        });

        const dateSectionCard = document.createElement("div");
        dateSectionCard.className = "card date-report-card";
        dateSectionCard.id = sectionId;
        dateSectionCard.style.cssText = "margin-bottom:28px;padding:0;overflow:hidden;border:1px solid var(--border-color);box-shadow:0 2px 8px rgba(0,0,0,0.04);";

        dateSectionCard.innerHTML = `
            <!-- Date Section Header (Collapsible) -->
            <div class="date-header" onclick="toggleDateSection('${sectionId}')" style="cursor:pointer;padding:16px 20px;background:var(--bg-card-hover);border-bottom:1px solid var(--border-color);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
                <div style="display:flex;align-items:center;gap:12px;">
                    <span style="display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;border-radius:8px;background:rgba(59,130,246,0.12);color:#2563eb;font-size:18px;">
                        <i class="bi bi-calendar-date"></i>
                    </span>
                    <div>
                        <h3 style="font-size:17px;font-weight:700;margin:0;color:var(--text-main);letter-spacing:0.3px;">
                            DATE: ${dateGroup.date_formatted.toUpperCase()}
                        </h3>
                        <small style="font-size:12px;color:var(--text-muted);font-weight:500;">
                            ISO: ${dateGroup.date} &bull; ${s.total_unique_passengers} Unique Canonical Passenger${s.total_unique_passengers === 1 ? '' : 's'}
                        </small>
                    </div>
                </div>

                <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
                    <span class="badge-tag tag-unique" title="Distinct canonical passengers">
                        <i class="bi bi-person-check-fill"></i> ${s.total_unique_passengers} Unique
                    </span>
                    <span class="badge-tag tag-angle-a">
                        Angle A: ${s.angle_a_count}
                    </span>
                    <span class="badge-tag tag-angle-b">
                        Angle B: ${s.angle_b_count}
                    </span>
                    <span class="badge-tag tag-angle-c">
                        Angle C: ${s.angle_c_count}
                    </span>
                    ${s.unknown_angle_count > 0 ? `<span class="badge-tag tag-angle-unknown">Unknown: ${s.unknown_angle_count}</span>` : ''}
                    ${s.merged_count > 0 ? `<span class="badge-tag tag-merged"><i class="bi bi-link-45deg"></i> ${s.merged_count} Merged</span>` : ''}
                    <button class="btn-toggle-section" style="background:none;border:none;color:var(--text-muted);font-size:18px;margin-left:8px;cursor:pointer;">
                        <i class="bi bi-chevron-down toggle-icon"></i>
                    </button>
                </div>
            </div>

            <!-- Date Section Body -->
            <div class="date-body" style="padding:20px;">
                
                <!-- UNIQUE PASSENGERS MANIFEST (IN SEQUENCE) -->
                ${renderUniquePassengersSection(dateGroup)}

                <!-- ANGLE DETECTION ACTION CARDS (CLICK TO OPEN POPUP LIST) -->
                <div style="margin-bottom:24px;">
                    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;flex-wrap:wrap;gap:8px;">
                        <span style="font-size:13px;font-weight:700;color:var(--text-main);text-transform:uppercase;letter-spacing:0.5px;display:flex;align-items:center;gap:6px;">
                            <i class="bi bi-camera-reels text-primary"></i> Camera Angle Reports (Click to Open Popup List)
                        </span>
                        <span style="font-size:12px;color:var(--text-muted);">
                            Angle A, B, and C passenger detections are isolated and viewable on demand in modal view
                        </span>
                    </div>

                    <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(280px, 1fr));gap:16px;">
                        <!-- Angle A Card -->
                        <div class="angle-action-card angle-card-a" onclick="openAnglePassengerModal('${dateGroup.date}', 'A')" style="cursor:pointer;background:var(--bg-card);border:1px solid rgba(59,130,246,0.3);border-radius:10px;padding:16px;transition:all 0.2s ease;box-shadow:0 2px 6px rgba(59,130,246,0.06);display:flex;flex-direction:column;justify-content:space-between;">
                            <div>
                                <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
                                    <div style="display:flex;align-items:center;gap:10px;">
                                        <span style="display:inline-flex;align-items:center;justify-content:center;width:38px;height:38px;border-radius:8px;background:rgba(59,130,246,0.12);color:#2563eb;font-size:18px;">
                                            <i class="bi bi-camera-fill"></i>
                                        </span>
                                        <div>
                                            <h4 style="margin:0;font-size:15px;font-weight:700;color:#1d4ed8;">Angle A (Section A)</h4>
                                            <div style="font-size:11px;color:var(--text-muted);font-weight:500;">Front / Assigned Seating</div>
                                        </div>
                                    </div>
                                    <span class="badge bg-primary" style="font-size:12px;padding:5px 10px;border-radius:20px;">
                                        ${s.angle_a_count} Detections
                                    </span>
                                </div>
                                <p style="font-size:12px;color:var(--text-muted);margin:0 0 12px 0;">
                                    Front cabin view. Click to view detailed passenger detection list, photos, timestamps, and confidence ratings in a dedicated popup.
                                </p>
                            </div>
                            <div style="display:flex;align-items:center;justify-content:space-between;border-top:1px solid rgba(59,130,246,0.15);padding-top:10px;margin-top:4px;">
                                <span style="font-size:12px;color:#2563eb;font-weight:600;"><i class="bi bi-box-arrow-up-right"></i> Open Angle A Popup</span>
                                <span style="font-size:11px;color:var(--text-muted);">${(dateGroup.angle_sections.angle_a || []).length} Records</span>
                            </div>
                        </div>

                        <!-- Angle B Card -->
                        <div class="angle-action-card angle-card-b" onclick="openAnglePassengerModal('${dateGroup.date}', 'B')" style="cursor:pointer;background:var(--bg-card);border:1px solid rgba(16,185,129,0.3);border-radius:10px;padding:16px;transition:all 0.2s ease;box-shadow:0 2px 6px rgba(16,185,129,0.06);display:flex;flex-direction:column;justify-content:space-between;">
                            <div>
                                <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
                                    <div style="display:flex;align-items:center;gap:10px;">
                                        <span style="display:inline-flex;align-items:center;justify-content:center;width:38px;height:38px;border-radius:8px;background:rgba(16,185,129,0.12);color:#059669;font-size:18px;">
                                            <i class="bi bi-camera-fill"></i>
                                        </span>
                                        <div>
                                            <h4 style="margin:0;font-size:15px;font-weight:700;color:#047857;">Angle B (Section B)</h4>
                                            <div style="font-size:11px;color:var(--text-muted);font-weight:500;">Middle / Assigned Seating</div>
                                        </div>
                                    </div>
                                    <span class="badge bg-success" style="font-size:12px;padding:5px 10px;border-radius:20px;">
                                        ${s.angle_b_count} Detections
                                    </span>
                                </div>
                                <p style="font-size:12px;color:var(--text-muted);margin:0 0 12px 0;">
                                    Middle cabin view. Click to view detailed passenger detection list, photos, timestamps, and confidence ratings in a dedicated popup.
                                </p>
                            </div>
                            <div style="display:flex;align-items:center;justify-content:space-between;border-top:1px solid rgba(16,185,129,0.15);padding-top:10px;margin-top:4px;">
                                <span style="font-size:12px;color:#059669;font-weight:600;"><i class="bi bi-box-arrow-up-right"></i> Open Angle B Popup</span>
                                <span style="font-size:11px;color:var(--text-muted);">${(dateGroup.angle_sections.angle_b || []).length} Records</span>
                            </div>
                        </div>

                        <!-- Angle C Card -->
                        <div class="angle-action-card angle-card-c" onclick="openAnglePassengerModal('${dateGroup.date}', 'C')" style="cursor:pointer;background:var(--bg-card);border:1px solid rgba(168,85,247,0.3);border-radius:10px;padding:16px;transition:all 0.2s ease;box-shadow:0 2px 6px rgba(168,85,247,0.06);display:flex;flex-direction:column;justify-content:space-between;">
                            <div>
                                <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
                                    <div style="display:flex;align-items:center;gap:10px;">
                                        <span style="display:inline-flex;align-items:center;justify-content:center;width:38px;height:38px;border-radius:8px;background:rgba(168,85,247,0.12);color:#7e22ce;font-size:18px;">
                                            <i class="bi bi-camera-fill"></i>
                                        </span>
                                        <div>
                                            <h4 style="margin:0;font-size:15px;font-weight:700;color:#7e22ce;">Angle C (Section C)</h4>
                                            <div style="font-size:11px;color:var(--text-muted);font-weight:500;">Rear / Assigned Seating</div>
                                        </div>
                                    </div>
                                    <span class="badge" style="background:#a855f7;color:#fff;font-size:12px;padding:5px 10px;border-radius:20px;">
                                        ${s.angle_c_count} Detections
                                    </span>
                                </div>
                                <p style="font-size:12px;color:var(--text-muted);margin:0 0 12px 0;">
                                    Rear cabin view. Click to view detailed passenger detection list, photos, timestamps, and confidence ratings in a dedicated popup.
                                </p>
                            </div>
                            <div style="display:flex;align-items:center;justify-content:space-between;border-top:1px solid rgba(168,85,247,0.15);padding-top:10px;margin-top:4px;">
                                <span style="font-size:12px;color:#7e22ce;font-weight:600;"><i class="bi bi-box-arrow-up-right"></i> Open Angle C Popup</span>
                                <span style="font-size:11px;color:var(--text-muted);">${(dateGroup.angle_sections.angle_c || []).length} Records</span>
                            </div>
                        </div>

                        ${dateGroup.angle_sections.unknown_angle && dateGroup.angle_sections.unknown_angle.length > 0 ? `
                        <!-- Unknown Angle Card -->
                        <div class="angle-action-card angle-card-unknown" onclick="openAnglePassengerModal('${dateGroup.date}', 'UNKNOWN')" style="cursor:pointer;background:var(--bg-card);border:1px solid rgba(100,116,139,0.3);border-radius:10px;padding:16px;transition:all 0.2s ease;box-shadow:0 2px 6px rgba(100,116,139,0.06);display:flex;flex-direction:column;justify-content:space-between;">
                            <div>
                                <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
                                    <div style="display:flex;align-items:center;gap:10px;">
                                        <span style="display:inline-flex;align-items:center;justify-content:center;width:38px;height:38px;border-radius:8px;background:rgba(100,116,139,0.12);color:#334155;font-size:18px;">
                                            <i class="bi bi-question-circle-fill"></i>
                                        </span>
                                        <div>
                                            <h4 style="margin:0;font-size:15px;font-weight:700;color:#334155;">Unknown / Multi-Angle</h4>
                                            <div style="font-size:11px;color:var(--text-muted);font-weight:500;">Unassigned Camera</div>
                                        </div>
                                    </div>
                                    <span class="badge bg-secondary" style="font-size:12px;padding:5px 10px;border-radius:20px;">
                                        ${s.unknown_angle_count} Detections
                                    </span>
                                </div>
                                <p style="font-size:12px;color:var(--text-muted);margin:0 0 12px 0;">
                                    Captures detections from unassigned or multi-view cameras. Click to inspect detections.
                                </p>
                            </div>
                            <div style="display:flex;align-items:center;justify-content:space-between;border-top:1px solid rgba(100,116,139,0.15);padding-top:10px;margin-top:4px;">
                                <span style="font-size:12px;color:#334155;font-weight:600;"><i class="bi bi-box-arrow-up-right"></i> Open Unknown List</span>
                                <span style="font-size:11px;color:var(--text-muted);">${(dateGroup.angle_sections.unknown_angle || []).length} Records</span>
                            </div>
                        </div>
                        ` : ''}
                    </div>
                </div>

                <hr style="border:0;border-top:1px solid var(--border-color);margin:24px 0;">

                <!-- SECTION: PASSENGERS DETECTED ON THIS DATE (Crop Gallery - Strictly One Card Per Unique Person ID) -->
                ${(() => {
                    const seenPids = new Set();
                    const activeGalleryCrops = [];
                    (dateGroup.gallery_crops || []).forEach(c => {
                        const pid = String(c.canonical_person_id || c.person_id || "").trim();
                        const rawPid = String(c.person_id || "").trim();
                        if (pid && !seenPids.has(pid) && !seenPids.has(rawPid) && !c.is_merged) {
                            seenPids.add(pid);
                            if (rawPid) seenPids.add(rawPid);
                            activeGalleryCrops.push(c);
                        }
                    });
                    currentGalleryItems.push(...activeGalleryCrops);
                    return `
                    <div class="gallery-wrapper" style="margin-bottom:16px;">
                        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;flex-wrap:wrap;gap:10px;">
                            <div>
                                <h4 style="font-size:15px;font-weight:700;margin:0;color:var(--text-main);display:flex;align-items:center;gap:8px;">
                                    <i class="bi bi-images text-primary"></i> PASSENGERS DETECTED ON THIS DATE
                                    <span class="badge bg-primary" style="font-size:11px;padding:3px 8px;border-radius:12px;">${activeGalleryCrops.length} Active Passengers (Unique IDs)</span>
                                </h4>
                                <p style="font-size:12px;color:var(--text-muted);margin:3px 0 0 0;">
                                    Single card per unique person ID. Multiple detections across angles are consolidated under their Master ID. Select checkboxes to merge duplicate identities.
                                </p>
                            </div>
                            <div style="display:flex;gap:8px;align-items:center;">
                                <button class="btn-sm btn-secondary" onclick="selectAllInDate('${dateGroup.date}', true)">
                                    <i class="bi bi-check-all"></i> Select All Date
                                </button>
                                <button class="btn-sm btn-secondary" onclick="selectAllInDate('${dateGroup.date}', false)">
                                    <i class="bi bi-x"></i> Deselect
                                </button>
                            </div>
                        </div>

                        <!-- Gallery Grid -->
                        <div class="crop-gallery-grid" id="gallery-grid-${dateGroup.date}">
                            ${renderCropGalleryCards(activeGalleryCrops, dateGroup.date)}
                        </div>
                    </div>
                    `;
                })()}

            </div>
        `;

        container.appendChild(dateSectionCard);
    });

    updateSelectionBar();
}

function renderUniquePassengersSection(dateGroup) {
    const uniqueList = dateGroup.unique_passengers || [];
    if (uniqueList.length === 0) return "";

    return `
        <!-- UNIQUE PASSENGERS MANIFEST (IN SEQUENCE — EXACT COUNTS) -->
        <div class="unique-manifest-panel" style="margin-bottom:28px;background:var(--bg-card);border:1px solid var(--border-color);border-radius:var(--radius-sm);overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.03);">
            <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 18px;background:rgba(37,99,235,0.08);border-bottom:1px solid var(--border-color);flex-wrap:wrap;gap:10px;">
                <div>
                    <h4 style="font-size:15px;font-weight:700;margin:0;color:#1e40af;display:flex;align-items:center;gap:8px;">
                        <i class="bi bi-person-check-fill text-primary"></i> UNIQUE PASSENGERS MANIFEST (IN SEQUENCE)
                        <span class="badge bg-success" style="font-size:11px;padding:3px 8px;border-radius:12px;">${uniqueList.length} Unique Persons Counted</span>
                    </h4>
                    <p style="font-size:12px;color:var(--text-muted);margin:3px 0 0 0;">
                        Single unique ID per passenger. Repeated observations across times and camera angles are consolidated into the Presence Timeline.
                    </p>
                </div>
                <span style="font-size:12px;color:#1e40af;font-weight:600;">
                    <i class="bi bi-sort-numeric-down"></i> Boarding & Detection Sequence
                </span>
            </div>

            <div class="table-responsive" style="margin:0;">
                <table class="custom-table" style="margin-bottom:0;">
                    <thead>
                        <tr>
                            <th style="width:40px;text-align:center;">#</th>
                            <th style="width:60px;">Photo</th>
                            <th>Unique Person ID</th>
                            <th>Status</th>
                            <th>First In (Boarding)</th>
                            <th>Last Seen Presence</th>
                            <th>Presence Timeline (Occurrences Across Times)</th>
                            <th>Seat & Bus</th>
                            <th style="text-align:right;">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${uniqueList.map(u => {
                            const globalIdx = currentGalleryItems.findIndex(i => i.person_id === u.person_id);
                            let thumbHtml = '';
                            const resolvedThumb = formatPassengerPhotoUrl(u.best_crop_url || `/data/${u.person_id}_face.jpg`);
                            if (resolvedThumb) {
                                thumbHtml = `<img src="${resolvedThumb}" class="table-crop-thumb" onclick="openImageEnlargeModal(${globalIdx >= 0 ? globalIdx : 0})" alt="${u.person_id}" title="Click to enlarge photo" onerror="this.onerror=null;this.parentElement.innerHTML='<div class=\\'table-crop-placeholder\\'>${u.person_id ? u.person_id.substring(0, 2) : 'P'}</div>';">`;
                            } else {
                                thumbHtml = `<div class="table-crop-placeholder" onclick="openImageEnlargeModal(${globalIdx >= 0 ? globalIdx : 0})">${u.person_id.substring(0, 2)}</div>`;
                            }

                            let statusBadgeClass = "badge-present";
                            if ((u.status || "").includes("EXIT")) statusBadgeClass = "badge-exit-confirmed";
                            else if ((u.status || "").includes("REVIEW")) statusBadgeClass = "badge-needs-review";

                            return `
                                <tr>
                                    <td style="text-align:center;font-weight:800;color:#2563eb;font-size:13px;">
                                        #${u.sequence_no}
                                    </td>
                                    <td>${thumbHtml}</td>
                                    <td>
                                        <strong class="pid-clickable" onclick="openProvenanceModal('${u.person_id}')" title="Click to view Previous ID & Master ID merge link" style="cursor:pointer;color:var(--text-main);font-size:13px;">
                                            ${u.person_id} <i class="bi bi-box-arrow-up-right" style="font-size:10px;opacity:0.6;margin-left:2px;"></i>
                                        </strong>
                                        <div><span class="badge bg-success" style="font-size:10px;padding:2px 6px;">Unique Master</span></div>
                                    </td>
                                    <td>
                                        <span class="badge-status ${statusBadgeClass}">${u.status || 'PRESENT'}</span>
                                    </td>
                                    <td>
                                        <div style="font-weight:600;font-size:12px;">${u.first_seen_time}</div>
                                        <small style="color:#2563eb;font-weight:600;font-size:11px;">${u.first_angle}</small>
                                    </td>
                                    <td>
                                        <div style="font-weight:600;font-size:12px;">${u.last_seen_time}</div>
                                        <small style="color:#059669;font-weight:600;font-size:11px;">${u.last_angle}</small>
                                    </td>
                                    <td>
                                        <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:4px;">
                                            ${u.presence_occurrences.map(occ => `
                                                <span class="badge bg-light text-dark" style="border:1px solid var(--border-color);font-size:11px;padding:3px 6px;" title="Confirmed presence at ${occ.time} from ${occ.angle} (${occ.det_conf_pct}% confidence)">
                                                    <i class="bi bi-clock"></i> ${occ.time_short} <b style="color:#2563eb;">[${occ.angle.replace('Angle ', '')}]</b>
                                                </span>
                                            `).join("")}
                                        </div>
                                        <div style="font-size:11px;color:var(--text-muted);">
                                            <i class="bi bi-check2-all text-success"></i> <b>${u.total_detections} confirmed detection(s)</b> across ${u.angles_str}
                                        </div>
                                    </td>
                                    <td>
                                        <div style="font-weight:600;font-size:12px;">Seat: ${u.current_seat || 'N/A'}</div>
                                        <small style="color:var(--text-muted);font-size:11px;">${u.bus_number}</small>
                                    </td>
                                    <td style="text-align:right;">
                                        <button class="btn-sm btn-secondary" onclick="openProvenanceModal('${u.person_id}')" title="View Identity Provenance">
                                            <i class="bi bi-link-45deg"></i> Link
                                        </button>
                                        ${u.passenger_id ? `
                                        <button class="btn-sm btn-primary ms-1" onclick="openPassengerModal(${u.passenger_id})" title="View Complete Timeline">
                                            <i class="bi bi-clock-history"></i> Timeline
                                        </button>
                                        ` : ''}
                                    </td>
                                </tr>
                            `;
                        }).join("")}
                    </tbody>
                </table>
            </div>
        </div>
    `;
}

function renderCropGalleryCards(crops, dateStr) {
    if (!crops || crops.length === 0) {
        return `<div style="grid-column:1/-1;padding:24px;text-align:center;color:var(--text-muted);background:var(--bg-app);border-radius:8px;">No crop observations found for this date.</div>`;
    }

    return crops.map((crop, idx) => {
        const isChecked = selectedPersonIds.has(crop.person_id);
        const cardAngleBadge = getAngleBadgeHtml(crop.camera_angle);
        const globalIdx = currentGalleryItems.findIndex(i => i.observation_id === crop.observation_id || i.person_id === crop.person_id);

        let imgHtml = '';
        const finalCropUrl = formatPassengerPhotoUrl(crop.crop_url || `/data/${crop.person_id}_face.jpg`);
        if (finalCropUrl) {
            imgHtml = `
                <div class="crop-img-container" onclick="openImageEnlargeModal(${globalIdx >= 0 ? globalIdx : 0})" title="Click to enlarge image">
                    <img src="${finalCropUrl}" class="crop-img" alt="${crop.person_id}" loading="lazy" onerror="this.onerror=null;this.parentElement.innerHTML='<div class=\\'crop-placeholder-empty\\'><i class=\\'bi bi-person\\'></i><span>${crop.person_id}</span></div>';">
                    <div class="crop-zoom-hover"><i class="bi bi-arrows-fullscreen"></i></div>
                </div>
            `;
        } else {
            imgHtml = `
                <div class="crop-img-container crop-placeholder-missing" onclick="openImageEnlargeModal(${globalIdx >= 0 ? globalIdx : 0})" title="Click to view details">
                    <i class="bi bi-person text-primary" style="font-size:24px;"></i>
                    <span style="font-size:10px;color:var(--text-muted);margin-top:4px;">${crop.person_id}</span>
                </div>
            `;
        }

        let aliasTag = '';
        if (crop.is_merged && crop.canonical_person_id && crop.canonical_person_id !== crop.person_id) {
            aliasTag = `<div class="crop-alias-badge" title="Merged alias of ${crop.canonical_person_id}"><i class="bi bi-link-45deg"></i> Alias of ${crop.canonical_person_id}</div>`;
        }

        return `
            <div class="gallery-card ${isChecked ? 'selected' : ''}" id="card-crop-${crop.observation_id || idx}" data-pid="${crop.person_id}" data-date="${dateStr}">
                <div class="gallery-card-top">
                    <label class="gallery-checkbox-label" onclick="event.stopPropagation();">
                        <input type="checkbox" class="passenger-select-cb" value="${crop.person_id}" 
                            data-obsid="${crop.observation_id}"
                            data-pid="${crop.person_id}"
                            data-canonical="${crop.canonical_person_id}"
                            data-crop="${crop.crop_url || ''}"
                            data-angle="${crop.camera_angle}"
                            data-time="${crop.observation_time}"
                            ${isChecked ? 'checked' : ''} 
                            onchange="togglePassengerSelection('${crop.person_id}', this)">
                        <span class="custom-cb-check"></span>
                    </label>
                    <div style="display:flex;align-items:center;gap:4px;">
                        <span class="badge bg-dark" style="font-size:10px;padding:2px 5px;border-radius:4px;">#${crop.sequence_no || idx + 1}</span>
                        ${cardAngleBadge}
                    </div>
                </div>

                ${imgHtml}

                <div class="gallery-card-info">
                    <div class="crop-pid pid-clickable" onclick="openProvenanceModal('${crop.person_id}')" title="Click to view Previous ID & Master ID merge link" style="cursor:pointer;">
                        ${crop.person_id} <i class="bi bi-box-arrow-up-right" style="font-size:10px;opacity:0.6;margin-left:3px;"></i>
                    </div>
                    ${aliasTag}
                    <div class="crop-meta-row">
                        <span class="badge-conf" title="Confirmed detections & stops traveled">${crop.stops_traveled && crop.stops_traveled > 1 ? `<i class="bi bi-signpost-fill"></i> ${crop.stops_traveled} Stops` : (crop.total_detections ? `${crop.total_detections}x Det` : `${crop.detection_confidence_pct}%`)}</span>
                        <span class="crop-time" title="${crop.presence_summary || crop.observation_time}">${crop.first_seen_time ? crop.first_seen_time.split(' ')[1] : (crop.observation_time ? crop.observation_time.split(' ')[1] : '')}</span>
                    </div>
                </div>
            </div>
        `;
    }).join("");
}

function renderAngleTable(observations, angleTitle) {
    if (!observations || observations.length === 0) {
        return `<div style="padding:16px;text-align:center;color:var(--text-muted);background:var(--bg-app);border-radius:6px;font-size:13px;">No passenger records reported from ${angleTitle} on this date.</div>`;
    }

    return `
        <div class="table-responsive" style="margin-bottom:0;overflow-x:auto;">
            <table class="custom-table angle-table" style="width:100%;min-width:980px;border-collapse:collapse;">
                <thead>
                    <tr style="background:var(--bg-app);border-bottom:2px solid var(--border-color);">
                        <th style="width:64px;min-width:64px;text-align:center;">Photo</th>
                        <th style="min-width:110px;">Person ID</th>
                        <th style="min-width:140px;">Canonical Identity</th>
                        <th style="min-width:140px;">Detection Timestamp</th>
                        <th style="min-width:85px;text-align:center;">Det. Conf.</th>
                        <th style="min-width:85px;text-align:center;">Ident. Conf.</th>
                        <th style="min-width:120px;">Bus & Journey</th>
                        <th style="min-width:130px;text-align:center;">Observation Status</th>
                        <th style="min-width:140px;text-align:right;">Actions</th>
                    </tr>
                </thead>
                <tbody>
                    ${observations.map(obs => {
                        const globalIdx = currentGalleryItems.findIndex(i => i.observation_id === obs.observation_id);
                        const isMerged = obs.is_merged && obs.canonical_person_id && obs.canonical_person_id !== obs.person_id;
                        
                        let thumbHtml = '';
                        const resolvedObsCrop = formatPassengerPhotoUrl(obs.crop_url || `/data/${obs.person_id}_face.jpg`);
                        if (resolvedObsCrop) {
                            thumbHtml = `<img src="${resolvedObsCrop}" class="table-crop-thumb" onclick="openImageEnlargeModal(${globalIdx})" alt="${obs.person_id}" onerror="this.onerror=null;this.parentElement.innerHTML='<div class=\\'table-crop-placeholder\\'>${obs.person_id ? obs.person_id.substring(0, 2) : 'P'}</div>';">`;
                        } else {
                            thumbHtml = `<div class="table-crop-placeholder" onclick="openImageEnlargeModal(${globalIdx})">${obs.person_id.substring(0, 2)}</div>`;
                        }

                        let statusBadgeClass = "badge-present";
                        if ((obs.status || "").includes("EXIT")) statusBadgeClass = "badge-exit-confirmed";
                        else if ((obs.status || "").includes("REVIEW") || (obs.review_status || "").includes("REVIEW")) statusBadgeClass = "badge-needs-review";
                        else if ((obs.status || "").includes("POSSIBLY")) statusBadgeClass = "badge-possibly-absent";

                        return `
                            <tr>
                                <td>${thumbHtml}</td>
                                <td>
                                    <strong class="pid-clickable" onclick="openProvenanceModal('${obs.person_id}')" title="Click to view Previous ID & Master ID merge link" style="cursor:pointer;color:var(--text-main);">
                                        ${obs.person_id} <i class="bi bi-box-arrow-up-right" style="font-size:10px;opacity:0.6;margin-left:2px;"></i>
                                    </strong>
                                    ${obs.current_seat ? `<div style="font-size:11px;color:var(--text-muted);">Seat: ${obs.current_seat}</div>` : ''}
                                </td>
                                <td>
                                    ${isMerged ? `
                                        <span class="badge bg-warning text-dark pid-clickable" onclick="openProvenanceModal('${obs.person_id}')" style="font-size:11px;cursor:pointer;" title="Click to view Previous ID & Master ID merge transition">
                                            <i class="bi bi-link-45deg"></i> Alias of ${obs.canonical_person_id}
                                        </span>
                                    ` : `
                                        <span class="badge bg-success text-white pid-clickable" onclick="openProvenanceModal('${obs.canonical_person_id}')" style="font-size:11px;cursor:pointer;" title="Click to view Master ID details">
                                            <i class="bi bi-shield-check"></i> Master ${obs.canonical_person_id}
                                        </span>
                                    `}
                                </td>
                                <td>
                                    <div>${obs.observation_time}</div>
                                    <small style="color:var(--text-muted);font-size:11px;">${obs.camera_angle}</small>
                                </td>
                                <td>
                                    <span class="badge-status badge-present">${obs.detection_confidence_pct}%</span>
                                </td>
                                <td>
                                    ${obs.identity_confidence_pct !== null ? `
                                        <span class="badge-status badge-present">${obs.identity_confidence_pct}%</span>
                                    ` : `<span style="color:var(--text-muted);font-size:12px;">N/A</span>`}
                                </td>
                                <td>
                                    <div style="font-weight:600;font-size:12px;">${obs.bus_number}</div>
                                    <small style="color:var(--text-muted);font-size:11px;">${obs.journey_code}</small>
                                </td>
                                <td>
                                    <span class="badge-status ${statusBadgeClass}">
                                        ${obs.review_status || obs.status}
                                    </span>
                                </td>
                                <td style="text-align:right;">
                                    <button class="btn-sm btn-secondary" onclick="openImageEnlargeModal(${globalIdx})" title="Enlarge Photo">
                                        <i class="bi bi-zoom-in"></i> View
                                    </button>
                                    ${obs.passenger_id ? `
                                    <button class="btn-sm btn-primary ms-1" onclick="openPassengerModal(${obs.passenger_id})" title="View Passenger Timeline">
                                        <i class="bi bi-clock-history"></i> Timeline
                                    </button>
                                    ` : ''}
                                </td>
                            </tr>
                        `;
                    }).join("")}
                </tbody>
            </table>
        </div>
    `;
}

// -------------------------------------------------------------
// ANGLE PASSENGER POPUP MODAL (User Request)
// -------------------------------------------------------------

function openAnglePassengerModal(dateStr, angleCode) {
    const modal = document.getElementById("angle-passenger-modal");
    if (!modal) return;

    if (!currentReportData || !currentReportData.date_groups) return;
    const dateGroup = currentReportData.date_groups.find(dg => dg.date === dateStr);
    if (!dateGroup) return;

    let observations = [];
    let title = "";
    let subtitle = "";
    let iconClass = "bi-camera-fill";
    let badgeColorStyle = "background:rgba(59,130,246,0.15);color:#2563eb;";
    let angleLabel = "Angle A";

    const code = (angleCode || "A").toUpperCase();
    if (code === "A") {
        observations = (dateGroup.angle_sections && dateGroup.angle_sections.angle_a) || [];
        title = "Section A — Angle A Passenger Detections";
        subtitle = `${dateGroup.date_formatted} &bull; Front Cabin View &bull; ${observations.length} Detections`;
        iconClass = "bi-camera-fill";
        badgeColorStyle = "background:rgba(59,130,246,0.15);color:#2563eb;";
        angleLabel = "Angle A";
    } else if (code === "B") {
        observations = (dateGroup.angle_sections && dateGroup.angle_sections.angle_b) || [];
        title = "Section B — Angle B Passenger Detections";
        subtitle = `${dateGroup.date_formatted} &bull; Middle Cabin View &bull; ${observations.length} Detections`;
        iconClass = "bi-camera-fill";
        badgeColorStyle = "background:rgba(16,185,129,0.15);color:#059669;";
        angleLabel = "Angle B";
    } else if (code === "C") {
        observations = (dateGroup.angle_sections && dateGroup.angle_sections.angle_c) || [];
        title = "Section C — Angle C Passenger Detections";
        subtitle = `${dateGroup.date_formatted} &bull; Rear Cabin View &bull; ${observations.length} Detections`;
        iconClass = "bi-camera-fill";
        badgeColorStyle = "background:rgba(168,85,247,0.15);color:#7e22ce;";
        angleLabel = "Angle C";
    } else {
        observations = (dateGroup.angle_sections && dateGroup.angle_sections.unknown_angle) || [];
        title = "Unknown Angle Passenger Detections";
        subtitle = `${dateGroup.date_formatted} &bull; Unassigned Camera &bull; ${observations.length} Detections`;
        iconClass = "bi-question-circle-fill";
        badgeColorStyle = "background:rgba(100,116,139,0.15);color:#475569;";
        angleLabel = "Unknown Angle";
    }

    const titleEl = document.getElementById("angle-modal-title");
    const subEl = document.getElementById("angle-modal-subtitle");
    const iconSpan = document.getElementById("angle-modal-badge-icon");
    const bodyEl = document.getElementById("angle-modal-body");
    const footerStatsEl = document.getElementById("angle-modal-footer-stats");

    if (titleEl) titleEl.textContent = title;
    if (subEl) subEl.innerHTML = subtitle;
    if (iconSpan) {
        iconSpan.style.cssText = `display:inline-flex;align-items:center;justify-content:center;width:40px;height:40px;border-radius:10px;font-size:20px;${badgeColorStyle}`;
        iconSpan.innerHTML = `<i class="bi ${iconClass}"></i>`;
    }
    if (bodyEl) {
        bodyEl.innerHTML = renderAngleTable(observations, angleLabel);
    }
    if (footerStatsEl) {
        footerStatsEl.innerHTML = `<i class="bi bi-info-circle"></i> Showing <strong>${observations.length}</strong> reported passenger detections for ${angleLabel} on ${dateGroup.date_formatted}`;
    }

    modal.classList.add("active");
}

function closeAnglePassengerModal() {
    const modal = document.getElementById("angle-passenger-modal");
    if (modal) modal.classList.remove("active");
}

window.openAnglePassengerModal = openAnglePassengerModal;
window.closeAnglePassengerModal = closeAnglePassengerModal;

function getAngleBadgeHtml(cameraAngle) {
    const raw = (cameraAngle || "").toUpperCase();
    if (raw.includes("A")) {
        return `<span class="badge-angle-pill pill-angle-a">Angle A</span>`;
    } else if (raw.includes("B")) {
        return `<span class="badge-angle-pill pill-angle-b">Angle B</span>`;
    } else if (raw.includes("C")) {
        return `<span class="badge-angle-pill pill-angle-c">Angle C</span>`;
    } else {
        return `<span class="badge-angle-pill pill-angle-unknown">Unknown</span>`;
    }
}

function toggleDateSection(sectionId) {
    const section = document.getElementById(sectionId);
    if (!section) return;
    const body = section.querySelector(".date-body");
    const icon = section.querySelector(".toggle-icon");
    if (!body) return;

    if (body.style.display === "none") {
        body.style.display = "block";
        if (icon) icon.className = "bi bi-chevron-down toggle-icon";
    } else {
        body.style.display = "none";
        if (icon) icon.className = "bi bi-chevron-right toggle-icon";
    }
}

// -------------------------------------------------------------
// CHECKBOX SELECTION & FLOATING ACTION BAR
// -------------------------------------------------------------

function togglePassengerSelection(personId, checkboxEl) {
    if (checkboxEl.checked) {
        selectedPersonIds.add(personId);
        selectedPersonCardsMap.set(personId, {
            personId: personId,
            canonicalId: checkboxEl.dataset.canonical || personId,
            cropUrl: checkboxEl.dataset.crop || "",
            angle: checkboxEl.dataset.angle || "Angle A",
            time: checkboxEl.dataset.time || ""
        });
    } else {
        selectedPersonIds.delete(personId);
        selectedPersonCardsMap.delete(personId);
    }

    // Sync all matching checkboxes with same personId
    document.querySelectorAll(`.passenger-select-cb[value="${personId}"]`).forEach(cb => {
        cb.checked = checkboxEl.checked;
        const card = cb.closest(".gallery-card");
        if (card) {
            if (checkboxEl.checked) card.classList.add("selected");
            else card.classList.remove("selected");
        }
    });

    updateSelectionBar();
}

function selectAllInDate(dateStr, select) {
    const checkboxes = document.querySelectorAll(`.gallery-card[data-date="${dateStr}"] .passenger-select-cb`);
    checkboxes.forEach(cb => {
        const pid = cb.value;
        cb.checked = select;
        if (select) {
            selectedPersonIds.add(pid);
            selectedPersonCardsMap.set(pid, {
                personId: pid,
                canonicalId: cb.dataset.canonical || pid,
                cropUrl: cb.dataset.crop || "",
                angle: cb.dataset.angle || "Angle A",
                time: cb.dataset.time || ""
            });
            cb.closest(".gallery-card")?.classList.add("selected");
        } else {
            selectedPersonIds.delete(pid);
            selectedPersonCardsMap.delete(pid);
            cb.closest(".gallery-card")?.classList.remove("selected");
        }
    });
    updateSelectionBar();
}

function clearAllSelections() {
    selectedPersonIds.clear();
    selectedPersonCardsMap.clear();
    document.querySelectorAll(".passenger-select-cb").forEach(cb => {
        cb.checked = false;
        cb.closest(".gallery-card")?.classList.remove("selected");
    });
    updateSelectionBar();
}

function updateSelectionBar() {
    let bar = document.getElementById("merge-action-bar");
    const count = selectedPersonIds.size;

    if (!bar) {
        bar = document.createElement("div");
        bar.id = "merge-action-bar";
        bar.className = "merge-floating-bar";
        document.body.appendChild(bar);
    }

    if (count > 0) {
        const pidsList = Array.from(selectedPersonIds).slice(0, 5).join(", ");
        const extra = count > 5 ? ` +${count - 5} more` : '';

        bar.innerHTML = `
            <div class="floating-bar-content">
                <div class="floating-bar-info">
                    <span class="selected-count-badge">${count} Selected</span>
                    <span class="selected-ids-text">IDs: <strong>${pidsList}${extra}</strong></span>
                </div>
                <div class="floating-bar-actions">
                    <button class="btn-merge-action" onclick="openMergeModal()" ${count < 2 ? 'disabled title="Select at least 2 IDs to merge"' : ''}>
                        <i class="bi bi-union"></i> MERGE SELECTED PERSON IDs
                    </button>
                    <button class="btn-clear-selection" onclick="clearAllSelections()" title="Clear selection">
                        <i class="bi bi-x-circle"></i> Clear
                    </button>
                </div>
            </div>
        `;
        bar.classList.add("active");
    } else {
        bar.classList.remove("active");
    }
}

// -------------------------------------------------------------
// IMAGE ENLARGEMENT MODAL (Requirement 5)
// -------------------------------------------------------------

function openImageEnlargeModal(index) {
    if (index < 0 || index >= currentGalleryItems.length) return;
    currentModalImageIndex = index;
    currentZoomLevel = 1.0;

    const item = currentGalleryItems[index];
    const modal = document.getElementById("image-enlarge-modal");
    if (!modal) return;

    updateEnlargeModalContent(item);
    modal.classList.add("active");
}

function updateEnlargeModalContent(item) {
    const imgEl = document.getElementById("enlarge-modal-img");
    const placeholderEl = document.getElementById("enlarge-modal-placeholder");
    const pidEl = document.getElementById("enlarge-modal-pid");
    const canonicalEl = document.getElementById("enlarge-modal-canonical");
    const angleEl = document.getElementById("enlarge-modal-angle");
    const timeEl = document.getElementById("enlarge-modal-time");
    const busEl = document.getElementById("enlarge-modal-bus");
    const journeyEl = document.getElementById("enlarge-modal-journey");
    const detConfEl = document.getElementById("enlarge-modal-det-conf");
    const idConfEl = document.getElementById("enlarge-modal-id-conf");
    const statusEl = document.getElementById("enlarge-modal-status");
    const counterEl = document.getElementById("enlarge-modal-counter");

    if (counterEl) {
        counterEl.textContent = `${currentModalImageIndex + 1} / ${currentGalleryItems.length}`;
    }

    const modalCropUrl = formatPassengerPhotoUrl(item.crop_url || item.best_crop_url || `/data/${item.person_id}_face.jpg`);
    if (modalCropUrl) {
        imgEl.src = modalCropUrl;
        imgEl.style.display = "block";
        imgEl.style.transform = `scale(${currentZoomLevel})`;
        if (placeholderEl) placeholderEl.style.display = "none";
    } else {
        imgEl.style.display = "none";
        if (placeholderEl) {
            placeholderEl.style.display = "flex";
            placeholderEl.innerHTML = `
                <i class="bi bi-exclamation-triangle text-warning" style="font-size:42px;"></i>
                <div style="font-size:14px;font-weight:600;margin-top:8px;">Crop Image Missing from Disk</div>
                <small style="color:var(--text-muted);font-size:12px;">${item.crop_warning || 'Original crop file unavailable'}</small>
            `;
        }
    }

    if (pidEl) pidEl.textContent = item.person_id;
    if (canonicalEl) {
        if (item.is_merged && item.canonical_person_id && item.canonical_person_id !== item.person_id) {
            canonicalEl.innerHTML = `<span class="badge bg-warning text-dark"><i class="bi bi-link-45deg"></i> Alias of Master ${item.canonical_person_id}</span>`;
        } else {
            canonicalEl.innerHTML = `<span class="badge bg-success text-white"><i class="bi bi-shield-check"></i> Master Identity</span>`;
        }
    }
    if (angleEl) angleEl.textContent = item.camera_angle;
    if (timeEl) timeEl.textContent = item.observation_time;
    if (busEl) busEl.textContent = item.bus_number;
    if (journeyEl) journeyEl.textContent = item.journey_code;
    if (detConfEl) detConfEl.textContent = `${item.detection_confidence_pct}%`;
    if (idConfEl) idConfEl.textContent = item.identity_confidence_pct !== null ? `${item.identity_confidence_pct}%` : 'N/A';
    if (statusEl) statusEl.textContent = item.status;
}

function navigateEnlargeModal(direction) {
    const newIdx = currentModalImageIndex + direction;
    if (newIdx >= 0 && newIdx < currentGalleryItems.length) {
        currentModalImageIndex = newIdx;
        currentZoomLevel = 1.0;
        updateEnlargeModalContent(currentGalleryItems[newIdx]);
    }
}

function zoomEnlargeImage(factor) {
    const imgEl = document.getElementById("enlarge-modal-img");
    if (!imgEl) return;
    if (factor === 0) {
        currentZoomLevel = 1.0;
    } else {
        currentZoomLevel = Math.max(0.5, Math.min(3.5, currentZoomLevel + factor));
    }
    imgEl.style.transform = `scale(${currentZoomLevel})`;
    imgEl.style.transition = "transform 0.15s ease-out";
}

function closeImageEnlargeModal() {
    const modal = document.getElementById("image-enlarge-modal");
    if (modal) modal.classList.remove("active");
    currentZoomLevel = 1.0;
}

// -------------------------------------------------------------
// MANUAL MERGE CONFIRMATION MODAL (Requirements 6 & 7)
// -------------------------------------------------------------

function openMergeModal() {
    if (selectedPersonIds.size < 2) {
        showToast("Please select at least 2 Person IDs to merge.", "warning");
        return;
    }

    const modal = document.getElementById("merge-confirm-modal");
    if (!modal) return;

    const selectEl = document.getElementById("master-person-select");
    const comparisonGrid = document.getElementById("merge-comparison-grid");
    const countEl = document.getElementById("merge-modal-selected-count");

    if (countEl) countEl.textContent = selectedPersonIds.size;
    if (selectEl) selectEl.innerHTML = "";
    if (comparisonGrid) comparisonGrid.innerHTML = "";

    const pids = Array.from(selectedPersonIds);

    // Populate dropdown and side-by-side comparison gallery
    pids.forEach((pid, idx) => {
        // Dropdown option
        const opt = document.createElement("option");
        opt.value = pid;
        opt.textContent = `${pid} (Keep as Master Person ID)`;
        if (idx === 0) opt.selected = true;
        selectEl.appendChild(opt);

        // Side-by-side comparison card
        const cardData = selectedPersonCardsMap.get(pid) || { personId: pid, cropUrl: "", angle: "Angle A", time: "" };
        const cardDiv = document.createElement("div");
        cardDiv.className = "merge-comparison-card";
        cardDiv.innerHTML = `
            <div class="comparison-crop-box">
                ${cardData.cropUrl ? `
                    <img src="${cardData.cropUrl}" alt="${pid}">
                ` : `
                    <div class="comp-avatar">${pid.substring(0, 2)}</div>
                `}
            </div>
            <div class="comp-info">
                <strong>${pid}</strong>
                <span class="badge bg-secondary" style="font-size:10px;">${cardData.angle}</span>
                <small style="color:var(--text-muted);font-size:11px;display:block;margin-top:2px;">${cardData.time ? cardData.time.split(' ')[1] : ''}</small>
            </div>
        `;
        comparisonGrid.appendChild(cardDiv);
    });

    modal.classList.add("active");
}

function closeMergeModal() {
    const modal = document.getElementById("merge-confirm-modal");
    if (modal) modal.classList.remove("active");
}

async function executeManualMerge() {
    const masterPid = document.getElementById("master-person-select")?.value;
    const comment = document.getElementById("merge-reason-input")?.value || "";
    const pids = Array.from(selectedPersonIds);

    if (!masterPid || pids.length < 2) {
        showToast("Invalid merge selection.", "error");
        return;
    }

    const btn = document.getElementById("btn-confirm-merge");
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-border spinner-border-sm" role="status"></span> Merging...`;
    }

    try {
        const res = await fetch("/api/passengers/merge-multiple", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                selected_person_ids: pids,
                master_person_id: masterPid,
                comment: comment
            })
        });

        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.error || "Merge failed.");
        }

        showToast(data.message || `Successfully merged into Master ${masterPid}!`, "success");
        closeMergeModal();
        clearAllSelections();
        loadDateWiseReport();

    } catch (err) {
        console.error("Merge error:", err);
        showToast(err.message || "Failed to merge passenger identities.", "error");
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `<i class="bi bi-check2-circle"></i> Confirm Merge`;
        }
    }
}

// -------------------------------------------------------------
// MERGE HISTORY & UNDO MODAL (Requirement 8)
// -------------------------------------------------------------

async function openMergeHistoryModal() {
    const modal = document.getElementById("merge-history-modal");
    if (!modal) return;
    const tbody = document.getElementById("merge-history-tbody");
    if (tbody) {
        tbody.innerHTML = `<tr><td colspan="7" class="text-center py-4"><span class="spinner-border spinner-border-sm"></span> Loading merge history...</td></tr>`;
    }

    modal.classList.add("active");

    try {
        const res = await fetch("/api/passengers/merge-history");
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Failed to fetch history");

        renderMergeHistoryTable(data.records || []);
    } catch (err) {
        if (tbody) {
            tbody.innerHTML = `<tr><td colspan="7" class="text-center text-danger py-3">${err.message}</td></tr>`;
        }
    }
}

function renderMergeHistoryTable(records) {
    const tbody = document.getElementById("merge-history-tbody");
    if (!tbody) return;

    if (!records || records.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="text-center py-4" style="color:var(--text-muted)">No manual identity merges recorded yet.</td></tr>`;
        return;
    }

    tbody.innerHTML = records.map(r => {
        const isActive = r.status === "ACTIVE";
        return `
            <tr>
                <td><strong>${r.master_person_id}</strong></td>
                <td><span class="badge bg-warning text-dark"><i class="bi bi-link-45deg"></i> ${r.merged_person_id}</span></td>
                <td>${r.merge_timestamp || 'N/A'}</td>
                <td>${r.merged_by_name || 'System'}</td>
                <td><small style="color:var(--text-muted);">${r.merge_reason || 'Consolidated duplicate identity'}</small></td>
                <td>
                    ${isActive ? `
                        <span class="badge bg-success">ACTIVE</span>
                    ` : `
                        <span class="badge bg-secondary">REVERSED</span>
                    `}
                </td>
                <td style="text-align:right;">
                    ${isActive ? `
                        <button class="btn-sm btn-danger" onclick="reverseMerge(${r.id}, '${r.merged_person_id}', '${r.master_person_id}')">
                            <i class="bi bi-arrow-counterclockwise"></i> Reverse Merge
                        </button>
                    ` : `
                        <small style="color:var(--text-muted);">Reversed ${r.reversed_at ? r.reversed_at.split(' ')[0] : ''}</small>
                    `}
                </td>
            </tr>
        `;
    }).join("");
}

async function reverseMerge(recordId, aliasPid, masterPid) {
    const confirmMsg = `Are you sure you want to reverse the merge of ${aliasPid} from Master ${masterPid}?\n\nThis will restore ${aliasPid} as an independent identity and restore all its original observations without data loss.`;
    if (!confirm(confirmMsg)) return;

    const reason = prompt("Enter rationale for reversing this merge:", "Operator discovered distinct passenger identity.");
    if (reason === null) return;

    try {
        const res = await fetch(`/api/passengers/merge-reverse/${recordId}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ reason: reason })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Reversal failed.");

        showToast(data.message || `Successfully reversed merge!`, "success");
        openMergeHistoryModal();
        loadDateWiseReport();
    } catch (err) {
        showToast(err.message || "Failed to reverse merge.", "error");
    }
}

function closeMergeHistoryModal() {
    const modal = document.getElementById("merge-history-modal");
    if (modal) modal.classList.remove("active");
}

// -------------------------------------------------------------
// EXPORT HANDLERS (Requirement 11)
// -------------------------------------------------------------

function exportReport(format) {
    const params = getFilterParams();
    if (format === "csv") {
        window.location.href = `/api/passengers/export-datewise.csv?${params.toString()}`;
    } else if (format === "xlsx") {
        window.location.href = `/api/passengers/export-datewise.xlsx?${params.toString()}`;
    } else if (format === "pdf") {
        window.location.href = `/api/passengers/export-datewise.pdf?${params.toString()}`;
    }
}

// -------------------------------------------------------------
// IDENTITY PROVENANCE & MERGE LINK MODAL
// -------------------------------------------------------------

async function openProvenanceModal(personId) {
    const modal = document.getElementById("identity-provenance-modal");
    const container = document.getElementById("provenance-modal-content");
    if (!modal || !container) return;

    modal.classList.add("active");
    container.innerHTML = `
        <div style="text-align:center;padding:32px;color:var(--text-muted);">
            <div class="spinner-border spinner-border-sm text-primary" role="status" style="width:2rem;height:2rem;margin-bottom:10px;"></div>
            <div style="font-size:14px;font-weight:600;">Retrieving identity provenance for ${personId}...</div>
        </div>
    `;

    try {
        const res = await fetch(`/api/passengers/provenance/${encodeURIComponent(personId)}`);
        if (!res.ok) {
            throw new Error(`Failed to load identity provenance: ${res.statusText}`);
        }
        const data = await res.json();
        renderProvenanceContent(data);
    } catch (err) {
        console.error("Provenance error:", err);
        container.innerHTML = `
            <div style="padding:20px;text-align:center;color:#ef4444;">
                <i class="bi bi-exclamation-triangle" style="font-size:32px;"></i>
                <div style="font-weight:600;margin-top:8px;">Could not load identity provenance</div>
                <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">${err.message}</div>
            </div>
        `;
    }
}

function renderProvenanceContent(data) {
    const container = document.getElementById("provenance-modal-content");
    if (!container) return;

    const isMerged = data.is_merged;
    const prevId = data.summary ? (data.summary.previous_id || data.person_id) : data.person_id;
    const newMasterId = data.summary ? (data.summary.new_id || data.canonical_person_id) : data.canonical_person_id;
    const aliases = data.active_aliases || [];
    const transitions = data.transitions || [];

    let flowHtml = '';
    if (isMerged && prevId !== newMasterId) {
        flowHtml = `
            <div style="background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.3);border-radius:10px;padding:16px;margin-bottom:18px;">
                <div style="font-size:11px;font-weight:700;color:#b45309;text-transform:uppercase;margin-bottom:8px;display:flex;align-items:center;gap:6px;">
                    <i class="bi bi-link-45deg" style="font-size:16px;"></i> Identity Merge Provenance Link
                </div>
                <div style="display:flex;align-items:center;justify-content:center;gap:14px;flex-wrap:wrap;padding:8px 0;">
                    <div style="background:#ffffff;border:2px solid #f59e0b;border-radius:8px;padding:10px 16px;text-align:center;box-shadow:0 2px 6px rgba(0,0,0,0.06);">
                        <div style="font-size:11px;color:var(--text-muted);font-weight:600;">Previous Person ID</div>
                        <div style="font-size:18px;font-weight:800;color:#b45309;">${prevId}</div>
                    </div>
                    <div style="display:flex;flex-direction:column;align-items:center;color:#b45309;">
                        <span style="font-size:11px;font-weight:700;background:rgba(245,158,11,0.2);padding:2px 8px;border-radius:12px;margin-bottom:2px;">MERGED INTO</span>
                        <i class="bi bi-arrow-right" style="font-size:22px;font-weight:bold;"></i>
                    </div>
                    <div style="background:#ffffff;border:2px solid #10b981;border-radius:8px;padding:10px 16px;text-align:center;box-shadow:0 2px 6px rgba(0,0,0,0.06);">
                        <div style="font-size:11px;color:var(--text-muted);font-weight:600;">New Master Person ID</div>
                        <div style="font-size:18px;font-weight:800;color:#047857;">${newMasterId}</div>
                    </div>
                </div>
                <div style="text-align:center;margin-top:10px;font-size:12px;color:#92400e;font-weight:600;">
                    Previous ID <strong>${prevId}</strong> was manually merged and changed to Master ID <strong>${newMasterId}</strong>.
                </div>
            </div>
        `;
    } else if (aliases.length > 0) {
        flowHtml = `
            <div style="background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.3);border-radius:10px;padding:16px;margin-bottom:18px;">
                <div style="font-size:11px;font-weight:700;color:#047857;text-transform:uppercase;margin-bottom:8px;display:flex;align-items:center;gap:6px;">
                    <i class="bi bi-shield-check" style="font-size:16px;"></i> Master Canonical Identity
                </div>
                <div style="display:flex;align-items:center;justify-content:center;gap:14px;flex-wrap:wrap;padding:8px 0;">
                    <div style="background:#ffffff;border:2px solid #10b981;border-radius:8px;padding:10px 18px;text-align:center;box-shadow:0 2px 6px rgba(0,0,0,0.06);">
                        <div style="font-size:11px;color:var(--text-muted);font-weight:600;">Primary Master Person ID</div>
                        <div style="font-size:20px;font-weight:800;color:#047857;">${newMasterId}</div>
                    </div>
                </div>
                <div style="margin-top:10px;text-align:center;">
                    <span style="font-size:12px;color:var(--text-muted);">Consolidated from previous duplicate IDs:</span>
                    <div style="display:flex;gap:6px;justify-content:center;flex-wrap:wrap;margin-top:6px;">
                        ${aliases.map(a => `<span class="badge bg-warning text-dark" style="font-size:11px;padding:4px 8px;"><i class="bi bi-link-45deg"></i> ${a}</span>`).join("")}
                    </div>
                </div>
            </div>
        `;
    } else {
        flowHtml = `
            <div style="background:rgba(59,130,246,0.08);border:1px solid rgba(59,130,246,0.3);border-radius:10px;padding:16px;margin-bottom:18px;text-align:center;">
                <div style="font-size:11px;font-weight:700;color:#1e40af;text-transform:uppercase;margin-bottom:6px;">
                    <i class="bi bi-person-badge"></i> Independent Master Identity
                </div>
                <div style="font-size:22px;font-weight:800;color:var(--text-main);">${data.person_id}</div>
                <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">
                    This passenger has no merged aliases and operates as a standalone canonical identity.
                </div>
            </div>
        `;
    }

    let transitionsHtml = '';
    if (transitions.length > 0) {
        transitionsHtml = `
            <div style="margin-top:14px;">
                <div style="font-size:12px;font-weight:700;color:var(--text-main);margin-bottom:8px;">
                    <i class="bi bi-clock-history"></i> Merge Audit Logs:
                </div>
                <div style="max-height:160px;overflow-y:auto;display:flex;flex-direction:column;gap:8px;">
                    ${transitions.map(t => `
                        <div style="background:var(--bg-app);border:1px solid var(--border-color);border-radius:6px;padding:8px 12px;font-size:12px;">
                            <div style="display:flex;justify-content:space-between;align-items:center;">
                                <strong>${t.previous_person_id} &rarr; ${t.new_master_person_id}</strong>
                                <span class="badge ${t.status === 'ACTIVE' ? 'bg-success' : 'bg-secondary'}" style="font-size:10px;">${t.status}</span>
                            </div>
                            <div style="color:var(--text-muted);font-size:11px;margin-top:2px;">
                                ${t.merge_timestamp || ''} &bull; Reviewer: ${t.operator_name}
                            </div>
                            ${t.reason ? `<div style="font-style:italic;color:var(--text-main);margin-top:3px;">"${t.reason}"</div>` : ''}
                        </div>
                    `).join("")}
                </div>
            </div>
        `;
    }

    container.innerHTML = `
        ${flowHtml}

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;background:var(--bg-card-hover);padding:12px;border-radius:8px;border:1px solid var(--border-color);font-size:12px;margin-bottom:14px;">
            <div>
                <span style="color:var(--text-muted);display:block;font-size:11px;">Current Query ID:</span>
                <strong>${data.person_id}</strong>
            </div>
            <div>
                <span style="color:var(--text-muted);display:block;font-size:11px;">Canonical Master ID:</span>
                <strong style="color:#10b981;">${data.canonical_person_id}</strong>
            </div>
            <div>
                <span style="color:var(--text-muted);display:block;font-size:11px;">Identity Classification:</span>
                <span>${isMerged ? 'Merged Alias' : 'Canonical Master'}</span>
            </div>
            <div>
                <span style="color:var(--text-muted);display:block;font-size:11px;">Presence Status:</span>
                <span>${data.status}</span>
            </div>
        </div>

        ${transitionsHtml}

        <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px;">
            <button class="btn-secondary" onclick="closeProvenanceModal()">
                Close
            </button>
            <button class="btn-primary" onclick="closeProvenanceModal(); document.getElementById('search-canonical-id').value='${data.canonical_person_id}'; loadDateWiseReport();">
                <i class="bi bi-funnel"></i> View All Observations of Master ${data.canonical_person_id}
            </button>
        </div>
    `;
}

function closeProvenanceModal() {
    const modal = document.getElementById("identity-provenance-modal");
    if (modal) modal.classList.remove("active");
}

function resetFilters() {
    const filterIds = [
        "filter-date", "filter-start-date", "filter-end-date",
        "filter-bus", "filter-journey", "filter-angle",
        "search-person-id", "search-canonical-id", "filter-status"
    ];
    filterIds.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = "";
    });
    loadDateWiseReport();
}

// Toast notification helper
function showToast(message, type = "info") {
    let toastContainer = document.getElementById("toast-container");
    if (!toastContainer) {
        toastContainer = document.createElement("div");
        toastContainer.id = "toast-container";
        toastContainer.style.cssText = "position:fixed;bottom:24px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:10px;";
        document.body.appendChild(toastContainer);
    }

    const toast = document.createElement("div");
    let bg = "#1e293b";
    let icon = "bi-info-circle";
    if (type === "success") { bg = "#10b981"; icon = "bi-check-circle-fill"; }
    else if (type === "error") { bg = "#ef4444"; icon = "bi-x-circle-fill"; }
    else if (type === "warning") { bg = "#f59e0b"; icon = "bi-exclamation-triangle-fill"; }

    toast.style.cssText = `background:${bg};color:#ffffff;padding:12px 18px;border-radius:8px;box-shadow:0 4px 14px rgba(0,0,0,0.18);display:flex;align-items:center;gap:10px;font-size:13px;font-weight:600;min-width:260px;animation:slideIn 0.25s ease-out;`;
    toast.innerHTML = `<i class="bi ${icon}"></i> <span>${message}</span>`;
    toastContainer.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = "0";
        toast.style.transition = "opacity 0.3s ease";
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}
