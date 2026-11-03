# Smart Bus Passenger Tracking & Attendance AI System

A complete full-stack, AI-powered system designed for bus transit operations. The system supports multi-bus fleets with specialized **Operator** and **Admin** experiences:
- **Operators**: Register/select assigned buses, broadcast live device GPS telemetry via browser Geolocation API (`watchPosition`), upload interior bus photos captured approximately every minute, and track visible passengers using deep learning face recognition.
- **Admins**: Monitor fleet-wide operations without cluttered charts—featuring a real-time Leaflet bus map with pulsing status markers, 8 database-driven KPI summary cards, a prominent Bus Selector, an active buses table, a live passenger manifest for the selected vehicle, and an immutable audit log.

---

## Key Features

### 1. Operator Onboarding & Bus Registration
- **First-Time Setup & Onboarding**: Operators without an assigned bus are guided to register their vehicle or select an authorized existing bus.
- **Duplicate-Safe Registry**: Validates vehicle registration numbers (e.g. `SF-7829-AI`). If a bus is already registered, an authorized operator can claim/select it without creating duplicate records.
- **Multi-Bus Support**: Operators authorized to operate multiple vehicles can switch their active bus at any time via the UI or API (`POST /api/buses/<id>/select`).
- **Comprehensive Bus Metadata**: Registration number, fleet ID, display name, bus type (Standard Transit, Electric Bus, Double Decker, Articulated, Minibus, Coach), capacity, route number, starting point, destination, operator name, contact number, and operational status (Active, Maintenance, Inactive).

### Automated Multi-Angle Video Processing & Timetable Stop Matching
- **Local Source Folder Selection**: Native Windows folder picker or secure path entry for Angle A, B, and C clip directories. Preserves original video and photo files as strictly read-only.
- **Image & Video Compatibility**: Dropzones and processing engine accept both high-res photos and video formats (`.mp4`, `.avi`, `.mov`, `.mkv`, etc.).
- **Stop Timetable Parsing**: Drag-and-drop support for Excel (`.xlsx`, `.xls`) and CSV (`.csv`) timetables with flexible column mapping and tolerance buffer configuration.
- **Automated Stop-Wise Synchronization**: Automatically determines which clips overlap with each stop's IN/OUT or arrival/departure window across Angles A, B, and C.
- **Sequential Background Processing**: Resumable queue with Start, Pause, Resume, Cancel, and Retry Failed operations without freezing the server.
- **Stop Coverage Categorization**: Accurately classifies every stop as **Fully Covered** (all 3 angles processed), **Partially Covered**, **Not Covered**, or **Needs Review**.
- **Certified Reporting & Exports**: Stop-wise passenger count and coverage reports available in CSV and Excel (`.xlsx`), alongside full media processing audit manifests.

### 2. Operator GPS Location Access & Telemetry
- **Explicit Permission Card**: Clean UI card prompting for location access with clear options: **Enable Location** or **Enter Location Manually**.
- **Device-Reported Telemetry**: Uses `navigator.geolocation.watchPosition()` with configurable intervals. Clearly labels coordinates as device-reported (operator inside bus) rather than fabricating bus movement.
- **Location Freshness Engine**:
  - **Live**: Coordinates received within the past 2 minutes (pulsing green marker).
  - **Stale**: Last update older than 2 minutes (yellow indicator).
  - **Offline**: No recent coordinates available (neutral indicator).
  - **Manual Location**: Coordinates manually entered (labeled explicitly, never spoofed as live GPS).
  - **Permission Denied**: Clear indicator and fallback to manual coordinates.
- **Secure Backend API**: Coordinates, timestamp, accuracy, and source logged to `bus_location_logs` and updated on the `buses` record.

### 3. Redesigned Admin Monitoring Dashboard (Graph-Free)
- **Focused Fleet Supervision**: Photo upload feature removed from Admin interface (exclusive to Operators).
- **No Cluttered Charts**: Replaced generic graphs with high-density operational telemetry.
- **Prominent Bus Selector**: Top dropdown allowing the admin to inspect "All Active Fleet" or drill down into a specific vehicle.
- **8 Database-Driven KPI Summary Cards**:
  1. Total Registered Buses
  2. Active Buses
  3. Buses with Recent GPS (Live Telemetry)
  4. Total Passengers Identified
  5. Currently Confirmed Present
  6. Total Confirmed Boardings
  7. Total Confirmed Exits
  8. Passengers Needing Verification
- **Interactive Leaflet Fleet Map**: Displays all active buses with real coordinates, route details, current occupancy, and live/stale/offline/manual markers. Clicking any marker centers the view and populates bus details.
- **Interactive 3D Bus Cabin & Seat Occupancy Map**:
  - Full visual 3D model of the interior cabin showing driver cockpit, front passenger door, 10 seating rows (Left A/B, Center Aisle, Right C/D, and Rear Bench).
  - Dynamic Passenger Positioning: Real-time seats display seated passenger avatars, face crops, Person IDs (`P001`, `P002`...), and status colors (Green for `PRESENT`, Yellow for `POSSIBLY_ABSENT`, Orange for `EXIT_UNCONFIRMED`, Neutral for Vacant).
  - Seat Independence & Movements: When passengers change seats across photo captures or are reassigned, the 3D cabin updates their seating position dynamically.
  - Interactive Perspective: 3D Isometric View vs 2D Top View toggle, orbit tilt, and rotation controls.
  - Click-to-Inspect & Reassign: Click any seat to pop up passenger details and reassign them to another available seat with `POST /api/passengers/<id>/seat`.
- **Selected Bus Details Panel**: Shows registration number, driver, route, capacity progress bar, and a live passenger manifest table (Seat, Person ID, First Seen / Boarding, Last Seen, Status, Confidence).
- **Active Buses Table**: Fleet list with real-time passenger counts, journey IDs, and GPS freshness badges.
- **Recent Activity Feed**: Chronological log of passenger boarding/exit events, photo inferences, and GPS updates.

### 4. Storage Analysis & Historical Data Cleanup
- **Storage Consumption Breakdown**: Visual meters for Photo Uploads (MB), Face Crops (MB), SQLite Database (MB), and Total Reclaimable Disk Space.
- **Historical Transit Analysis**: Tabular breakdown of past journeys with dates, routes, photo counts, passenger counts, and physical disk usage.
- **Selective & Bulk Data Purging**:
  - Purge completed journeys older than 7, 15, 30, or 60 days.
  - Purge all completed journeys regardless of age.
  - Delete individual historical journeys on demand.
  - Physical File Unlinking: Reclaims disk space by securely removing original photos and face crops from `data/uploads/` and `data/crops/`.
  - Optional Audit Trail Pruning: Purges older security log entries.
  - Audit Logged: Every cleanup event is recorded in the immutable security audit log (`CLEANUP_HISTORICAL_DATA`).

### 5. AI Face Recognition & Persistent Identity Tracking
- **Detector & Embedding Pipeline**: Detects faces using **MTCNN** and generates 512-dimensional embeddings via **InceptionResnetV1 (FaceNet/VGGFace2)**.
- **Seat Independence**: Passengers can change seats, turn, or move across the aisle; matching is strictly facial-embedding-based with 1-to-1 matching constraints per photo.
- **Temporal Presence State Machine**:
  - If a passenger is missing from one photo, they are marked **`POSSIBLY_ABSENT`** (temporarily occluded).
  - If missing for 3 consecutive uploads (or 5 minutes), state transitions to **`EXIT_UNCONFIRMED`**.
  - Operator manual review can confirm an exit, transitioning to **`EXIT_CONFIRMED`**.
  - First observation recorded as provisional boarding.

### 6. UI Theme & Privacy Compliance
- Fully responsive layout with persistent **Light Mode** and **Dark Mode** toggle.
- Tamper-evident `audit_logs` tracking logins, registrations, reviews, and config changes.
- Automated data retention cleanup and privacy-compliant CSV exports.

---

## System Architecture

```
d:\tracking\
├── app/
│   ├── __init__.py          # Flask app factory, role-based page routing, auto-migrations
│   ├── config.py            # Environment configuration & threshold defaults
│   ├── database.py          # SQLAlchemy initialization & schema migration runner
│   ├── models.py            # Relational models (Bus, User, OperatorBusAssignment, etc.)
│   ├── ai/
│   │   ├── face_pipeline.py # MTCNN detector, InceptionResnetV1, HUD bounding boxes
│   │   ├── matcher.py       # Cosine similarity, 1-to-1 matching, person ID generator
│   │   ├── quality.py       # Face size, blur (Laplacian variance), and lighting checks
│   │   └── generator.py     # Synthetic bus interior photo generator for testing
│   ├── services/
│   │   ├── exif_service.py  # EXIF parsing, GPS conversion & SHA-256 hash
│   │   ├── journey_service.py # Presence/Absence state machine & reviews
│   │   ├── audit_service.py # Security audit logger
│   │   └── export_service.py# CSV report generator
│   ├── routes/
│   │   ├── auth.py          # Auth, session, role redirects, and consent
### 7. Multi-Angle Bus Passenger Detection & Counting
- **Tri-Angle Coverage**: Upload up to 3 interior camera angles per capture session:
  - **Angle A**: Front / Assigned Seating Section (Visual reference standard)
  - **Angle B**: Middle / Assigned Seating Section
  - **Angle C**: Rear / Assigned Seating Section
- **Seating Area Configuration**: Operators and admins can customize seating area labels, target capacity (defaulting to 4 seats per angle), and region-of-interest (ROI) bounding coordinates via visual modal and REST API (`/api/uploads/seating-config`).
- **Hybrid AI Detection Pipeline**:
  - Pretrained **YOLOv8** person detector (`yolov8n.pt`, Class 0) detects visible passengers even if faces are turned away, occluded by headrests, or blurred. Fallback to OpenCV HOG person detection if needed.
  - Image quality assessment checks lighting variance and Laplacian blur.
  - **FaceNet MTCNN + InceptionResnetV1** face recognition extracts biometrics for visible faces to maintain persistent `P001`, `P002`... IDs across journeys.
  - Body detections without recognizable faces are retained as temporary observations (`T-{angle}-{index}`) under "Needs Review" status without fabricating identities or confidence scores.
- **Cross-Angle Duplicate Handling & Counting**:
  - Independent angle counts calculated directly from model inferences (never hardcoded or blindly summed).
  - Cautious biometric duplicate deduplication across simultaneous angles: matches confirmed passengers without double-counting; flags ambiguous or unverified overlaps as provisional duplicates for operator review.
  - Multi-angle presence state engine: single-angle absence does NOT trigger premature passenger exit; only multi-angle consensus or confirmed operator action transitions presence.
- **Annotated HUD Previews & Review Workflow**:
  - Visual HUD previews with color-coded bounding boxes:
    - **Confirmed Identity**: Emerald Green (`#10b981`)
    - **Unidentified Passenger**: Sky Blue (`#0ea5e9`)
    - **Needs Review**: Crimson Red (`#ef4444`)
    - **Low Confidence**: Amber (`#f59e0b`)
  - Operator review modal allows confirming, marking false positives, or manually linking detections to existing passengers with full audit trail logging.

### 8. Interactive Manual Bounding Box Correction Tool
- **Canvas Overlay**: Integrated HTML5 `<canvas>` layer directly superimposed on bus interior images.
- **Draw Box**: Click and drag to create new bounding boxes over passengers missed by automated AI detection.
- **Select / Move / Resize**: 8-handle interactive box resizing and position adjustment.
- **Instant Table Synchronization**:
  - Automatically appends a new record to the right-side detections table (`P_MANUAL_1`, `User-Added`, `Manual 100%`).
  - Offscreen canvas generates instantaneous face/body thumbnail crops in real time.
- **Save Correction API**: `POST /api/uploads/manual-annotation` submits updated bounding box coordinates `[x_min, y_min, x_max, y_max]`, image ID, camera angle, and base64 crop, persisting `PassengerObservation` and updating passenger count metrics.

### 9. Bus Speed Timeline (0-15 km/h) & Passenger Trajectory
- **Dedicated Route & Timeline Page**: Located at `/route-timeline` and accessible to Operators and Admins.
- **31-Stop Transit Telemetry**: Pre-seeded with real transit stops and vehicle speeds (0-15 km/h) along the Kaushambi - Sahibabad - Vaishali route in Ghaziabad.
- **Manual Stop Management**: Operators and admins can manually add intermediate or terminal stops (`POST /api/route-stops`) with arrival time, speed (km/h), and coordinates.
- **Green vs Red Passenger Trajectory**:
  - For each detected passenger (`P001`, `P002`, `P_MANUAL_1`...):
    - **GREEN Line (`#10b981`)**: Route segment where the passenger traveled (from Boarding Stop to Exit / Current Stop).
    - **RED Line (`#ef4444`)**: Route segments where the passenger did NOT travel (prior to boarding or after alighting).
  - Individual passenger carousel allows toggling between passengers to illuminate their personalized travel trajectory on both the interactive Leaflet map and the sequential stop-by-stop progress track.

---

## System Architecture

```
d:\tracking\
├── app/
│   ├── __init__.py          # Flask app factory, role-based page routing, auto-migrations
│   ├── config.py            # Environment configuration & threshold defaults
│   ├── database.py          # SQLAlchemy initialization & schema migration runner
│   ├── models.py            # Relational models (Bus, RouteStop, User, CameraAngleConfig, etc.)
│   ├── ai/
│   │   ├── face_pipeline.py # MTCNN detector, InceptionResnetV1, HUD bounding boxes
│   │   ├── person_pipeline.py # YOLOv8 person detector, blur/light checks, multi-angle HUD
│   │   ├── matcher.py       # Cosine similarity, 1-to-1 matching, person ID generator
│   │   ├── quality.py       # Face size, blur (Laplacian variance), and lighting checks
│   │   └── generator.py     # Synthetic bus interior photo generator for testing
│   ├── services/
│   │   ├── exif_service.py  # EXIF parsing, GPS conversion & SHA-256 hash
│   │   ├── journey_service.py # Multi-angle batch processing, presence states & reviews
│   │   ├── audit_service.py # Security audit logger
│   │   └── export_service.py# CSV report generator
│   ├── routes/
│   │   ├── auth.py          # Auth, session, role redirects, and consent
│   │   ├── buses.py         # Fleet registry, bus selection, GPS telemetry reporting
│   │   ├── dashboard.py     # Admin & operator dashboard stats, activity feed
│   │   ├── uploads.py       # Multi-angle photo upload, manual annotation, seating config
│   │   ├── route_stops.py   # Route stops, speed timeline (0-15 km/h), passenger trajectory
│   │   ├── passengers.py    # Directory, timeline modal, manual reviews
│   │   ├── journeys.py      # Bus & journey lifecycle management
│   │   ├── map.py           # GPS map points & route polyline
│   │   ├── reports.py       # CSV report download
│   │   └── settings.py      # AI thresholds, audit logs, demo runner
│   ├── static/
│   │   ├── css/style.css    # Custom responsive styles, pulsing markers, dark theme
│   │   └── js/              # Client scripts (upload.js, route_timeline.js, etc.)
│   └── templates/           # Templates (upload.html, route_timeline.html, etc.)
├── data/                    # Storage for SQLite DB, uploads, and face crops
├── tests/                   # 49 automated unit tests
├── run.py                   # Server startup runner
├── requirements.txt         # Python dependencies
└── .env.example             # Configuration template
```

---

## Database Tables

1. **`users`**: Operator and administrator credentials, role (`admin` or `operator`), `current_bus_id`, consent flags.
2. **`buses`**: Transit fleet records (`registration_number`, `bus_number`, `bus_name`, `bus_type`, `capacity`, `route_number`, `starting_point`, `destination`, `operator_name`, `operator_contact`, `bus_status`, `latest_latitude`, `latest_longitude`, `latest_gps_accuracy`, `latest_location_source`, `latest_location_time`).
3. **`operator_bus_assignments`**: M-to-N relationships between operators and authorized fleet vehicles.
4. **`bus_location_logs`**: Historical GPS telemetry stream with coordinates, accuracy, and source (`DEVICE_API` or `MANUAL_ENTRY`).
5. **`journeys`**: Active and completed journey sessions.
6. **`uploads`**: Immutable photo upload records (capture timestamp, SHA-256 hash, GPS coordinates, source, `camera_angle`, `batch_id`, `uncertain_detections_count`, `error_details`).
7. **`passenger_identities`**: Persistent journey-scoped passenger IDs (`P001`, `P002`, ...), biometric vectors, status, boarding stop, exit stop, first/last seen locations.
8. **`passenger_observations`**: Per-photo detection instances with face/body bounding boxes, detector confidence, similarity score, crop path, `camera_angle`, `temp_detection_id`, `review_status`, and `is_potential_duplicate`.
9. **`camera_angle_configs`**: Persisted configuration for interior angles (A, B, C) with display names, seating region labels, target seat capacity, and ROI coordinates.
10. **`route_stops`**: Sequential bus route stops with speed telemetry (0-15 km/h), time window, location address, and GPS coordinates.
11. **`presence_events`**: Chronological state transitions (`BOARDING_PROVISIONAL`, `PRESENT_CONFIRMED`, `POSSIBLY_ABSENT`, `EXIT_UNCONFIRMED`, `EXIT_CONFIRMED`, `MANUAL_OVERRIDE`).
12. **`gps_locations`**: Geographic coordinates linked to photo uploads.
13. **`manual_reviews`**: Operator review audit entries.
14. **`audit_logs`**: System audit trail capturing user IP, actions, and entities.

---

## Quick Start Guide (Windows PowerShell)

### 1. Prerequisites
- Python 3.11, 3.12, or 3.13 installed on Windows.
- PowerShell terminal.

### 2. Install Dependencies
```powershell
cd d:\tracking
pip install -r requirements.txt
```

### 3. Run Automated Tests
Verify all 49 unit tests (manual box annotations, route stops, speed telemetry, passenger trajectories, multi-angle uploads, person detection, bus registration, 3D seats, data cleanup, GPS freshness, AI matching, state machine):
```powershell
python -m unittest discover tests
```

### 4. Start the Application
```powershell
python run.py
```
Output:
```
================================================================
 Smart Bus Passenger Tracking & Attendance AI System
 Server running at: http://127.0.0.1:5000
 Admin credentials:    admin / admin123
 Operator credentials: operator / operator123
================================================================
```

### 5. Access the Web Dashboard
Open your web browser and navigate to:
```
http://127.0.0.1:5000
```

---

## Default User Accounts

| Username | Password | Role | Permissions |
| :--- | :--- | :--- | :--- |
| **`operator`** | `operator123` | Operator | Register/select bus, broadcast device GPS, upload multi-angle photos, manual box annotations, view route timeline, manual reviews |
| **`admin`** | `admin123` | Admin | Fleet-wide monitoring, Bus Selector, Leaflet bus map, route stops & speed timeline, configure seating areas, assign operators, tune AI thresholds, view audit logs |

---

## REST API Reference

| Endpoint | Method | Role | Description |
| :--- | :--- | :--- | :--- |
| `/api/auth/login` | `POST` | Public | Authenticate operator or administrator |
| `/api/auth/me` | `GET` | User | Get current user profile, role, and assigned bus |
| `/api/buses` | `GET` | User | List fleet buses with operational and passenger stats |
| `/api/buses` | `POST` | User | Register a new bus or claim an existing bus (duplicate-safe) |
| `/api/buses/<id>` | `GET` | User | Get detailed bus information, active journey, and passenger manifest |
| `/api/buses/<id>/select` | `POST` | Operator | Switch operator's active assigned bus |
| `/api/buses/operator/my-buses` | `GET` | Operator | Get list of buses assigned to the current operator |
| `/api/buses/operator/location` | `POST` | Operator | Report device GPS or manual location telemetry |
| `/api/buses/operators` | `GET` | Admin | List all operators and their assigned vehicles |
| `/api/buses/operators/assign` | `POST` | Admin | Assign an operator to a fleet bus |
| `/api/dashboard/stats` | `GET` | User | Fleet-wide or bus-filtered KPIs, active buses, and activity feed |
| `/api/uploads/multi-angle` | `POST` | Operator | Upload up to 3 camera angles (A, B, C) in a batch and run AI detection |
| `/api/uploads/manual-annotation` | `POST` | Operator | Save manually drawn passenger bounding box coordinates and crop |
| `/api/uploads/observations/<id>` | `DELETE` | Operator | Delete an observation or manual bounding box |
| `/api/uploads/seating-config` | `GET`/`POST` | User | Get or update camera angle seating labels, capacity, and ROI coords |
| `/api/uploads/observations/<id>/review` | `POST` | Operator | Review detection (confirm, false positive, link identity) |
| `/api/uploads` | `POST` | Operator | Upload single bus interior photo and execute AI inference |
| `/api/route-stops` | `GET` | User | List sequential route stops, speed telemetry (0-15 km/h), and route summary |
| `/api/route-stops` | `POST` | User | Manually add an intermediate or terminal stop to the bus route |
| `/api/route-stops/<id>` | `DELETE` | User | Remove a stop from the route and re-order sequence |
| `/api/route-stops/passenger-trajectories` | `GET` | User | Get passenger travel trajectories (Green traveled vs Red not traveled) |
| `/api/route-stops/passenger/<id>/stops` | `POST` | User | Manually assign or override a passenger's boarding and exit stop |
| `/api/passengers` | `GET` | User | Filter journey passengers by status/ID |
| `/api/passengers/<id>/review` | `POST` | Operator | Manual review (confirm exit, mark present) |
| `/api/passengers/<id>/seat` | `POST` | User | Reassign/move passenger to another seat on the 3D cabin |
| `/api/map/points` | `GET` | User | GPS coordinates for Leaflet route mapping |
| `/api/reports/export.csv` | `GET` | User | Download privacy-compliant CSV attendance report |
| `/api/settings/thresholds` | `GET`/`POST` | Admin | View and tune cosine similarity thresholds |
| `/api/settings/data-analysis` | `GET` | Admin | Storage consumption breakdown and historical journeys list |
| `/api/settings/data-cleanup` | `POST` | Admin | Purge old/completed transit data, unlink files, free disk space |
| `/api/settings/run-demo-simulation` | `POST` | User | Trigger automated synthetic journey test |
| `/api/settings/audit-logs` | `GET` | Admin | View tamper-evident system audit logs |

