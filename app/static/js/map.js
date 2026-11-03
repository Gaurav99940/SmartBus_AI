// GPS Map with Leaflet
let gpsMap = null;
let routePolyline = null;
let markersLayer = null;

document.addEventListener("DOMContentLoaded", () => {
    initLeafletMap();
    loadMapJourneys();
    loadMapPoints();
});

function initLeafletMap() {
    const mapEl = document.getElementById("map-container");
    if (!mapEl) return;

    gpsMap = L.map("map-container").setView([37.7749, -122.4194], 13);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution: '&copy; <a href="https://openstreetmap.org">OpenStreetMap</a> contributors'
    }).addTo(gpsMap);

    markersLayer = L.layerGroup().addTo(gpsMap);
}

async function loadMapJourneys() {
    try {
        const res = await fetch("/api/journeys");
        if (!res.ok) return;
        const journeys = await res.json();
        const select = document.getElementById("map-filter-journey");
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

async function loadMapPoints() {
    const journeyId = document.getElementById("map-filter-journey")?.value || "";
    const url = new URL("/api/map/points", window.location.origin);
    if (journeyId) url.searchParams.append("journey_id", journeyId);

    try {
        const res = await fetch(url);
        if (!res.ok) return;
        const data = await res.json();

        const emptyNotice = document.getElementById("map-empty-state");
        if (!data.has_gps_data) {
            if (emptyNotice) emptyNotice.style.display = "block";
            if (markersLayer) markersLayer.clearLayers();
            if (routePolyline) gpsMap.removeLayer(routePolyline);
            return;
        }

        if (emptyNotice) emptyNotice.style.display = "none";
        renderMapPoints(data.route_points, data.passenger_markers);

    } catch (e) {
        console.error("Failed to load map points:", e);
    }
}

function renderMapPoints(points, passengers) {
    if (!gpsMap || !markersLayer) return;
    markersLayer.clearLayers();
    if (routePolyline) gpsMap.removeLayer(routePolyline);

    const latlngs = [];

    // Custom bus stop / photo marker icon
    const busIcon = L.divIcon({
        className: 'custom-bus-marker',
        html: `<div style="background:#2563eb;color:#fff;width:32px;height:32px;border-radius:50%;border:2px solid #fff;box-shadow:0 2px 6px rgba(0,0,0,0.3);display:flex;align-items:center;justify-content:center;font-size:14px;"><i class="bi bi-camera-fill"></i></div>`,
        iconSize: [32, 32],
        iconAnchor: [16, 16]
    });

    points.forEach((pt, idx) => {
        const latlng = [pt.latitude, pt.longitude];
        latlngs.push(latlng);

        const popupContent = `
            <div style="font-family:-apple-system, sans-serif; font-size:13px; line-height:1.4;">
                <h4 style="margin:0 0 4px 0;font-size:14px;">Photo #${pt.upload_id} - ${pt.journey_code}</h4>
                <div><strong>Timestamp:</strong> ${pt.recorded_at}</div>
                <div><strong>Bus:</strong> ${pt.bus_number}</div>
                <div><strong>GPS Source:</strong> ${pt.location_source}</div>
                <div><strong>Passengers Detected:</strong> ${pt.passengers_detected}</div>
                ${pt.annotated_url ? `<div style="margin-top:6px;"><img src="${pt.annotated_url}" style="width:100%;max-width:200px;border-radius:6px;display:block;" alt="HUD"></div>` : ''}
            </div>
        `;

        const marker = L.marker(latlng, { icon: busIcon }).bindPopup(popupContent);
        markersLayer.addLayer(marker);
    });

    // Draw trajectory polyline connecting the uploads in chronological order
    if (latlngs.length > 1) {
        routePolyline = L.polyline(latlngs, {
            color: '#2563eb',
            weight: 4,
            opacity: 0.8,
            dashArray: '6, 6'
        }).addTo(gpsMap);

        gpsMap.fitBounds(routePolyline.getBounds(), { padding: [50, 50] });
    } else if (latlngs.length === 1) {
        gpsMap.setView(latlngs[0], 14);
    }
}
