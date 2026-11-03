from datetime import datetime
import sqlite3
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text, event
from sqlalchemy.engine import Engine

db = SQLAlchemy()

@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """Configures SQLite WAL mode and busy timeout to avoid 'database is locked' errors."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=60000")
            cursor.close()
        except Exception:
            pass

def run_migrations(app):
    """
    Safely adds any missing columns or tables without dropping existing data.
    """
    with app.app_context():
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()

        # Check and migrate 'buses' table columns
        if "buses" in tables:
            bus_cols = {col["name"] for col in inspector.get_columns("buses")}
            bus_cols_to_add = [
                ("registration_number", "VARCHAR(60)"),
                ("bus_name", "VARCHAR(100)"),
                ("bus_type", "VARCHAR(50) DEFAULT 'Standard Transit'"),
                ("route_number", "VARCHAR(50)"),
                ("starting_point", "VARCHAR(100)"),
                ("destination", "VARCHAR(100)"),
                ("operator_name", "VARCHAR(120)"),
                ("operator_contact", "VARCHAR(50)"),
                ("bus_status", "VARCHAR(30) DEFAULT 'Active'"),
                ("assigned_operator_id", "INTEGER"),
                ("latest_latitude", "FLOAT"),
                ("latest_longitude", "FLOAT"),
                ("latest_gps_accuracy", "FLOAT"),
                ("latest_location_source", "VARCHAR(30)"),
                ("latest_location_time", "TIMESTAMP"),
                ("last_activity_time", "TIMESTAMP"),
            ]
            with db.engine.connect() as conn:
                for col_name, col_type in bus_cols_to_add:
                    if col_name not in bus_cols:
                        try:
                            conn.execute(text(f"ALTER TABLE buses ADD COLUMN {col_name} {col_type}"))
                            conn.commit()
                        except Exception as e:
                            print(f"[Migration Warning] Adding column {col_name} to buses: {e}")

        # Check and migrate 'users' table columns
        if "users" in tables:
            user_cols = {col["name"] for col in inspector.get_columns("users")}
            if "current_bus_id" not in user_cols:
                with db.engine.connect() as conn:
                    try:
                        conn.execute(text("ALTER TABLE users ADD COLUMN current_bus_id INTEGER"))
                        conn.commit()
                    except Exception as e:
                        print(f"[Migration Warning] Adding current_bus_id to users: {e}")

        # Check and migrate 'passenger_identities' table columns
        if "passenger_identities" in tables:
            p_cols = {col["name"] for col in inspector.get_columns("passenger_identities")}
            with db.engine.connect() as conn:
                if "current_seat" not in p_cols:
                    try:
                        conn.execute(text("ALTER TABLE passenger_identities ADD COLUMN current_seat VARCHAR(10)"))
                        conn.commit()
                    except Exception as e:
                        print(f"[Migration Warning] Adding current_seat to passenger_identities: {e}")
                if "previous_seat" not in p_cols:
                    try:
                        conn.execute(text("ALTER TABLE passenger_identities ADD COLUMN previous_seat VARCHAR(10)"))
                        conn.commit()
                    except Exception as e:
                        print(f"[Migration Warning] Adding previous_seat to passenger_identities: {e}")
                if "boarding_stop_id" not in p_cols:
                    try:
                        conn.execute(text("ALTER TABLE passenger_identities ADD COLUMN boarding_stop_id INTEGER"))
                        conn.commit()
                    except Exception as e:
                        print(f"[Migration Warning] Adding boarding_stop_id to passenger_identities: {e}")
                if "exit_stop_id" not in p_cols:
                    try:
                        conn.execute(text("ALTER TABLE passenger_identities ADD COLUMN exit_stop_id INTEGER"))
                        conn.commit()
                    except Exception as e:
                        print(f"[Migration Warning] Adding exit_stop_id to passenger_identities: {e}")
                if "canonical_identity_id" not in p_cols:
                    try:
                        conn.execute(text("ALTER TABLE passenger_identities ADD COLUMN canonical_identity_id INTEGER"))
                        conn.commit()
                    except Exception as e:
                        print(f"[Migration Warning] Adding canonical_identity_id to passenger_identities: {e}")
                if "canonical_person_id" not in p_cols:
                    try:
                        conn.execute(text("ALTER TABLE passenger_identities ADD COLUMN canonical_person_id VARCHAR(30)"))
                        conn.commit()
                    except Exception as e:
                        print(f"[Migration Warning] Adding canonical_person_id to passenger_identities: {e}")
                if "is_merged" not in p_cols:
                    try:
                        conn.execute(text("ALTER TABLE passenger_identities ADD COLUMN is_merged BOOLEAN DEFAULT 0"))
                        conn.commit()
                    except Exception as e:
                        print(f"[Migration Warning] Adding is_merged to passenger_identities: {e}")
                if "merged_at" not in p_cols:
                    try:
                        conn.execute(text("ALTER TABLE passenger_identities ADD COLUMN merged_at TIMESTAMP"))
                        conn.commit()
                    except Exception as e:
                        print(f"[Migration Warning] Adding merged_at to passenger_identities: {e}")

        # Check and migrate 'uploads' table columns
        if "uploads" in tables:
            upload_cols = {col["name"] for col in inspector.get_columns("uploads")}
            upload_cols_to_add = [
                ("bus_id", "INTEGER"),
                ("camera_angle", "VARCHAR(10) DEFAULT 'A'"),
                ("batch_id", "VARCHAR(64)"),
                ("uncertain_detections_count", "INTEGER DEFAULT 0"),
                ("is_color", "BOOLEAN DEFAULT 1"),
                ("color_score", "FLOAT"),
                ("skip_reason", "TEXT"),
                ("color_classification_details_json", "TEXT"),
                ("error_details", "TEXT")
            ]
            with db.engine.connect() as conn:
                for col_name, col_type in upload_cols_to_add:
                    if col_name not in upload_cols:
                        try:
                            conn.execute(text(f"ALTER TABLE uploads ADD COLUMN {col_name} {col_type}"))
                            conn.commit()
                        except Exception as e:
                            print(f"[Migration Warning] Adding column {col_name} to uploads: {e}")

        # Check and migrate 'stop_media_matches' table columns
        if "stop_media_matches" in tables:
            match_cols = {col["name"] for col in inspector.get_columns("stop_media_matches")}
            match_cols_to_add = [
                ("is_color", "BOOLEAN DEFAULT 1"),
                ("color_score", "FLOAT"),
                ("skip_reason", "TEXT")
            ]
            with db.engine.connect() as conn:
                for col_name, col_type in match_cols_to_add:
                    if col_name not in match_cols:
                        try:
                            conn.execute(text(f"ALTER TABLE stop_media_matches ADD COLUMN {col_name} {col_type}"))
                            conn.commit()
                        except Exception as e:
                            print(f"[Migration Warning] Adding column {col_name} to stop_media_matches: {e}")

        # Check and migrate 'source_media_files' table columns
        if "source_media_files" in tables:
            smf_cols = {col["name"] for col in inspector.get_columns("source_media_files")}
            smf_cols_to_add = [
                ("is_color", "BOOLEAN DEFAULT 1"),
                ("color_score", "FLOAT"),
                ("skip_reason", "TEXT")
            ]
            with db.engine.connect() as conn:
                for col_name, col_type in smf_cols_to_add:
                    if col_name not in smf_cols:
                        try:
                            conn.execute(text(f"ALTER TABLE source_media_files ADD COLUMN {col_name} {col_type}"))
                            conn.commit()
                        except Exception as e:
                            print(f"[Migration Warning] Adding column {col_name} to source_media_files: {e}")

        # Check and migrate 'passenger_observations' table columns
        if "passenger_observations" in tables:
            obs_cols = {col["name"] for col in inspector.get_columns("passenger_observations")}
            obs_cols_to_add = [
                ("camera_angle", "VARCHAR(10) DEFAULT 'A'"),
                ("temp_detection_id", "VARCHAR(50)"),
                ("detection_confidence", "FLOAT DEFAULT 0.0"),
                ("review_status", "VARCHAR(30) DEFAULT 'CONFIRMED'"),
                ("is_potential_duplicate", "BOOLEAN DEFAULT 0"),
                ("duplicate_review_status", "VARCHAR(30)"),
                ("face_embedding_json", "TEXT"),
                ("embedding_model_version", "VARCHAR(50) DEFAULT 'facenet_vggface2_512'")
            ]
            with db.engine.connect() as conn:
                for col_name, col_type in obs_cols_to_add:
                    if col_name not in obs_cols:
                        try:
                            conn.execute(text(f"ALTER TABLE passenger_observations ADD COLUMN {col_name} {col_type}"))
                            conn.commit()
                        except Exception as e:
                            print(f"[Migration Warning] Adding column {col_name} to passenger_observations: {e}")

        # Check and migrate 'passenger_identities' table columns
        if "passenger_identities" in tables:
            pid_cols = {col["name"] for col in inspector.get_columns("passenger_identities")}
            pid_cols_to_add = [
                ("boarding_stop_name", "VARCHAR(255)"),
                ("exit_stop_name", "VARCHAR(255)"),
                ("boarding_stop_number", "INTEGER"),
                ("exit_stop_number", "INTEGER"),
                ("stops_traveled", "INTEGER DEFAULT 1"),
                ("traveled_stops_json", "TEXT"),
                ("gallery_embeddings_json", "TEXT"),
                ("embedding_model_version", "VARCHAR(50) DEFAULT 'facenet_vggface2_512'"),
                ("first_observed_angle", "VARCHAR(10)"),
                ("last_observed_angle", "VARCHAR(10)"),
                ("detected_angles_json", "TEXT"),
                ("observed_interval_seconds", "FLOAT DEFAULT 0.0"),
                ("uncertain_match", "BOOLEAN DEFAULT 0")
            ]
            with db.engine.connect() as conn:
                for col_name, col_type in pid_cols_to_add:
                    if col_name not in pid_cols:
                        try:
                            conn.execute(text(f"ALTER TABLE passenger_identities ADD COLUMN {col_name} {col_type}"))
                            conn.commit()
                        except Exception as e:
                            print(f"[Migration Warning] Adding column {col_name} to passenger_identities: {e}")

        # Check and migrate 'timetable_stops' table columns
        if "timetable_stops" in tables:
            tt_cols = {col["name"] for col in inspector.get_columns("timetable_stops")}
            tt_cols_to_add = [
                ("boarded_count", "INTEGER DEFAULT 0"),
                ("exited_count", "INTEGER DEFAULT 0"),
                ("continuing_count", "INTEGER DEFAULT 0"),
                ("occupancy", "INTEGER DEFAULT 0")
            ]
            with db.engine.connect() as conn:
                for col_name, col_type in tt_cols_to_add:
                    if col_name not in tt_cols:
                        try:
                            conn.execute(text(f"ALTER TABLE timetable_stops ADD COLUMN {col_name} {col_type}"))
                            conn.commit()
                        except Exception as e:
                            print(f"[Migration Warning] Adding column {col_name} to timetable_stops: {e}")

        # Create any new tables (operator_bus_assignments, bus_location_logs, camera_angle_configs, route_stops)
        db.create_all()

        # Seed default CameraAngleConfig if empty
        try:
            from app.models import CameraAngleConfig
            if CameraAngleConfig.query.count() == 0:
                defaults = [
                    CameraAngleConfig(
                        camera_angle="A",
                        title="Angle A — Front / Assigned Seating Section",
                        seating_area_label="Front Section (Rows 1-2 / Seats 1A-2D)",
                        target_seats="1A, 1B, 1C, 1D",
                        target_capacity=4,
                        roi_json='{"x_min": 0.0, "y_min": 0.0, "x_max": 1.0, "y_max": 1.0}'
                    ),
                    CameraAngleConfig(
                        camera_angle="B",
                        title="Angle B — Middle / Assigned Seating Section",
                        seating_area_label="Middle Section (Rows 3-4 / Seats 3A-4D)",
                        target_seats="2A, 2B, 2C, 2D",
                        target_capacity=4,
                        roi_json='{"x_min": 0.0, "y_min": 0.0, "x_max": 1.0, "y_max": 1.0}'
                    ),
                    CameraAngleConfig(
                        camera_angle="C",
                        title="Angle C — Rear / Assigned Seating Section",
                        seating_area_label="Rear Section (Rows 5-6 / Seats 5A-6D)",
                        target_seats="3A, 3B, 3C, 3D",
                        target_capacity=4,
                        roi_json='{"x_min": 0.0, "y_min": 0.0, "x_max": 1.0, "y_max": 1.0}'
                    )
                ]
                db.session.add_all(defaults)
                db.session.commit()
        except Exception as e:
            print(f"[Migration Warning] Seeding camera_angle_configs: {e}")
            db.session.rollback()

        # 31 Standard Corridor Transit Stops Master Dataset
        raw_stops = [
            {"order": 1, "name": "Koshambi Bus Stand", "time": "15-10-2026 | 06:01:20 PM - 06:01:30 PM", "speed": 3.0, "lat": 28.64720, "lon": 77.31890, "address": "Koshambi Bus Stand, Plot Alpha, Sahibabad Industrial Area Site 4, Sahibabad, Ghaziabad, Uttar Pradesh 201010, India", "major": True},
            {"order": 2, "name": "Plot No. 1, Dr Burman Rd", "time": "15-10-2026 | 06:37:26 PM - 06:37:48 PM", "speed": 3.0, "lat": 28.64765, "lon": 77.32040, "address": "Plot No. 1, Dr Burman Rd, Plot Alpha, Sahibabad Industrial Area Site 4, Sahibabad, Ghaziabad, Uttar Pradesh 201010, India", "major": False},
            {"order": 3, "name": "Plot Alpha, Sahibabad Site 4", "time": "15-10-2026 | 06:37:58 PM", "speed": 6.0, "lat": 28.64790, "lon": 77.32160, "address": "Plot Alpha, Sahibabad Industrial Area Site 4, Sahibabad, Ghaziabad, Uttar Pradesh, India", "major": False},
            {"order": 4, "name": "20/2/7, Dr Burman Road", "time": "15-10-2026 | 06:38:09 PM", "speed": 3.0, "lat": 28.64810, "lon": 77.32280, "address": "20/2/7, Doctor Burman Road, Plot Alpha, Sahibabad Industrial Area Site 4, Sahibabad, Ghaziabad, Uttar Pradesh, India", "major": False},
            {"order": 5, "name": "Dr Burman Road Junction", "time": "15-10-2026 | 06:38:19 PM", "speed": 5.0, "lat": 28.64830, "lon": 77.32390, "address": "20/2/7, Doctor Burman Road, Plot Alpha, Sahibabad Industrial Area Site 4, Sahibabad, Ghaziabad, Uttar Pradesh, India", "major": False},
            {"order": 6, "name": "Pacific Mall Entry", "time": "15-10-2026 | 06:38:29 PM", "speed": 10.0, "lat": 28.64855, "lon": 77.32510, "address": "Pacific Mall, Plot Alpha, Sahibabad Industrial Area Site 4, Sahibabad, Ghaziabad, Uttar Pradesh, India", "major": True},
            {"order": 7, "name": "Pacific Mall, Plot No. 1", "time": "15-10-2026 | 06:38:39 PM", "speed": 11.0, "lat": 28.64880, "lon": 77.32630, "address": "Pacific Mall, Plot No. 1, Dr Burman Rd, Plot Alpha, Sahibabad Industrial Area Site 4, Sahibabad, Ghaziabad, Uttar Pradesh 201010, India", "major": False},
            {"order": 8, "name": "Dr Burman Rd North", "time": "15-10-2026 | 06:38:49 PM", "speed": 15.0, "lat": 28.64910, "lon": 77.32750, "address": "Plot No. 1, Dr Burman Rd, Plot Alpha, Sahibabad Industrial Area Site 4, Sahibabad, Ghaziabad, Uttar Pradesh 201010, India", "major": False},
            {"order": 9, "name": "45, Doctor Burman Road", "time": "15-10-2026 | 06:39:07 PM", "speed": 11.0, "lat": 28.64890, "lon": 77.32870, "address": "45, Doctor Burman Road, Kaushambi, Ghaziabad, Uttar Pradesh, India, 201010", "major": False},
            {"order": 10, "name": "J8WC+8R Site 4 Corner", "time": "15-10-2026 | 06:39:13 PM", "speed": 8.0, "lat": 28.64860, "lon": 77.32980, "address": "J8WC+8R, Plot Alpha, Sahibabad Industrial Area Site 4, Sahibabad, Ghaziabad, Uttar Pradesh, India, 201012", "major": False},
            {"order": 11, "name": "Maharajpur Industrial", "time": "15-10-2026 | 06:39:23 PM", "speed": 11.0, "lat": 28.64810, "lon": 77.33080, "address": "Maharajpur, Sahibabad Industrial Area Site 4, Sahibabad, Ghaziabad, Uttar Pradesh, India", "major": False},
            {"order": 12, "name": "Kaushambi Link", "time": "15-10-2026 | 06:39:33 PM", "speed": 15.0, "lat": 28.64750, "lon": 77.33170, "address": "Kaushambi, Ghaziabad, Uttar Pradesh, India, 201012", "major": False},
            {"order": 13, "name": "Kaushambi Metro (McD)", "time": "15-10-2026 | 06:39:53 PM", "speed": 15.0, "lat": 28.64680, "lon": 77.33260, "address": "Kaushambi Metro (McD), Maharajpur, Sahibabad Industrial Area Site 4, Sahibabad, Ghaziabad, Uttar Pradesh, India, 201012", "major": True},
            {"order": 14, "name": "Dr Burman Rd / Maharajpur", "time": "15-10-2026 | 06:41:11 PM", "speed": 11.0, "lat": 28.64590, "lon": 77.33330, "address": "Dr Burman Rd, Maharajpur, Sahibabad Industrial Area Site 4, Sahibabad, Ghaziabad, Uttar Pradesh 201010, India", "major": False},
            {"order": 15, "name": "Neelpadam Kunj 23, MMM Marg", "time": "15-10-2026 | 06:42:34 PM", "speed": 5.0, "lat": 28.64490, "lon": 77.33390, "address": "23, Madan Mohan Malviya Marg, Neelpadam Kunj Complex, Kamma 1, Vaishali, Ghaziabad, Uttar Pradesh 201012, India", "major": False},
            {"order": 16, "name": "Neelpadan Kunj Complex", "time": "15-10-2026 | 06:42:39 PM", "speed": 6.0, "lat": 28.64420, "lon": 77.33440, "address": "23, Madan Mohan Malviya Marg, Neelpadan Kunj Complex, Kamma 1, Vaishali, Ghaziabad, Uttar Pradesh 201012, India", "major": False},
            {"order": 17, "name": "45, Dr Burman Rd Return", "time": "15-10-2026 | 06:42:49 PM - 06:42:51 PM", "speed": 5.0, "lat": 28.64360, "lon": 77.33480, "address": "45, Dr Burman Rd, Kaushambi, Ghaziabad, Uttar Pradesh 201010, India", "major": False},
            {"order": 18, "name": "Maharajpur Crossing", "time": "15-10-2026 | 06:42:54 PM", "speed": 8.0, "lat": 28.64300, "lon": 77.33520, "address": "Maharajpur, Sahibabad Industrial Area Site 4, Sahibabad, Ghaziabad, Uttar Pradesh, India", "major": False},
            {"order": 19, "name": "45, Dr Burman South", "time": "15-10-2026 | 06:42:56 PM", "speed": 10.0, "lat": 28.64250, "lon": 77.33560, "address": "45, Dr Burman Rd, Kaushambi, Ghaziabad, Uttar Pradesh 201010, India", "major": False},
            {"order": 20, "name": "Dr Burman Rd Link", "time": "15-10-2026 | 06:42:58 PM", "speed": 11.0, "lat": 28.64200, "lon": 77.33600, "address": "Dr Burman Rd, Kaushambi, Ghaziabad, Uttar Pradesh 201010, India", "major": False},
            {"order": 21, "name": "Neelpadam Kunj, Kamma 1", "time": "15-10-2026 | 06:43:01 PM", "speed": 10.0, "lat": 28.64150, "lon": 77.33640, "address": "Neelpadam Kunj Complex, Kamma 1, Vaishali, Ghaziabad, Uttar Pradesh, India", "major": False},
            {"order": 22, "name": "Neelpadam Kunj Gate", "time": "15-10-2026 | 06:43:04 PM", "speed": 10.0, "lat": 28.64100, "lon": 77.33680, "address": "Neelpadam Kunj Complex, Kamma 1, Vaishali, Ghaziabad, Uttar Pradesh, India", "major": False},
            {"order": 23, "name": "Goyal Market / Sector 14 Vaishali", "time": "15-10-2026 | 06:45:14 PM", "speed": 9.0, "lat": 28.64020, "lon": 77.33750, "address": "Shop No:14/12, Madan Mohan Malviya Marg, Goyal Market, Neelam Vihar Society, Sector 14, Vaishali, Ghaziabad, Uttar Pradesh 201010, India", "major": True},
            {"order": 24, "name": "Vaishali Modipon Vihar Park", "time": "15-10-2026 | 06:45:27 PM - 06:45:38 PM", "speed": 8.0, "lat": 28.63960, "lon": 77.33810, "address": "Vaishali, Ghaziabad, Uttar Pradesh (Modipon Vihar Park / Sector 3)", "major": False},
            {"order": 25, "name": "Suman Petroleum / Link Road", "time": "15-10-2026 | 06:45:58 PM", "speed": 13.0, "lat": 28.63910, "lon": 77.33870, "address": "Link Road, Suman Petroleum, Vaishali, Ghaziabad, Uttar Pradesh 201010, India", "major": False},
            {"order": 26, "name": "MMM Marg, Kamma 1 North", "time": "15-10-2026 | 06:46:11 PM - 06:46:25 PM", "speed": 3.0, "lat": 28.63870, "lon": 77.33920, "address": "Madan Mohan Malviya Marg, Kamma 1, Vaishali, Delhi, Uttar Pradesh 201012, India", "major": False},
            {"order": 27, "name": "MMM Marg, Kamma 1 Mid", "time": "15-10-2026 | 06:47:39 PM", "speed": 3.0, "lat": 28.63840, "lon": 77.33960, "address": "Madan Mohan Malviya Marg, Kamma 1, Vaishali, Delhi, Uttar Pradesh 201012, India", "major": False},
            {"order": 28, "name": "Kamma 1 Central", "time": "15-10-2026 | 06:47:49 PM - 06:48:09 PM", "speed": 11.0, "lat": 28.63810, "lon": 77.34000, "address": "Kamma 1, Vaishali, Ghaziabad, Uttar Pradesh, India", "major": False},
            {"order": 29, "name": "Ashok Marg, Vaishali", "time": "15-10-2026 | 06:48:19 PM", "speed": 7.0, "lat": 28.63770, "lon": 77.34050, "address": "Ashok Marg, Kamma 1, Vaishali, Ghaziabad, Uttar Pradesh 201012, India", "major": False},
            {"order": 30, "name": "Sector-1 Vaishali Entry", "time": "15-10-2026 | 06:48:31 PM", "speed": 6.0, "lat": 28.63730, "lon": 77.34110, "address": "Madan Mohan Malviya Marg, Sector-1, Vaishali, Ghaziabad, Uttar Pradesh 201012, India", "major": False},
            {"order": 31, "name": "Sector-1 Vaishali Terminal", "time": "15-10-2026 | 06:49:18 PM", "speed": 11.0, "lat": 28.63690, "lon": 77.34170, "address": "Sector-1, Vaishali, Ghaziabad, Uttar Pradesh 201010, India", "major": True}
        ]

        # Seed MasterRouteStop library (admin-managed master stations)
        try:
            from app.models import MasterRouteStop
            db.create_all()
            if MasterRouteStop.query.count() == 0:
                for item in raw_stops:
                    ms = MasterRouteStop(
                        stop_name=item["name"],
                        location_address=item["address"],
                        default_speed_kmh=item["speed"],
                        latitude=item["lat"],
                        longitude=item["lon"],
                        is_major_stop=item["major"]
                    )
                    db.session.add(ms)
                db.session.commit()
        except Exception as e:
            print(f"[Migration Warning] Seeding master_route_stops: {e}")
            db.session.rollback()

        # Seed RouteStops (31 stops and speed telemetry) if empty
        try:
            from app.models import RouteStop, Bus
            first_bus = Bus.query.first()
            bus_id_val = first_bus.id if first_bus else None

            if RouteStop.query.count() == 0:
                for item in raw_stops:
                    st = RouteStop(
                        bus_id=bus_id_val,
                        stop_order=item["order"],
                        stop_name=item["name"],
                        location_address=item["address"],
                        time_window=item["time"],
                        speed_kmh=item["speed"],
                        latitude=item["lat"],
                        longitude=item["lon"],
                        is_major_stop=item["major"]
                    )
                    db.session.add(st)
                db.session.commit()
            # Ensure first_bus has all 31 baseline corridor stops
            if bus_id_val:
                existing_orders = set(s.stop_order for s in RouteStop.query.filter_by(bus_id=bus_id_val).all())
                for item in raw_stops:
                    if item["order"] not in existing_orders:
                        st = RouteStop(
                            bus_id=bus_id_val,
                            stop_order=item["order"],
                            stop_name=item["name"],
                            location_address=item["address"],
                            time_window=item["time"],
                            speed_kmh=item["speed"],
                            latitude=item["lat"],
                            longitude=item["lon"],
                            is_major_stop=item["major"]
                        )
                        db.session.add(st)
                db.session.commit()
        except Exception as e:
            print(f"[Migration Warning] Seeding route_stops: {e}")
            db.session.rollback()

        # Seed MasterUPRoute library (Major Uttar Pradesh transit corridors)
        try:
            from app.models import MasterUPRoute
            db.create_all()
            if MasterUPRoute.query.count() == 0:
                up_routes_seed = [
                    {
                        "route_code": "UP-RT-01",
                        "route_name": "Delhi/NCR (Kaushambi) — Moradabad — Bareilly — Lucknow Express",
                        "region": "Central & Western UP (NH-9 / NH-24 / NH-30)",
                        "origin": "Kaushambi Bus Stand, Ghaziabad",
                        "destination": "Alambagh Bus Terminal, Lucknow",
                        "total_distance_km": 520.0,
                        "estimated_duration_hours": 8.5,
                        "avg_speed_kmh": 11.5,
                        "stops": [
                            {"order": 1, "name": "Kaushambi ISBT Bus Stand, Ghaziabad", "address": "Kaushambi Bus Stand, Sahibabad, Ghaziabad, UP", "speed": 3.0, "time": "06:00 AM", "lat": 28.6472, "lon": 77.3189, "major": True},
                            {"order": 2, "name": "Sahibabad Site 4 Transit Post", "address": "Site 4 Industrial Area, Sahibabad, Ghaziabad, UP", "speed": 6.0, "time": "06:20 AM", "lat": 28.6441, "lon": 77.3240, "major": False},
                            {"order": 3, "name": "Lal Kuan Crossing, Ghaziabad", "address": "Lal Kuan, GT Road NH-9, Ghaziabad, UP", "speed": 8.0, "time": "06:45 AM", "lat": 28.6420, "lon": 77.4320, "major": True},
                            {"order": 4, "name": "Hapur Bypass Transit Stop", "address": "Hapur Bypass, NH-9, Hapur, UP", "speed": 12.0, "time": "07:25 AM", "lat": 28.7305, "lon": 77.7760, "major": True},
                            {"order": 5, "name": "Garhmukteshwar Ganga Bridge Depot", "address": "Garhmukteshwar, Brijghat, Hapur, UP", "speed": 10.0, "time": "08:00 AM", "lat": 28.7890, "lon": 78.1020, "major": False},
                            {"order": 6, "name": "Gajraula Bus Station", "address": "Gajraula Junction, Amroha, UP", "speed": 11.0, "time": "08:35 AM", "lat": 28.8470, "lon": 78.2380, "major": True},
                            {"order": 7, "name": "Moradabad Imperial Bus Terminal", "address": "Imperial Tiraha, Moradabad, UP", "speed": 9.0, "time": "09:40 AM", "lat": 28.8386, "lon": 78.7733, "major": True},
                            {"order": 8, "name": "Rampur City Station", "address": "Rampur Bus Stand, Rampur, UP", "speed": 10.0, "time": "10:30 AM", "lat": 28.8040, "lon": 79.0270, "major": True},
                            {"order": 9, "name": "Bareilly Satellite Bus Terminal", "address": "Satellite Bus Station, Bareilly, UP", "speed": 8.0, "time": "11:45 AM", "lat": 28.3670, "lon": 79.4304, "major": True},
                            {"order": 10, "name": "Shahjahanpur Bus Depot", "address": "Roza Bypass / Bus Stand, Shahjahanpur, UP", "speed": 10.0, "time": "01:00 PM", "lat": 27.8810, "lon": 79.9120, "major": True},
                            {"order": 11, "name": "Sitapur Road Transit Hub", "address": "Sitapur Bus Stand, Sitapur, UP", "speed": 11.0, "time": "02:15 PM", "lat": 27.5680, "lon": 80.6820, "major": True},
                            {"order": 12, "name": "Alambagh Bus Terminal, Lucknow", "address": "Alambagh ISBT, Lucknow, Uttar Pradesh", "speed": 5.0, "time": "03:30 PM", "lat": 26.8190, "lon": 80.9020, "major": True}
                        ]
                    },
                    {
                        "route_code": "UP-RT-02",
                        "route_name": "Lucknow — Kanpur — Fatehpur — Prayagraj — Varanasi Express",
                        "region": "Central & Eastern UP (NH-19 / GT Road)",
                        "origin": "Charbagh Central Station, Lucknow",
                        "destination": "Cantt Bus Terminal, Varanasi",
                        "total_distance_km": 320.0,
                        "estimated_duration_hours": 6.0,
                        "avg_speed_kmh": 11.0,
                        "stops": [
                            {"order": 1, "name": "Charbagh Central Station, Lucknow", "address": "Charbagh Bus Depot, Lucknow, UP", "speed": 4.0, "time": "07:00 AM", "lat": 26.8320, "lon": 80.9180, "major": True},
                            {"order": 2, "name": "Unnao Bypass Junction", "address": "Unnao Bypass, NH-27, Unnao, UP", "speed": 9.0, "time": "07:50 AM", "lat": 26.5430, "lon": 80.4890, "major": True},
                            {"order": 3, "name": "Jhakarkati Bus Terminal, Kanpur", "address": "Jhakarkati Bus Stand, Kanpur Nagar, UP", "speed": 7.0, "time": "08:45 AM", "lat": 26.4520, "lon": 80.3520, "major": True},
                            {"order": 4, "name": "Fatehpur Town Bus Stand", "address": "GT Road, Fatehpur, Uttar Pradesh", "speed": 10.0, "time": "10:00 AM", "lat": 25.9280, "lon": 80.8120, "major": True},
                            {"order": 5, "name": "Khaga Transit Point", "address": "Khaga, NH-19, Fatehpur District, UP", "speed": 12.0, "time": "10:45 AM", "lat": 25.7720, "lon": 81.1080, "major": False},
                            {"order": 6, "name": "Civil Lines Bus Depot, Prayagraj", "address": "Civil Lines Bus Stand, Prayagraj (Allahabad), UP", "speed": 8.0, "time": "11:50 AM", "lat": 25.4520, "lon": 81.8340, "major": True},
                            {"order": 7, "name": "Handia Highway Junction", "address": "Handia Bypass, NH-19, Prayagraj, UP", "speed": 11.0, "time": "12:35 PM", "lat": 25.3620, "lon": 82.1890, "major": False},
                            {"order": 8, "name": "Gopiganj Station, Bhadohi", "address": "GT Road, Gopiganj, Bhadohi, UP", "speed": 10.0, "time": "01:10 PM", "lat": 25.2890, "lon": 82.4350, "major": True},
                            {"order": 9, "name": "Cantt Bus Terminal, Varanasi", "address": "Varanasi Cantt ISBT, Varanasi, UP", "speed": 5.0, "time": "02:00 PM", "lat": 25.3280, "lon": 82.9870, "major": True}
                        ]
                    },
                    {
                        "route_code": "UP-RT-03",
                        "route_name": "Noida / Greater Noida — Yamuna Expressway — Mathura — Agra Heritage Corridor",
                        "region": "Braj & NCR Yamuna Expressway",
                        "origin": "Botanical Garden Terminal, Noida",
                        "destination": "Idgah Bus Terminal, Agra",
                        "total_distance_km": 205.0,
                        "estimated_duration_hours": 3.5,
                        "avg_speed_kmh": 13.5,
                        "stops": [
                            {"order": 1, "name": "Botanical Garden Bus Terminal, Noida", "address": "Sector 38, Noida, Uttar Pradesh", "speed": 4.0, "time": "06:30 AM", "lat": 28.5645, "lon": 77.3340, "major": True},
                            {"order": 2, "name": "Pari Chowk Express Depot, Greater Noida", "address": "Pari Chowk, Greater Noida, UP", "speed": 8.0, "time": "07:00 AM", "lat": 28.4630, "lon": 77.5110, "major": True},
                            {"order": 3, "name": "Jewar Toll Plaza Hub", "address": "Yamuna Expressway Km 38, Jewar, UP", "speed": 14.0, "time": "07:35 AM", "lat": 28.1280, "lon": 77.5620, "major": True},
                            {"order": 4, "name": "Tappal Transit Rest Area", "address": "Tappal Cut, Yamuna Expressway, Aligarh, UP", "speed": 15.0, "time": "08:00 AM", "lat": 27.9420, "lon": 77.6320, "major": False},
                            {"order": 5, "name": "Mathura Raya Cut Junction", "address": "Yamuna Expressway Mathura Exit, UP", "speed": 11.0, "time": "08:40 AM", "lat": 27.5420, "lon": 77.7820, "major": True},
                            {"order": 6, "name": "Khandauli Interchange", "address": "Agra Ring Road Cut, Khandauli, UP", "speed": 12.0, "time": "09:15 AM", "lat": 27.2890, "lon": 77.9890, "major": False},
                            {"order": 7, "name": "ISBT Transport Nagar, Agra", "address": "Transport Nagar, Agra, UP", "speed": 8.0, "time": "09:40 AM", "lat": 27.2180, "lon": 77.9710, "major": True},
                            {"order": 8, "name": "Idgah Bus Terminal, Agra", "address": "Idgah Bus Stand, Agra Cantt, Uttar Pradesh", "speed": 5.0, "time": "10:00 AM", "lat": 27.1650, "lon": 77.9940, "major": True}
                        ]
                    },
                    {
                        "route_code": "UP-RT-04",
                        "route_name": "Lucknow — Ayodhya Dham — Basti — Khalilabad — Gorakhpur Fast Transit",
                        "region": "Awadh & Purvanchal (NH-27)",
                        "origin": "Kaiserbagh Bus Station, Lucknow",
                        "destination": "Gorakhpur Railway Bus Depot",
                        "total_distance_km": 275.0,
                        "estimated_duration_hours": 5.0,
                        "avg_speed_kmh": 11.0,
                        "stops": [
                            {"order": 1, "name": "Kaiserbagh Bus Station, Lucknow", "address": "Kaiserbagh, Lucknow, Uttar Pradesh", "speed": 4.0, "time": "06:00 AM", "lat": 26.8570, "lon": 80.9320, "major": True},
                            {"order": 2, "name": "Barabanki Bus Stand", "address": "Budaun-Lucknow Road, Barabanki, UP", "speed": 8.0, "time": "06:40 AM", "lat": 26.9280, "lon": 81.1890, "major": True},
                            {"order": 3, "name": "Rudauli Transit Stop", "address": "NH-27 Rudauli, Ayodhya District, UP", "speed": 12.0, "time": "07:30 AM", "lat": 26.7520, "lon": 81.7510, "major": False},
                            {"order": 4, "name": "Ayodhya Dham ISBT, Ayodhya", "address": "Naya Ghat / Ram Ki Paidi, Ayodhya, UP", "speed": 7.0, "time": "08:15 AM", "lat": 26.7920, "lon": 82.2030, "major": True},
                            {"order": 5, "name": "Basti Central Bus Depot", "address": "Malviya Road, Basti, Uttar Pradesh", "speed": 9.0, "time": "09:20 AM", "lat": 26.8120, "lon": 82.7620, "major": True},
                            {"order": 6, "name": "Khalilabad Terminal, Sant Kabir Nagar", "address": "Old GT Road, Khalilabad, UP", "speed": 10.0, "time": "10:00 AM", "lat": 26.7780, "lon": 83.0720, "major": True},
                            {"order": 7, "name": "Sahjanwa Industrial Bypass", "address": "Sahjanwa, Gorakhpur, UP", "speed": 12.0, "time": "10:30 AM", "lat": 26.7620, "lon": 83.2420, "major": False},
                            {"order": 8, "name": "Gorakhpur Railway Bus Depot", "address": "Railway Station Road, Gorakhpur, UP", "speed": 5.0, "time": "11:00 AM", "lat": 26.7610, "lon": 83.3820, "major": True}
                        ]
                    },
                    {
                        "route_code": "UP-RT-05",
                        "route_name": "Ghaziabad — Meerut — Muzaffarnagar — Saharanpur Western UP Corridor",
                        "region": "Western UP (Delhi-Meerut Expressway / NH-334)",
                        "origin": "Purana Bus Adda, Ghaziabad",
                        "destination": "Saharanpur ISBT Terminal",
                        "total_distance_km": 170.0,
                        "estimated_duration_hours": 3.5,
                        "avg_speed_kmh": 10.5,
                        "stops": [
                            {"order": 1, "name": "Purana Bus Adda, Ghaziabad", "address": "Old Bus Stand, GT Road, Ghaziabad, UP", "speed": 4.0, "time": "07:00 AM", "lat": 28.6690, "lon": 77.4320, "major": True},
                            {"order": 2, "name": "Muradnagar Rapid Transit Point", "address": "Delhi Meerut Road, Muradnagar, UP", "speed": 9.0, "time": "07:25 AM", "lat": 28.7750, "lon": 77.5020, "major": False},
                            {"order": 3, "name": "Modinagar Central", "address": "Modinagar Bus Stand, Ghaziabad, UP", "speed": 8.0, "time": "07:45 AM", "lat": 28.8340, "lon": 77.5810, "major": True},
                            {"order": 4, "name": "Bhainsali Bus Terminal, Meerut", "address": "Delhi Road, Bhainsali, Meerut, UP", "speed": 6.0, "time": "08:15 AM", "lat": 28.9850, "lon": 77.7060, "major": True},
                            {"order": 5, "name": "Khatauli Bypass", "address": "NH-334 Khatauli, Muzaffarnagar, UP", "speed": 13.0, "time": "08:50 AM", "lat": 29.2780, "lon": 77.7220, "major": False},
                            {"order": 6, "name": "Muzaffarnagar Bus Depot", "address": "Circular Road, Muzaffarnagar, UP", "speed": 8.0, "time": "09:20 AM", "lat": 29.4720, "lon": 77.7080, "major": True},
                            {"order": 7, "name": "Deoband Transit Station", "address": "GT Road, Deoband, Saharanpur, UP", "speed": 10.0, "time": "09:55 AM", "lat": 29.6960, "lon": 77.6820, "major": True},
                            {"order": 8, "name": "Saharanpur ISBT Terminal", "address": "Ambala Road, Saharanpur, Uttar Pradesh", "speed": 5.0, "time": "10:30 AM", "lat": 29.9640, "lon": 77.5460, "major": True}
                        ]
                    },
                    {
                        "route_code": "UP-RT-06",
                        "route_name": "Agra — Kannauj — Etawah — Lucknow Expressway Link",
                        "region": "Agra-Lucknow Expressway (ALE Corridor)",
                        "origin": "Fatehabad Road Terminal, Agra",
                        "destination": "Alambagh Bus Terminal, Lucknow",
                        "total_distance_km": 302.0,
                        "estimated_duration_hours": 4.5,
                        "avg_speed_kmh": 13.0,
                        "stops": [
                            {"order": 1, "name": "Fatehabad Road Terminal, Agra", "address": "Taj Link Road, Agra, Uttar Pradesh", "speed": 5.0, "time": "08:00 AM", "lat": 27.1520, "lon": 78.0420, "major": True},
                            {"order": 2, "name": "Shikohabad Cut, Firozabad", "address": "ALE Km 55, Shikohabad, UP", "speed": 14.0, "time": "08:45 AM", "lat": 27.1120, "lon": 78.5820, "major": True},
                            {"order": 3, "name": "Saifai / Etawah Interchange", "address": "ALE Km 105, Saifai, Etawah, UP", "speed": 14.0, "time": "09:30 AM", "lat": 26.9580, "lon": 79.0340, "major": True},
                            {"order": 4, "name": "Kannauj Perfume City Hub", "address": "ALE Km 180, Kannauj, Uttar Pradesh", "speed": 12.0, "time": "10:20 AM", "lat": 27.0510, "lon": 79.9190, "major": True},
                            {"order": 5, "name": "Bangarmau Junction, Unnao", "address": "ALE Km 230, Bangarmau, Unnao, UP", "speed": 13.0, "time": "11:00 AM", "lat": 26.9020, "lon": 80.2180, "major": False},
                            {"order": 6, "name": "Mohan Cut, Lucknow Outskirts", "address": "ALE Toll Plaza, Mohan, Lucknow, UP", "speed": 11.0, "time": "11:50 AM", "lat": 26.8040, "lon": 80.7650, "major": False},
                            {"order": 7, "name": "Alambagh Bus Terminal, Lucknow", "address": "Alambagh ISBT, Lucknow, Uttar Pradesh", "speed": 5.0, "time": "12:30 PM", "lat": 26.8190, "lon": 80.9020, "major": True}
                        ]
                    },
                    {
                        "route_code": "UP-RT-07",
                        "route_name": "Varanasi — Jaunpur — Sultanpur — Lucknow Central Link",
                        "region": "Eastern-Central UP (NH-731)",
                        "origin": "Cantt Bus Terminal, Varanasi",
                        "destination": "Charbagh Central Station, Lucknow",
                        "total_distance_km": 290.0,
                        "estimated_duration_hours": 5.5,
                        "avg_speed_kmh": 10.5,
                        "stops": [
                            {"order": 1, "name": "Cantt Bus Terminal, Varanasi", "address": "Cantt Railway Station Bus Stop, Varanasi, UP", "speed": 5.0, "time": "06:30 AM", "lat": 25.3280, "lon": 82.9870, "major": True},
                            {"order": 2, "name": "Babatpur Airport Cut", "address": "NH-731 Babatpur, Varanasi, UP", "speed": 11.0, "time": "07:10 AM", "lat": 25.4480, "lon": 82.8590, "major": False},
                            {"order": 3, "name": "Jaunpur Bus Stand, Poly Line", "address": "Civil Lines, Jaunpur, Uttar Pradesh", "speed": 8.0, "time": "07:55 AM", "lat": 25.7460, "lon": 82.6840, "major": True},
                            {"order": 4, "name": "Badlapur Transit Station", "address": "Badlapur Market, Jaunpur, UP", "speed": 11.0, "time": "08:35 AM", "lat": 25.9180, "lon": 82.4680, "major": False},
                            {"order": 5, "name": "Sultanpur Bus Depot", "address": "Golaghat, Sultanpur, Uttar Pradesh", "speed": 8.0, "time": "09:20 AM", "lat": 26.2640, "lon": 82.0720, "major": True},
                            {"order": 6, "name": "Musafirkhana Hub", "address": "Musafirkhana, Amethi, UP", "speed": 10.0, "time": "10:00 AM", "lat": 26.3720, "lon": 81.7980, "major": False},
                            {"order": 7, "name": "Jagdishpur Industrial Point", "address": "BHEL Township Cut, Jagdishpur, UP", "speed": 12.0, "time": "10:35 AM", "lat": 26.4710, "lon": 81.6210, "major": True},
                            {"order": 8, "name": "Haidergarh Station", "address": "Haidergarh, Barabanki, UP", "speed": 11.0, "time": "11:15 AM", "lat": 26.5920, "lon": 81.3320, "major": False},
                            {"order": 9, "name": "Charbagh Central Station, Lucknow", "address": "Charbagh Bus Depot, Lucknow, UP", "speed": 5.0, "time": "12:00 PM", "lat": 26.8320, "lon": 80.9180, "major": True}
                        ]
                    },
                    {
                        "route_code": "UP-RT-08",
                        "route_name": "Prayagraj — Mirzapur — Chandauli — Varanasi Vindhya Express",
                        "region": "Vindhya & Southern Eastern UP (NH-35)",
                        "origin": "Civil Lines Bus Depot, Prayagraj",
                        "destination": "Cantt Bus Terminal, Varanasi",
                        "total_distance_km": 135.0,
                        "estimated_duration_hours": 3.0,
                        "avg_speed_kmh": 10.0,
                        "stops": [
                            {"order": 1, "name": "Civil Lines Bus Depot, Prayagraj", "address": "Civil Lines, Prayagraj, UP", "speed": 5.0, "time": "08:00 AM", "lat": 25.4520, "lon": 81.8340, "major": True},
                            {"order": 2, "name": "Naini Industrial Terminal", "address": "Naini Bridge Crossing, Prayagraj, UP", "speed": 8.0, "time": "08:25 AM", "lat": 25.3920, "lon": 81.8680, "major": True},
                            {"order": 3, "name": "Meja Road Junction", "address": "Meja Road, Prayagraj District, UP", "speed": 11.0, "time": "09:00 AM", "lat": 25.2140, "lon": 82.1180, "major": False},
                            {"order": 4, "name": "Vindhyachal Dham Bus Point", "address": "Vindhyachal Temple Road, Mirzapur, UP", "speed": 8.0, "time": "09:35 AM", "lat": 25.1680, "lon": 82.4980, "major": True},
                            {"order": 5, "name": "Mirzapur Bus Stand", "address": "Station Road, Mirzapur, Uttar Pradesh", "speed": 7.0, "time": "09:55 AM", "lat": 25.1480, "lon": 82.5690, "major": True},
                            {"order": 6, "name": "Chunar Fort Crossing", "address": "Chunar Town Bypass, Mirzapur, UP", "speed": 10.0, "time": "10:25 AM", "lat": 25.1240, "lon": 82.8760, "major": True},
                            {"order": 7, "name": "Ramnagar Fort Chowk", "address": "Ramnagar, Varanasi, UP", "speed": 7.0, "time": "10:45 AM", "lat": 25.2680, "lon": 83.0240, "major": False},
                            {"order": 8, "name": "Cantt Bus Terminal, Varanasi", "address": "Cantt Bus Depot, Varanasi, UP", "speed": 5.0, "time": "11:00 AM", "lat": 25.3280, "lon": 82.9870, "major": True}
                        ]
                    },
                    {
                        "route_code": "UP-RT-09",
                        "route_name": "Jhansi — Orai — Kanpur — Lucknow Bundelkhand Corridor",
                        "region": "Bundelkhand to Central UP (NH-27)",
                        "origin": "Jhansi Bus Stand, Elite Chauraha",
                        "destination": "Alambagh Bus Terminal, Lucknow",
                        "total_distance_km": 315.0,
                        "estimated_duration_hours": 6.0,
                        "avg_speed_kmh": 11.0,
                        "stops": [
                            {"order": 1, "name": "Elite Chauraha Bus Stand, Jhansi", "address": "Elite Crossing, Jhansi, UP", "speed": 5.0, "time": "06:00 AM", "lat": 25.4480, "lon": 78.5680, "major": True},
                            {"order": 2, "name": "Moth Transit Stop", "address": "NH-27 Moth, Jhansi District, UP", "speed": 12.0, "time": "06:45 AM", "lat": 25.7180, "lon": 78.9480, "major": False},
                            {"order": 3, "name": "Orai Central Bus Terminal", "address": "Konch Road, Orai, Jalaun, UP", "speed": 8.0, "time": "07:35 AM", "lat": 25.9920, "lon": 79.4520, "major": True},
                            {"order": 4, "name": "Kalpi Yamuna Bridge Station", "address": "Kalpi Ghat, Jalaun, UP", "speed": 10.0, "time": "08:15 AM", "lat": 26.1240, "lon": 79.7420, "major": True},
                            {"order": 5, "name": "Pukhrayan Depot, Kanpur Dehat", "address": "NH-27, Pukhrayan, UP", "speed": 11.0, "time": "08:50 AM", "lat": 26.2280, "lon": 79.8450, "major": False},
                            {"order": 6, "name": "Jhakarkati Bus Terminal, Kanpur", "address": "Jhakarkati, Kanpur Nagar, UP", "speed": 7.0, "time": "09:45 AM", "lat": 26.4520, "lon": 80.3520, "major": True},
                            {"order": 7, "name": "Unnao Bypass Junction", "address": "Unnao Bypass, NH-27, Unnao, UP", "speed": 11.0, "time": "10:40 AM", "lat": 26.5430, "lon": 80.4890, "major": False},
                            {"order": 8, "name": "Alambagh Bus Terminal, Lucknow", "address": "Alambagh ISBT, Lucknow, Uttar Pradesh", "speed": 5.0, "time": "12:00 PM", "lat": 26.8190, "lon": 80.9020, "major": True}
                        ]
                    },
                    {
                        "route_code": "UP-RT-10",
                        "route_name": "Kaushambi Bus Stand — Sahibabad Site 4 — Vaishali Sector-1 (NCR Urban Feeder)",
                        "region": "Ghaziabad NCR Feeder (Local Transit)",
                        "origin": "Kaushambi Bus Stand, Sahibabad",
                        "destination": "Sector-1 Vaishali Terminal",
                        "total_distance_km": 15.0,
                        "estimated_duration_hours": 0.8,
                        "avg_speed_kmh": 8.7,
                        "stops": raw_stops
                    }
                ]

                import json
                for r in up_routes_seed:
                    mur = MasterUPRoute(
                        route_code=r["route_code"],
                        route_name=r["route_name"],
                        region=r["region"],
                        origin=r["origin"],
                        destination=r["destination"],
                        total_distance_km=r["total_distance_km"],
                        estimated_duration_hours=r["estimated_duration_hours"],
                        avg_speed_kmh=r["avg_speed_kmh"],
                        stops_json=json.dumps(r["stops"]),
                        is_active=True
                    )
                    db.session.add(mur)
                db.session.commit()
        except Exception as e:
            print(f"[Migration Warning] Seeding master_up_routes: {e}")
            db.session.rollback()

        # Ensure UP-RT-11 (179-stops Kaushambi - Lucknow Express) exists
        try:
            from app.models import MasterUPRoute
            from pathlib import Path
            import csv, re
            rt11 = MasterUPRoute.query.filter_by(route_code="UP-RT-11").first()
            if not rt11:
                csv_path = Path("data/kaushambi_lucknow_179_stops.csv")
                if csv_path.exists():
                    stops_179 = []
                    with open(csv_path, encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            sno = int(row.get("S.No") or len(stops_179) + 1)
                            in_t = row.get("In Time") or ""
                            out_t = row.get("Out Time") or ""
                            m_in = re.search(r"(\d{2}:\d{2})(:\d{2})?\s*(AM|PM)", in_t)
                            m_out = re.search(r"(\d{2}:\d{2})(:\d{2})?\s*(AM|PM)", out_t)
                            in_fmt = f"{m_in.group(1)} {m_in.group(3)}" if m_in else in_t
                            out_fmt = f"{m_out.group(1)} {m_out.group(3)}" if m_out else out_t
                            time_win = in_fmt if in_fmt == out_fmt or not out_fmt else f"{in_fmt} - {out_fmt}"
                            
                            speed_raw = str(row.get("Speed (km/h)") or "8").strip()
                            if "-" in speed_raw:
                                p = speed_raw.split("-")
                                try: speed = round((float(p[0]) + float(p[1])) / 2.0, 1)
                                except: speed = 8.0
                            else:
                                try: speed = float(speed_raw)
                                except: speed = 8.0
                                
                            loc = (row.get("Location") or "").strip()
                            parts = [p.strip() for p in loc.split(",") if p.strip()]
                            name = parts[0] if parts else f"Stop #{sno}"
                            if len(parts) > 1 and len(name) < 15:
                                name = f"{parts[0]}, {parts[1]}"
                            is_major = any(k in loc.lower() for k in ["bus stand", "depot", "terminal", "metro", "mall", "station", "crossing", "bypass"]) or sno in [1, 179]
                            frac = (sno - 1) / 178.0
                            lat = round(28.6472 + (26.8570 - 28.6472) * frac, 6)
                            lon = round(77.3189 + (80.9320 - 77.3189) * frac, 6)
                            stops_179.append({
                                "order": sno,
                                "name": name,
                                "address": loc,
                                "speed": speed,
                                "time": time_win,
                                "lat": lat,
                                "lon": lon,
                                "major": is_major
                            })
                    if stops_179:
                        new_mur = MasterUPRoute(
                            route_code="UP-RT-11",
                            route_name="Kaushambi (Ghaziabad) — Moradabad — Bareilly — Sitapur — Qaiserbagh (Lucknow) 179-Stops Express",
                            region="Western & Central UP Super Corridor (NH-9 / NH-24 / NH-30)",
                            origin="Koshambi Bus Stand, Sahibabad, Ghaziabad",
                            destination="Bus Depot, Qaiserbagh, Lucknow",
                            total_distance_km=512.0,
                            estimated_duration_hours=11.8,
                            avg_speed_kmh=8.6,
                            stops_json=json.dumps(stops_179),
                            is_active=True
                        )
                        db.session.add(new_mur)
                        db.session.commit()
        except Exception as e:
            print(f"[Migration Warning] Auto-seeding UP-RT-11: {e}")
            db.session.rollback()



        # Backfill existing buses if registration_number or route details are missing
        try:
            from app.models import Bus, User, OperatorBusAssignment
            existing_buses = Bus.query.all()
            for b in existing_buses:
                updated = False
                if not b.registration_number:
                    b.registration_number = b.license_plate or b.bus_number
                    updated = True
                if not b.bus_name:
                    b.bus_name = f"Transit Express {b.bus_number}"
                    updated = True
                if not b.bus_type:
                    b.bus_type = "Standard Transit"
                    updated = True
                if not b.bus_status:
                    b.bus_status = "Active" if b.is_active else "Inactive"
                    updated = True
                if b.route_name and (not b.starting_point or not b.destination):
                    parts = [p.strip() for p in b.route_name.split("-") if p.strip()]
                    if len(parts) >= 2:
                        b.starting_point = parts[0]
                        b.destination = parts[1]
                    else:
                        b.starting_point = b.starting_point or "Civic Terminal"
                        b.destination = b.destination or "Harbor Point"
                    updated = True
                
                # Assign operator if available and none assigned
                if not b.assigned_operator_id:
                    operator_user = User.query.filter_by(role="operator").first()
                    if operator_user:
                        b.assigned_operator_id = operator_user.id
                        b.operator_name = operator_user.full_name or operator_user.username
                        updated = True

                if updated:
                    db.session.add(b)
            db.session.commit()

            # Ensure operator Dave is assigned to primary bus
            operator_user = User.query.filter_by(role="operator").first()
            if operator_user:
                primary_bus = Bus.query.first()
                if primary_bus:
                    if not operator_user.current_bus_id:
                        operator_user.current_bus_id = primary_bus.id
                        db.session.add(operator_user)
                    
                    assignment = OperatorBusAssignment.query.filter_by(operator_id=operator_user.id, bus_id=primary_bus.id).first()
                    if not assignment:
                        assignment = OperatorBusAssignment(
                            operator_id=operator_user.id,
                            bus_id=primary_bus.id,
                            is_primary=True,
                            assigned_at=datetime.utcnow()
                        )
                        db.session.add(assignment)
                    db.session.commit()
        except Exception as e:
            print(f"[Migration Warning] Backfilling buses: {e}")
            db.session.rollback()


def init_db(app):
    db.init_app(app)
    with app.app_context():
        run_migrations(app)
