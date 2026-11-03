from datetime import datetime
import json
from werkzeug.security import generate_password_hash, check_password_hash
from app.database import db

class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default="operator", nullable=False) # 'admin', 'operator', 'auditor'
    full_name = db.Column(db.String(120), nullable=True)
    consent_accepted = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, nullable=True)

    current_bus_id = db.Column(db.Integer, nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "role": self.role,
            "full_name": self.full_name,
            "consent_accepted": self.consent_accepted,
            "current_bus_id": self.current_bus_id,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


class Bus(db.Model):
    __tablename__ = "buses"

    id = db.Column(db.Integer, primary_key=True)
    bus_number = db.Column(db.String(50), unique=True, nullable=False, index=True) # e.g. "BUS-101"
    registration_number = db.Column(db.String(60), unique=True, nullable=True, index=True) # e.g. "SF-7829-AI"
    license_plate = db.Column(db.String(50), nullable=True)
    bus_name = db.Column(db.String(100), nullable=True)
    bus_type = db.Column(db.String(50), default="Standard Transit")
    capacity = db.Column(db.Integer, default=40)
    route_name = db.Column(db.String(120), nullable=True)
    route_number = db.Column(db.String(50), nullable=True)
    starting_point = db.Column(db.String(100), nullable=True)
    destination = db.Column(db.String(100), nullable=True)
    operator_name = db.Column(db.String(120), nullable=True)
    operator_contact = db.Column(db.String(50), nullable=True)
    bus_status = db.Column(db.String(30), default="Active") # 'Active', 'Inactive', 'Maintenance'
    is_active = db.Column(db.Boolean, default=True)

    assigned_operator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    # Latest reported GPS location
    latest_latitude = db.Column(db.Float, nullable=True)
    latest_longitude = db.Column(db.Float, nullable=True)
    latest_gps_accuracy = db.Column(db.Float, nullable=True)
    latest_location_source = db.Column(db.String(30), nullable=True) # 'DEVICE_API', 'MANUAL_ENTRY', 'EXIF'
    latest_location_time = db.Column(db.DateTime, nullable=True)
    last_activity_time = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    journeys = db.relationship("Journey", backref="bus", lazy=True, cascade="all, delete-orphan")
    assigned_operator = db.relationship("User", foreign_keys=[assigned_operator_id])

    def get_location_freshness(self):
        if self.latest_latitude is None or self.latest_longitude is None:
            return "Offline"
        if self.latest_location_source == "MANUAL_ENTRY":
            return "Manual Location"
        if not self.latest_location_time:
            return "Offline"
        delta = (datetime.utcnow() - self.latest_location_time).total_seconds()
        if delta < 120: # < 2 min
            return "Live"
        elif delta < 600: # 2 - 10 min
            return "Stale"
        else:
            return "Offline"

    def is_operational_active(self):
        """
        A bus is active if marked 'Active' AND has an active journey or activity/GPS within 30 mins.
        """
        if self.bus_status != "Active":
            return False
        has_active_journey = any(j.status == "active" for j in self.journeys)
        if has_active_journey:
            return True
        if self.latest_location_time:
            delta = (datetime.utcnow() - self.latest_location_time).total_seconds()
            if delta < 1800:
                return True
        return False

    def to_dict(self):
        active_j = next((j for j in self.journeys if j.status == "active"), None)
        return {
            "id": self.id,
            "bus_number": self.bus_number,
            "registration_number": self.registration_number or self.license_plate or self.bus_number,
            "license_plate": self.license_plate,
            "bus_name": self.bus_name or f"Bus {self.bus_number}",
            "bus_type": self.bus_type or "Standard Transit",
            "capacity": self.capacity,
            "route_name": self.route_name,
            "route_number": self.route_number or "",
            "starting_point": self.starting_point or "",
            "destination": self.destination or "",
            "operator_name": self.operator_name or (self.assigned_operator.full_name or self.assigned_operator.username if self.assigned_operator else ""),
            "operator_contact": self.operator_contact or "",
            "bus_status": self.bus_status or "Active",
            "is_active": self.is_active,
            "assigned_operator_id": self.assigned_operator_id,
            "latest_latitude": self.latest_latitude,
            "latest_longitude": self.latest_longitude,
            "latest_gps_accuracy": self.latest_gps_accuracy,
            "latest_location_source": self.latest_location_source,
            "latest_location_time": self.latest_location_time.strftime("%Y-%m-%d %H:%M:%S") if self.latest_location_time else None,
            "location_freshness": self.get_location_freshness(),
            "is_operational_active": self.is_operational_active(),
            "active_journey": active_j.to_dict() if active_j else None,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None
        }


class OperatorBusAssignment(db.Model):
    __tablename__ = "operator_bus_assignments"

    id = db.Column(db.Integer, primary_key=True)
    operator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    bus_id = db.Column(db.Integer, db.ForeignKey("buses.id"), nullable=False, index=True)
    is_primary = db.Column(db.Boolean, default=True)
    assigned_at = db.Column(db.DateTime, default=datetime.utcnow)

    operator = db.relationship("User", foreign_keys=[operator_id])
    bus = db.relationship("Bus", foreign_keys=[bus_id])

    def to_dict(self):
        return {
            "id": self.id,
            "operator_id": self.operator_id,
            "bus_id": self.bus_id,
            "is_primary": self.is_primary,
            "bus": self.bus.to_dict() if self.bus else None,
            "assigned_at": self.assigned_at.strftime("%Y-%m-%d %H:%M:%S") if self.assigned_at else None
        }


class BusLocationLog(db.Model):
    __tablename__ = "bus_location_logs"

    id = db.Column(db.Integer, primary_key=True)
    bus_id = db.Column(db.Integer, db.ForeignKey("buses.id"), nullable=False, index=True)
    operator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    journey_id = db.Column(db.Integer, db.ForeignKey("journeys.id"), nullable=True)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    accuracy = db.Column(db.Float, nullable=True)
    location_source = db.Column(db.String(30), default="DEVICE_API") # 'DEVICE_API', 'MANUAL_ENTRY'
    recorded_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    bus = db.relationship("Bus", foreign_keys=[bus_id])
    operator = db.relationship("User", foreign_keys=[operator_id])

    def to_dict(self):
        return {
            "id": self.id,
            "bus_id": self.bus_id,
            "operator_id": self.operator_id,
            "journey_id": self.journey_id,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "accuracy": self.accuracy,
            "location_source": self.location_source,
            "recorded_at": self.recorded_at.strftime("%Y-%m-%d %H:%M:%S") if self.recorded_at else None
        }


class Journey(db.Model):
    __tablename__ = "journeys"

    id = db.Column(db.Integer, primary_key=True)
    journey_code = db.Column(db.String(60), unique=True, nullable=False, index=True)
    bus_id = db.Column(db.Integer, db.ForeignKey("buses.id"), nullable=False)
    origin = db.Column(db.String(100), nullable=False)
    destination = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default="active", nullable=False) # 'active', 'completed', 'scheduled'
    start_time = db.Column(db.DateTime, default=datetime.utcnow)
    end_time = db.Column(db.DateTime, nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    uploads = db.relationship("Upload", backref="journey", lazy=True, cascade="all, delete-orphan")
    passengers = db.relationship("PassengerIdentity", backref="journey", lazy=True, cascade="all, delete-orphan")
    presence_events = db.relationship("PresenceEvent", backref="journey", lazy=True, cascade="all, delete-orphan")

    @property
    def duration(self):
        if self.start_time and self.end_time:
            diff = max(0.0, (self.end_time - self.start_time).total_seconds())
            hrs = int(diff // 3600)
            mins = int((diff % 3600) // 60)
            return f"{hrs}h {mins}m" if hrs > 0 else f"{mins}m"
        elif self.start_time:
            diff = max(0.0, (datetime.utcnow() - self.start_time).total_seconds())
            hrs = int(diff // 3600)
            mins = int((diff % 3600) // 60)
            return f"{hrs}h {mins}m (In-Transit)"
        return "-"

    def to_dict(self):
        duration_str = self.duration

        return {
            "id": self.id,
            "journey_code": self.journey_code,
            "bus_id": self.bus_id,
            "bus_number": self.bus.bus_number if self.bus else None,
            "origin": self.origin,
            "destination": self.destination,
            "status": self.status,
            "start_time": self.start_time.strftime("%Y-%m-%d %H:%M:%S") if self.start_time else None,
            "end_time": self.end_time.strftime("%Y-%m-%d %H:%M:%S") if self.end_time else None,
            "duration": duration_str,
            "passenger_count": len(self.passengers) if self.passengers else 0,
            "uploads_count": len(self.uploads) if self.uploads else 0,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None
        }


class Upload(db.Model):
    __tablename__ = "uploads"

    id = db.Column(db.Integer, primary_key=True)
    journey_id = db.Column(db.Integer, db.ForeignKey("journeys.id"), nullable=False, index=True)
    bus_id = db.Column(db.Integer, db.ForeignKey("buses.id"), nullable=True, index=True)
    camera_angle = db.Column(db.String(10), nullable=False, default="A", index=True) # 'A', 'B', 'C'
    batch_id = db.Column(db.String(64), nullable=True, index=True) # groups multi-angle submissions
    file_path = db.Column(db.String(255), nullable=False)
    annotated_path = db.Column(db.String(255), nullable=True)
    original_filename = db.Column(db.String(255), nullable=False)
    file_size = db.Column(db.Integer, default=0)
    file_hash = db.Column(db.String(64), nullable=True, index=True) # SHA-256 to detect duplicate uploads
    
    # Timing
    capture_timestamp = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    upload_timestamp = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    # GPS
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    gps_accuracy = db.Column(db.Float, nullable=True)
    location_source = db.Column(db.String(30), default="NONE") # 'EXIF', 'DEVICE_API', 'MANUAL_ENTRY', 'MAP_PICKER', 'NONE'
    exif_raw_json = db.Column(db.Text, nullable=True)

    status = db.Column(db.String(20), default="processing") # 'processing', 'processed', 'failed'
    route_stop_id = db.Column(db.Integer, db.ForeignKey("route_stops.id"), nullable=True, index=True)
    passengers_detected_count = db.Column(db.Integer, default=0)
    uncertain_detections_count = db.Column(db.Integer, default=0)
    is_color = db.Column(db.Boolean, default=True)
    color_score = db.Column(db.Float, nullable=True)
    skip_reason = db.Column(db.Text, nullable=True)
    color_classification_details_json = db.Column(db.Text, nullable=True)
    processing_notes = db.Column(db.Text, nullable=True)
    error_details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    observations = db.relationship("PassengerObservation", backref="upload", lazy=True, cascade="all, delete-orphan")
    gps_record = db.relationship("GPSLocation", backref="upload", uselist=False, cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "journey_id": self.journey_id,
            "bus_id": self.bus_id,
            "camera_angle": self.camera_angle,
            "batch_id": self.batch_id,
            "file_path": self.file_path,
            "annotated_path": self.annotated_path,
            "original_filename": self.original_filename,
            "file_size": self.file_size,
            "file_hash": self.file_hash,
            "capture_timestamp": self.capture_timestamp.strftime("%Y-%m-%d %H:%M:%S") if self.capture_timestamp else None,
            "upload_timestamp": self.upload_timestamp.strftime("%Y-%m-%d %H:%M:%S") if self.upload_timestamp else None,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "gps_accuracy": self.gps_accuracy,
            "location_source": self.location_source,
            "status": self.status,
            "is_color": self.is_color if self.is_color is not None else True,
            "color_score": self.color_score,
            "skip_reason": self.skip_reason,
            "route_stop_id": self.route_stop_id,
            "passengers_detected_count": self.passengers_detected_count,
            "uncertain_detections_count": self.uncertain_detections_count,
            "observations_count": len(self.observations) if self.observations else 0,
            "processing_notes": self.processing_notes,
            "error_details": self.error_details,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None
        }


class PassengerIdentity(db.Model):
    __tablename__ = "passenger_identities"

    id = db.Column(db.Integer, primary_key=True)
    journey_id = db.Column(db.Integer, db.ForeignKey("journeys.id"), nullable=False, index=True)
    person_id = db.Column(db.String(30), nullable=False) # e.g. "P001", unique within journey
    
    # Tracking estimates
    first_seen_time = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_seen_time = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    first_seen_upload_id = db.Column(db.Integer, db.ForeignKey("uploads.id"), nullable=True)
    last_seen_upload_id = db.Column(db.Integer, db.ForeignKey("uploads.id"), nullable=True)

    # GPS estimates
    first_latitude = db.Column(db.Float, nullable=True)
    first_longitude = db.Column(db.Float, nullable=True)
    last_latitude = db.Column(db.Float, nullable=True)
    last_longitude = db.Column(db.Float, nullable=True)
    
    # State machine
    # 'PRESENT', 'POSSIBLY_ABSENT', 'EXIT_UNCONFIRMED', 'EXIT_CONFIRMED', 'NEEDS_REVIEW'
    status = db.Column(db.String(30), default="PRESENT", nullable=False, index=True)
    total_observations = db.Column(db.Integer, default=1)
    consecutive_missed_count = db.Column(db.Integer, default=0)

    # Biometric representation: stored as JSON array of floats or serialized bytes
    representative_embedding_json = db.Column(db.Text, nullable=True)
    representative_clothes_json = db.Column(db.Text, nullable=True)
    gallery_embeddings_json = db.Column(db.Text, nullable=True) # JSON list of diverse reference embeddings
    embedding_model_version = db.Column(db.String(50), default="facenet_vggface2_512", nullable=True)
    embedding_sample_count = db.Column(db.Integer, default=1)
    thumbnail_path = db.Column(db.String(255), nullable=True)
    
    # Human in the loop review & notes
    manual_notes = db.Column(db.Text, nullable=True)
    confirmed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    confirmed_at = db.Column(db.DateTime, nullable=True)

    # Seat positioning
    current_seat = db.Column(db.String(10), nullable=True) # e.g. "1A", "2B", "3C", "4D"
    previous_seat = db.Column(db.String(10), nullable=True)

    # Route Stop Tracking
    boarding_stop_id = db.Column(db.Integer, nullable=True)
    exit_stop_id = db.Column(db.Integer, nullable=True)
    boarding_stop_name = db.Column(db.String(255), nullable=True)
    exit_stop_name = db.Column(db.String(255), nullable=True)
    boarding_stop_number = db.Column(db.Integer, nullable=True)
    exit_stop_number = db.Column(db.Integer, nullable=True)
    stops_traveled = db.Column(db.Integer, default=1)
    traveled_stops_json = db.Column(db.Text, nullable=True)

    # Multi-Angle & Observation Timeline
    first_observed_angle = db.Column(db.String(10), nullable=True)  # 'A', 'B', 'C'
    last_observed_angle = db.Column(db.String(10), nullable=True)   # 'A', 'B', 'C'
    detected_angles_json = db.Column(db.Text, nullable=True)        # JSON array e.g. ["A", "B"]
    observed_interval_seconds = db.Column(db.Float, default=0.0)
    uncertain_match = db.Column(db.Boolean, default=False)

    # Canonical Identity Merging
    canonical_identity_id = db.Column(db.Integer, db.ForeignKey("passenger_identities.id"), nullable=True)
    canonical_person_id = db.Column(db.String(30), nullable=True)
    is_merged = db.Column(db.Boolean, default=False, nullable=False)
    merged_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    observations = db.relationship("PassengerObservation", backref="passenger", lazy=True, cascade="all, delete-orphan")
    events = db.relationship("PresenceEvent", backref="passenger", lazy=True, cascade="all, delete-orphan")
    reviews = db.relationship("ManualReview", backref="passenger", lazy=True, cascade="all, delete-orphan")

    def get_canonical_identity(self):
        """Resolves the root master PassengerIdentity if this identity was merged, avoiding cycles."""
        visited = {self.id}
        curr = self
        while curr.canonical_identity_id and curr.canonical_identity_id != curr.id:
            parent = PassengerIdentity.query.get(curr.canonical_identity_id)
            if not parent or parent.id in visited:
                break
            visited.add(parent.id)
            curr = parent
        return curr

    def get_canonical_person_id(self):
        if not self.is_merged and not self.canonical_identity_id:
            return self.canonical_person_id or self.person_id
        canonical = self.get_canonical_identity()
        return canonical.person_id if canonical else (self.canonical_person_id or self.person_id)

    def get_seat_number(self):
        """Returns explicitly set seat or calculates a stable deterministic seat."""
        if self.current_seat:
            return self.current_seat
        try:
            num = int(''.join(filter(str.isdigit, self.person_id)) or "1")
            row = ((num - 1) // 4) + 1
            col = ["A", "B", "C", "D"][(num - 1) % 4]
            return f"{row}{col}"
        except Exception:
            return "1A"

    def get_detected_angles(self):
        if self.detected_angles_json:
            try:
                angles = json.loads(self.detected_angles_json)
                if isinstance(angles, list) and angles:
                    return angles
            except Exception:
                pass
        if self.observations:
            angles = sorted(list(set(o.camera_angle for o in self.observations if o.camera_angle)))
            if angles:
                return angles
        return [self.first_observed_angle] if self.first_observed_angle else ["A"]

    def get_first_observed_angle(self):
        if self.first_observed_angle:
            return self.first_observed_angle
        if self.observations:
            earliest = min(self.observations, key=lambda o: o.observation_time or datetime.min)
            if earliest and earliest.camera_angle:
                return earliest.camera_angle
        return "A"

    def get_observed_interval_str(self):
        if self.first_seen_time and self.last_seen_time:
            diff = (self.last_seen_time - self.first_seen_time).total_seconds()
            if diff < 0:
                diff = 0
            hrs = int(diff // 3600)
            mins = int((diff % 3600) // 60)
            secs = int(diff % 60)
            if hrs > 0:
                return f"{hrs}h {mins}m"
            elif mins > 0:
                return f"{mins}m {secs}s"
            else:
                return f"{secs}s"
        return "0s"

    def get_distinct_stops_count(self):
        if self.traveled_stops_json:
            try:
                ts = json.loads(self.traveled_stops_json)
                return max(1, len(ts))
            except Exception:
                pass
        return max(1, self.stops_traveled or 1)

    def to_dict(self):
        traveled_list = []
        if self.traveled_stops_json:
            try:
                traveled_list = json.loads(self.traveled_stops_json)
            except Exception:
                pass

        first_angle = self.get_first_observed_angle()
        all_angles = self.get_detected_angles()
        interval_str = self.get_observed_interval_str()
        distinct_stops = self.get_distinct_stops_count()

        canonical_pid = self.get_canonical_person_id()
        is_merged = bool(self.is_merged or (self.canonical_identity_id is not None))
        bus_id = self.journey.bus_id if self.journey else None
        bus_number = self.journey.bus.bus_number if (self.journey and self.journey.bus) else None

        return {
            "id": self.id,
            "journey_id": self.journey_id,
            "bus_id": bus_id,
            "bus_number": bus_number,
            "person_id": self.person_id,
            "canonical_person_id": canonical_pid,
            "canonical_identity_id": self.canonical_identity_id,
            "is_merged": is_merged,
            "merged_at": self.merged_at.strftime("%Y-%m-%d %H:%M:%S") if self.merged_at else None,
            "current_seat": self.get_seat_number(),
            "previous_seat": self.previous_seat,
            "boarding_stop_id": self.boarding_stop_id,
            "exit_stop_id": self.exit_stop_id,
            "boarding_stop_name": self.boarding_stop_name,
            "exit_stop_name": self.exit_stop_name,
            "boarding_stop_number": self.boarding_stop_number,
            "exit_stop_number": self.exit_stop_number,
            "stops_traveled": self.stops_traveled or 1,
            "traveled_stops": traveled_list,
            "distinct_stops_count": distinct_stops,
            "first_seen_time": self.first_seen_time.strftime("%Y-%m-%d %H:%M:%S") if self.first_seen_time else None,
            "last_seen_time": self.last_seen_time.strftime("%Y-%m-%d %H:%M:%S") if self.last_seen_time else None,
            "first_observed_angle": first_angle,
            "last_observed_angle": self.last_observed_angle or (all_angles[-1] if all_angles else first_angle),
            "detected_angles": all_angles,
            "observed_interval": interval_str,
            "uncertain_match": self.uncertain_match or (self.status == "NEEDS_REVIEW"),
            "first_latitude": self.first_latitude,
            "first_longitude": self.first_longitude,
            "last_latitude": self.last_latitude,
            "last_longitude": self.last_longitude,
            "status": self.status,
            "total_observations": self.total_observations,
            "consecutive_missed_count": self.consecutive_missed_count,
            "thumbnail_path": self.thumbnail_path,
            "manual_notes": self.manual_notes,
            "confirmed_at": self.confirmed_at.strftime("%Y-%m-%d %H:%M:%S") if self.confirmed_at else None,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None
        }


class PassengerObservation(db.Model):
    __tablename__ = "passenger_observations"

    id = db.Column(db.Integer, primary_key=True)
    upload_id = db.Column(db.Integer, db.ForeignKey("uploads.id"), nullable=False, index=True)
    journey_id = db.Column(db.Integer, db.ForeignKey("journeys.id"), nullable=False, index=True)
    passenger_identity_id = db.Column(db.Integer, db.ForeignKey("passenger_identities.id"), nullable=True, index=True)
    camera_angle = db.Column(db.String(10), nullable=True, default="A", index=True) # 'A', 'B', 'C'
    temp_detection_id = db.Column(db.String(50), nullable=True) # e.g. "T-A-01" for unidentified visible bodies

    # Face or body bounding box [x, y, w, h] in pixel coords
    bbox_x = db.Column(db.Integer, nullable=False)
    bbox_y = db.Column(db.Integer, nullable=False)
    bbox_w = db.Column(db.Integer, nullable=False)
    bbox_h = db.Column(db.Integer, nullable=False)

    # Secondary body bounding box (if detected via YOLO)
    person_bbox_json = db.Column(db.Text, nullable=True)

    # Metrics
    detection_confidence = db.Column(db.Float, default=0.0) # YOLO/body detector confidence
    face_confidence = db.Column(db.Float, default=0.0) # Face detector confidence
    similarity_score = db.Column(db.Float, default=0.0) # Cosine similarity to person identity
    confidence_label = db.Column(db.String(20), default="High") # 'High', 'Medium', 'Low', 'Needs Review'
    quality_score = db.Column(db.Float, default=1.0) # Image/Face quality rating
    is_blur = db.Column(db.Boolean, default=False)
    crop_path = db.Column(db.String(255), nullable=True)
    clothing_signature_json = db.Column(db.Text, nullable=True)
    face_embedding_json = db.Column(db.Text, nullable=True) # Normalized 512-dim embedding for this observation
    embedding_model_version = db.Column(db.String(50), default="facenet_vggface2_512", nullable=True)
    route_stop_id = db.Column(db.Integer, db.ForeignKey("route_stops.id"), nullable=True, index=True)

    # Cross-angle duplicate & review handling
    review_status = db.Column(db.String(30), default="CONFIRMED", index=True) # 'CONFIRMED', 'NEEDS_REVIEW', 'FALSE_POSITIVE'
    is_potential_duplicate = db.Column(db.Boolean, default=False)
    duplicate_review_status = db.Column(db.String(30), nullable=True) # 'PENDING_REVIEW', 'RESOLVED_UNIQUE', 'RESOLVED_MERGED'

    observation_time = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def get_canonical_person_id(self):
        if self.passenger:
            return self.passenger.get_canonical_person_id()
        return self.temp_detection_id or "Unidentified"

    def to_dict(self):
        orig_pid = self.passenger.person_id if self.passenger else (self.temp_detection_id or "Unidentified")
        canonical_pid = self.get_canonical_person_id()
        is_merged = bool(self.passenger and (self.passenger.is_merged or self.passenger.canonical_identity_id is not None))

        return {
            "id": self.id,
            "upload_id": self.upload_id,
            "journey_id": self.journey_id,
            "passenger_identity_id": self.passenger_identity_id,
            "person_id": orig_pid,
            "canonical_person_id": canonical_pid,
            "is_merged": is_merged,
            "camera_angle": self.camera_angle or "Unknown Angle",
            "temp_detection_id": self.temp_detection_id,
            "bbox": [self.bbox_x, self.bbox_y, self.bbox_w, self.bbox_h],
            "detection_confidence": round(self.detection_confidence, 3) if self.detection_confidence else 0.0,
            "face_confidence": round(self.face_confidence, 3) if self.face_confidence else 0.0,
            "similarity_score": round(self.similarity_score, 3) if self.similarity_score else 0.0,
            "confidence_label": self.confidence_label,
            "quality_score": round(self.quality_score, 2) if self.quality_score else 1.0,
            "is_blur": self.is_blur,
            "review_status": self.review_status,
            "is_potential_duplicate": self.is_potential_duplicate,
            "duplicate_review_status": self.duplicate_review_status,
            "crop_path": self.crop_path,
            "route_stop_id": self.route_stop_id,
            "observation_time": self.observation_time.strftime("%Y-%m-%d %H:%M:%S") if self.observation_time else None
        }


class PassengerMergeRecord(db.Model):
    __tablename__ = "passenger_merge_records"

    id = db.Column(db.Integer, primary_key=True)
    journey_id = db.Column(db.Integer, db.ForeignKey("journeys.id"), nullable=True, index=True)
    master_identity_id = db.Column(db.Integer, db.ForeignKey("passenger_identities.id"), nullable=False, index=True)
    master_person_id = db.Column(db.String(30), nullable=False)
    merged_identity_id = db.Column(db.Integer, db.ForeignKey("passenger_identities.id"), nullable=False, index=True)
    merged_person_id = db.Column(db.String(30), nullable=False)
    merge_timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    merged_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    merge_reason = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default="ACTIVE", nullable=False) # 'ACTIVE', 'REVERSED'
    reversed_at = db.Column(db.DateTime, nullable=True)
    reversed_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    reversal_reason = db.Column(db.Text, nullable=True)
    audit_reference = db.Column(db.String(64), nullable=True)
    snapshot_json = db.Column(db.Text, nullable=True)

    master_identity = db.relationship("PassengerIdentity", foreign_keys=[master_identity_id])
    merged_identity = db.relationship("PassengerIdentity", foreign_keys=[merged_identity_id])
    merged_by = db.relationship("User", foreign_keys=[merged_by_user_id])
    reversed_by = db.relationship("User", foreign_keys=[reversed_by_user_id])

    def to_dict(self):
        return {
            "id": self.id,
            "journey_id": self.journey_id,
            "master_identity_id": self.master_identity_id,
            "master_person_id": self.master_person_id,
            "merged_identity_id": self.merged_identity_id,
            "merged_person_id": self.merged_person_id,
            "merge_timestamp": self.merge_timestamp.strftime("%Y-%m-%d %H:%M:%S") if self.merge_timestamp else None,
            "merged_by_user_id": self.merged_by_user_id,
            "merged_by_name": self.merged_by.username if self.merged_by else "System",
            "merge_reason": self.merge_reason,
            "status": self.status,
            "reversed_at": self.reversed_at.strftime("%Y-%m-%d %H:%M:%S") if self.reversed_at else None,
            "reversed_by_name": self.reversed_by.username if self.reversed_by else None,
            "reversal_reason": self.reversal_reason,
            "audit_reference": self.audit_reference
        }


class CameraAngleConfig(db.Model):
    __tablename__ = "camera_angle_configs"

    id = db.Column(db.Integer, primary_key=True)
    bus_id = db.Column(db.Integer, db.ForeignKey("buses.id"), nullable=True) # Nullable for default global angle setup
    camera_angle = db.Column(db.String(10), nullable=False, index=True) # 'A', 'B', 'C'
    title = db.Column(db.String(120), nullable=False) # e.g. "Angle A — Front / Assigned Seating Section"
    seating_area_label = db.Column(db.String(120), nullable=False) # e.g. "Front Rows (Rows 1-2 / Seats 1A-2D)"
    target_seats = db.Column(db.String(100), default="1A, 1B, 1C, 1D")
    target_capacity = db.Column(db.Integer, default=4)
    roi_json = db.Column(db.Text, nullable=True) # JSON: {"x_min": 0.0, "y_min": 0.0, "x_max": 1.0, "y_max": 1.0}
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        roi = None
        if self.roi_json:
            try:
                roi = json.loads(self.roi_json)
            except Exception:
                pass
        return {
            "id": self.id,
            "bus_id": self.bus_id,
            "camera_angle": self.camera_angle,
            "title": self.title,
            "seating_area_label": self.seating_area_label,
            "target_seats": self.target_seats,
            "target_capacity": self.target_capacity,
            "roi": roi,
            "is_active": self.is_active,
            "updated_at": self.updated_at.strftime("%Y-%m-%d %H:%M:%S") if self.updated_at else None
        }


class PresenceEvent(db.Model):
    __tablename__ = "presence_events"

    id = db.Column(db.Integer, primary_key=True)
    journey_id = db.Column(db.Integer, db.ForeignKey("journeys.id"), nullable=False, index=True)
    passenger_identity_id = db.Column(db.Integer, db.ForeignKey("passenger_identities.id"), nullable=False, index=True)
    upload_id = db.Column(db.Integer, db.ForeignKey("uploads.id"), nullable=True)
    route_stop_id = db.Column(db.Integer, db.ForeignKey("route_stops.id"), nullable=True, index=True)
    
    # Event Types:
    # 'BOARDING_PROVISIONAL', 'PRESENT_CONFIRMED', 'POSSIBLY_ABSENT', 'EXIT_UNCONFIRMED', 'EXIT_CONFIRMED', 'MANUAL_OVERRIDE'
    event_type = db.Column(db.String(30), nullable=False)
    event_time = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    triggered_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "journey_id": self.journey_id,
            "passenger_identity_id": self.passenger_identity_id,
            "person_id": self.passenger.person_id if self.passenger else None,
            "route_stop_id": self.route_stop_id,
            "event_type": self.event_type,
            "event_time": self.event_time.strftime("%Y-%m-%d %H:%M:%S") if self.event_time else None,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "notes": self.notes,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None
        }


class GPSLocation(db.Model):
    __tablename__ = "gps_locations"

    id = db.Column(db.Integer, primary_key=True)
    upload_id = db.Column(db.Integer, db.ForeignKey("uploads.id"), nullable=False, index=True)
    journey_id = db.Column(db.Integer, db.ForeignKey("journeys.id"), nullable=False, index=True)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    accuracy = db.Column(db.Float, nullable=True)
    location_source = db.Column(db.String(30), default="NONE") # 'EXIF', 'DEVICE_API', 'MANUAL_ENTRY', 'MAP_PICKER'
    recorded_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "upload_id": self.upload_id,
            "journey_id": self.journey_id,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "accuracy": self.accuracy,
            "location_source": self.location_source,
            "recorded_at": self.recorded_at.strftime("%Y-%m-%d %H:%M:%S") if self.recorded_at else None
        }


class ManualReview(db.Model):
    __tablename__ = "manual_reviews"

    id = db.Column(db.Integer, primary_key=True)
    journey_id = db.Column(db.Integer, db.ForeignKey("journeys.id"), nullable=False, index=True)
    passenger_identity_id = db.Column(db.Integer, db.ForeignKey("passenger_identities.id"), nullable=False, index=True)
    upload_id = db.Column(db.Integer, db.ForeignKey("uploads.id"), nullable=True)
    reviewer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    
    # 'CONFIRM_EXIT', 'CORRECT_ID', 'VERIFY_MATCH', 'REJECT_MATCH', 'MARK_PRESENT'
    review_type = db.Column(db.String(30), nullable=False)
    previous_status = db.Column(db.String(30), nullable=True)
    new_status = db.Column(db.String(30), nullable=False)
    comment = db.Column(db.Text, nullable=True)
    reviewed_at = db.Column(db.DateTime, default=datetime.utcnow)

    reviewer = db.relationship("User", foreign_keys=[reviewer_id])

    def to_dict(self):
        return {
            "id": self.id,
            "journey_id": self.journey_id,
            "passenger_identity_id": self.passenger_identity_id,
            "person_id": self.passenger.person_id if self.passenger else None,
            "reviewer_name": self.reviewer.full_name or self.reviewer.username if self.reviewer else "Unknown",
            "review_type": self.review_type,
            "previous_status": self.previous_status,
            "new_status": self.new_status,
            "comment": self.comment,
            "reviewed_at": self.reviewed_at.strftime("%Y-%m-%d %H:%M:%S") if self.reviewed_at else None
        }


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    action = db.Column(db.String(100), nullable=False, index=True)
    entity_type = db.Column(db.String(50), nullable=True)
    entity_id = db.Column(db.String(50), nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    details_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    user = db.relationship("User", foreign_keys=[user_id])

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "username": self.user.username if self.user else "System",
            "action": self.action,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "ip_address": self.ip_address,
            "details": json.loads(self.details_json) if self.details_json else {},
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None
        }


class RouteStop(db.Model):
    __tablename__ = "route_stops"

    id = db.Column(db.Integer, primary_key=True)
    bus_id = db.Column(db.Integer, db.ForeignKey("buses.id"), nullable=True, index=True)
    journey_id = db.Column(db.Integer, db.ForeignKey("journeys.id"), nullable=True, index=True)
    stop_order = db.Column(db.Integer, nullable=False, default=1)
    stop_name = db.Column(db.String(150), nullable=False)
    location_address = db.Column(db.Text, nullable=False)
    time_window = db.Column(db.String(80), nullable=True) # e.g. "06:01:20 PM - 06:01:30 PM"
    speed_kmh = db.Column(db.Float, default=0.0) # Speed in km/h
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    is_major_stop = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    bus = db.relationship("Bus", foreign_keys=[bus_id])
    journey = db.relationship("Journey", foreign_keys=[journey_id])

    def to_dict(self):
        return {
            "id": self.id,
            "bus_id": self.bus_id,
            "journey_id": self.journey_id,
            "stop_order": self.stop_order,
            "stop_name": self.stop_name,
            "location_address": self.location_address,
            "time_window": self.time_window,
            "speed_kmh": round(self.speed_kmh, 1) if self.speed_kmh is not None else 0.0,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "is_major_stop": self.is_major_stop,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None
        }


class MasterRouteStop(db.Model):
    """
    Admin-provided master transit stations and stops library.
    Operators can pick from this list during bus registration, or add custom stops.
    """
    __tablename__ = "master_route_stops"

    id = db.Column(db.Integer, primary_key=True)
    stop_name = db.Column(db.String(150), nullable=False, unique=True, index=True)
    location_address = db.Column(db.Text, nullable=False)
    default_speed_kmh = db.Column(db.Float, default=8.0)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    is_major_stop = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "stop_name": self.stop_name,
            "location_address": self.location_address,
            "default_speed_kmh": round(self.default_speed_kmh, 1) if self.default_speed_kmh is not None else 8.0,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "is_major_stop": self.is_major_stop,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None
        }


class MasterUPRoute(db.Model):
    """
    Master Uttar Pradesh Transit Routes Library.
    Major intercity and regional corridors across Uttar Pradesh (UPSRTC & National Highways).
    Operators choose their operating route during registration, which auto-configures stops and timeline.
    Admin can dynamically configure, add, update, and monitor buses operating on these routes.
    """
    __tablename__ = "master_up_routes"

    id = db.Column(db.Integer, primary_key=True)
    route_code = db.Column(db.String(50), unique=True, nullable=False, index=True) # e.g. "UP-RT-01"
    route_name = db.Column(db.String(180), nullable=False) # e.g. "Delhi/NCR — Ghaziabad — Moradabad — Bareilly — Lucknow Express"
    region = db.Column(db.String(80), default="Uttar Pradesh") # e.g. "Western / Central UP", "NCR", "Eastern UP"
    origin = db.Column(db.String(120), nullable=False)
    destination = db.Column(db.String(120), nullable=False)
    total_distance_km = db.Column(db.Float, default=100.0)
    estimated_duration_hours = db.Column(db.Float, default=3.0)
    avg_speed_kmh = db.Column(db.Float, default=12.0)
    stops_json = db.Column(db.Text, nullable=False) # JSON array of stops
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def get_stops(self):
        try:
            return json.loads(self.stops_json) if self.stops_json else []
        except Exception:
            return []

    def set_stops(self, stops_list):
        self.stops_json = json.dumps(stops_list)

    def to_dict(self):
        stops = self.get_stops()
        return {
            "id": self.id,
            "route_code": self.route_code,
            "route_name": self.route_name,
            "region": self.region,
            "origin": self.origin,
            "destination": self.destination,
            "total_distance_km": self.total_distance_km,
            "estimated_duration_hours": self.estimated_duration_hours,
            "avg_speed_kmh": self.avg_speed_kmh,
            "stops_count": len(stops),
            "stops": stops,
            "is_active": self.is_active,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None
        }


# ============================================================================
# AUTOMATED MULTI-ANGLE VIDEO & TIMETABLE STOP MODELS
# ============================================================================

class SourceFolder(db.Model):
    """
    Operator-selected source media folder for camera angles A, B, or C.
    """
    __tablename__ = "source_folders"

    id = db.Column(db.Integer, primary_key=True)
    journey_id = db.Column(db.Integer, db.ForeignKey("journeys.id"), nullable=True, index=True)
    camera_angle = db.Column(db.String(10), nullable=False, index=True)  # 'A', 'B', 'C'
    folder_path = db.Column(db.String(500), nullable=False)
    folder_name = db.Column(db.String(255), nullable=False)
    clips_count = db.Column(db.Integer, default=0)
    images_count = db.Column(db.Integer, default=0)
    status = db.Column(db.String(30), default="ready")  # 'ready', 'scanning', 'error'
    error_message = db.Column(db.Text, nullable=True)
    last_scanned_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    media_files = db.relationship("SourceMediaFile", backref="source_folder", lazy=True, cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "journey_id": self.journey_id,
            "camera_angle": self.camera_angle,
            "folder_path": self.folder_path,
            "folder_name": self.folder_name,
            "clips_count": self.clips_count,
            "images_count": self.images_count,
            "status": self.status,
            "error_message": self.error_message,
            "last_scanned_at": self.last_scanned_at.strftime("%Y-%m-%d %H:%M:%S") if self.last_scanned_at else None,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None
        }


class SourceMediaFile(db.Model):
    """
    Read-only source media clip/image discovered in a source folder.
    """
    __tablename__ = "source_media_files"

    id = db.Column(db.Integer, primary_key=True)
    source_folder_id = db.Column(db.Integer, db.ForeignKey("source_folders.id"), nullable=False, index=True)
    journey_id = db.Column(db.Integer, db.ForeignKey("journeys.id"), nullable=True, index=True)
    camera_angle = db.Column(db.String(10), nullable=False, index=True)  # 'A', 'B', 'C'
    file_path = db.Column(db.String(600), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    file_ext = db.Column(db.String(20), nullable=False)
    media_type = db.Column(db.String(20), nullable=False)  # 'video', 'image'
    file_size = db.Column(db.BigInteger, default=0)
    file_hash = db.Column(db.String(64), nullable=True, index=True)  # SHA-256 for dedup

    # Timestamps
    start_time = db.Column(db.DateTime, nullable=True, index=True)
    end_time = db.Column(db.DateTime, nullable=True, index=True)
    duration_seconds = db.Column(db.Float, default=0.0)
    fps = db.Column(db.Float, default=0.0)
    total_frames = db.Column(db.Integer, default=0)
    width = db.Column(db.Integer, default=0)
    height = db.Column(db.Integer, default=0)
    timestamp_source = db.Column(db.String(50), default="FILE_MTIME")  # 'MEDIA_CONTAINER', 'EXIF', 'FILENAME', 'FILE_MTIME', 'MANUAL'
    is_timestamp_reliable = db.Column(db.Boolean, default=True)

    status = db.Column(db.String(30), default="ready")  # 'ready', 'used', 'failed', 'error', 'skipped_black_and_white'
    is_color = db.Column(db.Boolean, default=True)
    color_score = db.Column(db.Float, nullable=True)
    skip_reason = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "source_folder_id": self.source_folder_id,
            "journey_id": self.journey_id,
            "camera_angle": self.camera_angle,
            "file_path": self.file_path,
            "filename": self.filename,
            "file_ext": self.file_ext,
            "media_type": self.media_type,
            "file_size": self.file_size,
            "file_hash": self.file_hash,
            "start_time": self.start_time.strftime("%Y-%m-%d %H:%M:%S") if self.start_time else None,
            "end_time": self.end_time.strftime("%Y-%m-%d %H:%M:%S") if self.end_time else None,
            "duration_seconds": round(self.duration_seconds, 2) if self.duration_seconds else 0.0,
            "fps": round(self.fps, 2) if self.fps else 0.0,
            "total_frames": self.total_frames,
            "width": self.width,
            "height": self.height,
            "timestamp_source": self.timestamp_source,
            "is_timestamp_reliable": self.is_timestamp_reliable,
            "status": self.status,
            "is_color": self.is_color if self.is_color is not None else True,
            "color_score": self.color_score,
            "skip_reason": self.skip_reason,
            "error_message": self.error_message,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None
        }


class TimetableImport(db.Model):
    """
    Uploaded Excel/CSV timetable record and mapping metadata.
    """
    __tablename__ = "timetable_imports"

    id = db.Column(db.Integer, primary_key=True)
    journey_id = db.Column(db.Integer, db.ForeignKey("journeys.id"), nullable=True, index=True)
    filename = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)
    file_size = db.Column(db.Integer, default=0)
    service_date = db.Column(db.Date, nullable=True)
    timezone = db.Column(db.String(50), default="UTC")
    tolerance_minutes = db.Column(db.Integer, default=3)
    time_window_mode = db.Column(db.String(50), default="ARRIVAL_DEPARTURE")  # 'ARRIVAL_DEPARTURE', 'WINDOW_WITH_BUFFER', 'POINT_FRAMES'
    column_mapping_json = db.Column(db.Text, nullable=True)
    total_stops = db.Column(db.Integer, default=0)
    status = db.Column(db.String(30), default="imported")  # 'imported', 'valid', 'invalid'
    validation_errors_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    stops = db.relationship("TimetableStop", backref="timetable", lazy=True, cascade="all, delete-orphan")

    def to_dict(self):
        col_map = {}
        if self.column_mapping_json:
            try:
                col_map = json.loads(self.column_mapping_json)
            except Exception:
                pass
        errs = []
        if self.validation_errors_json:
            try:
                errs = json.loads(self.validation_errors_json)
            except Exception:
                pass
        return {
            "id": self.id,
            "journey_id": self.journey_id,
            "filename": self.filename,
            "file_size": self.file_size,
            "service_date": self.service_date.strftime("%Y-%m-%d") if self.service_date else None,
            "timezone": self.timezone,
            "tolerance_minutes": self.tolerance_minutes,
            "time_window_mode": self.time_window_mode,
            "column_mapping": col_map,
            "total_stops": self.total_stops,
            "status": self.status,
            "validation_errors": errs,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None
        }


class TimetableStop(db.Model):
    """
    Parsed stop record from uploaded timetable.
    """
    __tablename__ = "timetable_stops"

    id = db.Column(db.Integer, primary_key=True)
    timetable_id = db.Column(db.Integer, db.ForeignKey("timetable_imports.id"), nullable=False, index=True)
    stop_number = db.Column(db.Integer, nullable=False)
    stop_name = db.Column(db.String(150), nullable=False)
    scheduled_arrival = db.Column(db.DateTime, nullable=True)
    scheduled_departure = db.Column(db.DateTime, nullable=True)
    actual_arrival = db.Column(db.DateTime, nullable=True)
    actual_departure = db.Column(db.DateTime, nullable=True)
    window_start = db.Column(db.DateTime, nullable=True)
    window_end = db.Column(db.DateTime, nullable=True)

    # Coverage & Processing
    coverage_status = db.Column(db.String(30), default="NOT_COVERED", index=True)
    # 'FULLY_COVERED', 'PARTIALLY_COVERED', 'NOT_COVERED', 'NEEDS_REVIEW', 'FAILED'
    processing_status = db.Column(db.String(30), default="pending", index=True)
    # 'pending', 'processing', 'completed', 'failed', 'skipped'
    error_message = db.Column(db.Text, nullable=True)

    # Stop Counts
    angle_a_count = db.Column(db.Integer, default=0)
    angle_b_count = db.Column(db.Integer, default=0)
    angle_c_count = db.Column(db.Integer, default=0)
    total_raw_detections = db.Column(db.Integer, default=0)
    potential_duplicates = db.Column(db.Integer, default=0)
    estimated_unique_passengers = db.Column(db.Integer, default=0)
    confirmed_identities = db.Column(db.Integer, default=0)
    unidentified_passengers = db.Column(db.Integer, default=0)
    needs_review_count = db.Column(db.Integer, default=0)
    missing_angles = db.Column(db.String(30), nullable=True)  # e.g. "B, C" or "None"

    # Boarding, Exit & Occupancy
    boarded_count = db.Column(db.Integer, default=0)
    exited_count = db.Column(db.Integer, default=0)
    continuing_count = db.Column(db.Integer, default=0)
    occupancy = db.Column(db.Integer, default=0)

    processed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    matches = db.relationship("StopMediaMatch", backref="stop", lazy=True, cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "timetable_id": self.timetable_id,
            "stop_number": self.stop_number,
            "stop_name": self.stop_name,
            "scheduled_arrival": self.scheduled_arrival.strftime("%Y-%m-%d %H:%M:%S") if self.scheduled_arrival else None,
            "scheduled_departure": self.scheduled_departure.strftime("%Y-%m-%d %H:%M:%S") if self.scheduled_departure else None,
            "actual_arrival": self.actual_arrival.strftime("%Y-%m-%d %H:%M:%S") if self.actual_arrival else None,
            "actual_departure": self.actual_departure.strftime("%Y-%m-%d %H:%M:%S") if self.actual_departure else None,
            "window_start": self.window_start.strftime("%Y-%m-%d %H:%M:%S") if self.window_start else None,
            "window_end": self.window_end.strftime("%Y-%m-%d %H:%M:%S") if self.window_end else None,
            "coverage_status": self.coverage_status,
            "processing_status": self.processing_status,
            "error_message": self.error_message,
            "angle_a_count": self.angle_a_count,
            "angle_b_count": self.angle_b_count,
            "angle_c_count": self.angle_c_count,
            "total_raw_detections": self.total_raw_detections,
            "potential_duplicates": self.potential_duplicates,
            "estimated_unique_passengers": self.estimated_unique_passengers,
            "confirmed_identities": self.confirmed_identities,
            "unidentified_passengers": self.unidentified_passengers,
            "needs_review_count": self.needs_review_count,
            "missing_angles": self.missing_angles or "None",
            "boarded_count": self.boarded_count or 0,
            "exited_count": self.exited_count or 0,
            "continuing_count": self.continuing_count or 0,
            "occupancy": self.occupancy or 0,
            "matches": [m.to_dict() for m in self.matches] if self.matches else [],
            "processed_at": self.processed_at.strftime("%Y-%m-%d %H:%M:%S") if self.processed_at else None
        }


class StopMediaMatch(db.Model):
    """
    Matched clip / frame associated with a stop and specific camera angle.
    """
    __tablename__ = "stop_media_matches"

    id = db.Column(db.Integer, primary_key=True)
    stop_id = db.Column(db.Integer, db.ForeignKey("timetable_stops.id"), nullable=False, index=True)
    camera_angle = db.Column(db.String(10), nullable=False, index=True)  # 'A', 'B', 'C'
    source_media_file_id = db.Column(db.Integer, db.ForeignKey("source_media_files.id"), nullable=True)
    upload_id = db.Column(db.Integer, db.ForeignKey("uploads.id"), nullable=True)

    source_filename = db.Column(db.String(255), nullable=True)
    source_folder = db.Column(db.String(500), nullable=True)
    media_type = db.Column(db.String(20), default="video")
    capture_timestamp = db.Column(db.DateTime, nullable=True)
    selected_frame_timestamp = db.Column(db.DateTime, nullable=True)
    frame_number = db.Column(db.Integer, nullable=True)
    extracted_frame_path = db.Column(db.String(500), nullable=True)
    annotated_frame_path = db.Column(db.String(500), nullable=True)
    
    match_reason = db.Column(db.String(100), default="AUTOMATIC_WINDOW_OVERLAP")
    time_diff_seconds = db.Column(db.Float, default=0.0)
    is_manual_override = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(30), default="matched")  # 'matched', 'processed', 'missing', 'failed', 'skipped_black_and_white'
    is_color = db.Column(db.Boolean, default=True)
    color_score = db.Column(db.Float, nullable=True)
    skip_reason = db.Column(db.Text, nullable=True)
    detection_count = db.Column(db.Integer, default=0)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    source_media_file = db.relationship("SourceMediaFile", foreign_keys=[source_media_file_id])
    upload = db.relationship("Upload", foreign_keys=[upload_id])

    def to_dict(self):
        target_ts = self.selected_frame_timestamp or (self.stop.scheduled_arrival if self.stop else None)
        clip_path = self.source_folder or (self.source_media_file.file_path if self.source_media_file else self.source_filename)
        fallback_reason = self.skip_reason or self.error_message or (self.match_reason if self.status in ['missing', 'failed'] else None)
        return {
            "id": self.id,
            "stop_id": self.stop_id,
            "excel_row_id": self.stop.stop_number if self.stop else None,
            "target_timestamp": target_ts.strftime("%Y-%m-%d %H:%M:%S") if target_ts else None,
            "target_location": self.stop.stop_name if self.stop else None,
            "camera_angle": self.camera_angle,
            "selected_clip_path": clip_path,
            "actual_clip_timestamp": self.capture_timestamp.strftime("%Y-%m-%d %H:%M:%S") if self.capture_timestamp else None,
            "source_media_file_id": self.source_media_file_id,
            "upload_id": self.upload_id,
            "source_filename": self.source_filename,
            "source_folder": self.source_folder,
            "media_type": self.media_type,
            "capture_timestamp": self.capture_timestamp.strftime("%Y-%m-%d %H:%M:%S") if self.capture_timestamp else None,
            "selected_frame_timestamp": self.selected_frame_timestamp.strftime("%Y-%m-%d %H:%M:%S") if self.selected_frame_timestamp else None,
            "frame_number": self.frame_number,
            "extracted_frame_path": self.extracted_frame_path,
            "annotated_frame_path": self.annotated_frame_path,
            "match_reason": self.match_reason,
            "selection_method": self.match_reason,
            "time_diff_seconds": round(self.time_diff_seconds, 1) if self.time_diff_seconds is not None else 0.0,
            "is_manual_override": self.is_manual_override,
            "status": self.status,
            "match_status": self.status,
            "is_color": self.is_color if self.is_color is not None else True,
            "color_score": self.color_score,
            "skip_reason": self.skip_reason,
            "failure_or_fallback_reason": fallback_reason,
            "detection_count": self.detection_count,
            "error_message": self.error_message
        }


class StopProcessingJob(db.Model):
    """
    Automated batch processing job queue and state tracker.
    """
    __tablename__ = "stop_processing_jobs"

    id = db.Column(db.Integer, primary_key=True)
    journey_id = db.Column(db.Integer, db.ForeignKey("journeys.id"), nullable=False, index=True)
    timetable_id = db.Column(db.Integer, db.ForeignKey("timetable_imports.id"), nullable=False, index=True)
    status = db.Column(db.String(30), default="idle", index=True)
    # 'idle', 'queued', 'running', 'paused', 'completed', 'cancelled', 'failed'
    
    current_stop_index = db.Column(db.Integer, default=0)
    current_stop_number = db.Column(db.Integer, nullable=True)
    current_stop_name = db.Column(db.String(150), nullable=True)
    current_angle = db.Column(db.String(10), nullable=True)  # 'A', 'B', 'C'
    
    total_stops = db.Column(db.Integer, default=0)
    processed_stops = db.Column(db.Integer, default=0)
    remaining_stops = db.Column(db.Integer, default=0)
    failed_stops = db.Column(db.Integer, default=0)
    skipped_stops = db.Column(db.Integer, default=0)
    fully_covered_stops = db.Column(db.Integer, default=0)
    partially_covered_stops = db.Column(db.Integer, default=0)
    not_covered_stops = db.Column(db.Integer, default=0)
    
    total_unique_passengers_estimate = db.Column(db.Integer, default=0)
    total_confirmed_identities = db.Column(db.Integer, default=0)

    progress_percent = db.Column(db.Float, default=0.0)
    job_settings_json = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    journey = db.relationship("Journey", foreign_keys=[journey_id])
    timetable = db.relationship("TimetableImport", foreign_keys=[timetable_id])

    def to_dict(self):
        settings = {}
        if self.job_settings_json:
            try:
                settings = json.loads(self.job_settings_json)
            except Exception:
                pass
        return {
            "id": self.id,
            "journey_id": self.journey_id,
            "timetable_id": self.timetable_id,
            "status": self.status,
            "current_stop_index": self.current_stop_index,
            "current_stop_number": self.current_stop_number,
            "current_stop_name": self.current_stop_name,
            "current_angle": self.current_angle,
            "total_stops": self.total_stops,
            "processed_stops": self.processed_stops,
            "remaining_stops": self.remaining_stops,
            "failed_stops": self.failed_stops,
            "skipped_stops": self.skipped_stops,
            "fully_covered_stops": self.fully_covered_stops,
            "partially_covered_stops": self.partially_covered_stops,
            "not_covered_stops": self.not_covered_stops,
            "coverage_percentage": round((self.fully_covered_stops / self.total_stops * 100), 1) if self.total_stops > 0 else 0.0,
            "total_unique_passengers_estimate": self.total_unique_passengers_estimate,
            "total_confirmed_identities": self.total_confirmed_identities,
            "progress_percent": round(self.progress_percent, 1),
            "job_settings": settings,
            "error_message": self.error_message,
            "started_at": self.started_at.strftime("%Y-%m-%d %H:%M:%S") if self.started_at else None,
            "completed_at": self.completed_at.strftime("%Y-%m-%d %H:%M:%S") if self.completed_at else None,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None
        }



