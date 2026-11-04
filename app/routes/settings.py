import os
from datetime import datetime, timedelta
from pathlib import Path
from flask import Blueprint, request, jsonify, session
from app.config import Config
from app.database import db
from app.models import AuditLog, Journey, Bus, Upload, PassengerIdentity, PassengerObservation, PresenceEvent, ManualReview, GPSLocation, BusLocationLog
from app.services.audit_service import log_audit
from app.ai.face_pipeline import detect_and_extract_faces
from app.services.journey_service import process_upload_for_journey
from app.services.exif_service import extract_metadata, compute_file_hash

settings_bp = Blueprint("settings", __name__, url_prefix="/api/settings")

@settings_bp.route("/thresholds", methods=["GET"])
def get_thresholds():
    return jsonify({
        "SIMILARITY_HIGH_THRESHOLD": Config.SIMILARITY_HIGH_THRESHOLD,
        "SIMILARITY_MEDIUM_THRESHOLD": Config.SIMILARITY_MEDIUM_THRESHOLD,
        "SIMILARITY_LOW_THRESHOLD": Config.SIMILARITY_LOW_THRESHOLD,
        "MIN_FACE_SIZE": Config.MIN_FACE_SIZE,
        "BLUR_THRESHOLD": Config.BLUR_THRESHOLD,
        "MISSED_OBSERVATIONS_FOR_EXIT_UNCONFIRMED": Config.MISSED_OBSERVATIONS_FOR_EXIT_UNCONFIRMED,
        "MISSED_MINUTES_FOR_EXIT_UNCONFIRMED": Config.MISSED_MINUTES_FOR_EXIT_UNCONFIRMED,
        "RETENTION_DAYS": Config.RETENTION_DAYS,
        "DEMO_MODE": Config.DEMO_MODE
    })


@settings_bp.route("/thresholds", methods=["POST"])
def update_thresholds():
    data = request.get_json() or {}
    
    if "SIMILARITY_HIGH_THRESHOLD" in data:
        Config.SIMILARITY_HIGH_THRESHOLD = float(data["SIMILARITY_HIGH_THRESHOLD"])
    if "SIMILARITY_MEDIUM_THRESHOLD" in data:
        Config.SIMILARITY_MEDIUM_THRESHOLD = float(data["SIMILARITY_MEDIUM_THRESHOLD"])
    if "SIMILARITY_LOW_THRESHOLD" in data:
        Config.SIMILARITY_LOW_THRESHOLD = float(data["SIMILARITY_LOW_THRESHOLD"])
    if "BLUR_THRESHOLD" in data:
        Config.BLUR_THRESHOLD = float(data["BLUR_THRESHOLD"])
    if "MISSED_OBSERVATIONS_FOR_EXIT_UNCONFIRMED" in data:
        Config.MISSED_OBSERVATIONS_FOR_EXIT_UNCONFIRMED = int(data["MISSED_OBSERVATIONS_FOR_EXIT_UNCONFIRMED"])

    log_audit("UPDATE_SETTINGS_THRESHOLDS", details=data, user_id=session.get("user_id"))

    return jsonify({"message": "Settings updated successfully.", "thresholds": {
        "SIMILARITY_HIGH_THRESHOLD": Config.SIMILARITY_HIGH_THRESHOLD,
        "SIMILARITY_MEDIUM_THRESHOLD": Config.SIMILARITY_MEDIUM_THRESHOLD,
        "SIMILARITY_LOW_THRESHOLD": Config.SIMILARITY_LOW_THRESHOLD,
        "BLUR_THRESHOLD": Config.BLUR_THRESHOLD,
        "MISSED_OBSERVATIONS_FOR_EXIT_UNCONFIRMED": Config.MISSED_OBSERVATIONS_FOR_EXIT_UNCONFIRMED
    }})


@settings_bp.route("/mongodb-status", methods=["GET"])
def get_mongodb_status_endpoint():
    """Returns real-time connection status and collection statistics from MongoDB Atlas."""
    from app.mongodb import get_mongo_status
    status = get_mongo_status()
    return jsonify(status)


@settings_bp.route("/mongodb-config", methods=["POST"])
def update_mongodb_config():
    """Updates MongoDB Atlas URI in .env and tests connection."""
    data = request.get_json() or {}
    mongo_uri = (data.get("mongo_uri") or "").strip()
    db_name = (data.get("db_name") or "bus_tracker").strip()

    if not mongo_uri:
        return jsonify({"error": "mongo_uri is required."}), 400

    # Write to .env
    env_path = Path(Config.BASE_DIR) / ".env"
    lines = []
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    uri_found = False
    db_found = False
    new_lines = []
    for line in lines:
        if line.startswith("MONGO_URI="):
            new_lines.append(f"MONGO_URI={mongo_uri}\n")
            uri_found = True
        elif line.startswith("MONGO_DB_NAME="):
            new_lines.append(f"MONGO_DB_NAME={db_name}\n")
            db_found = True
        else:
            new_lines.append(line)

    if not uri_found:
        new_lines.append(f"\nMONGO_URI={mongo_uri}\n")
    if not db_found:
        new_lines.append(f"MONGO_DB_NAME={db_name}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    os.environ["MONGO_URI"] = mongo_uri
    os.environ["MONGO_DB_NAME"] = db_name
    Config.MONGO_URI = mongo_uri
    Config.MONGO_DB_NAME = db_name

    from app.mongodb import get_mongo_client, get_mongo_status
    client = get_mongo_client(timeout_ms=5000, force_reconnect=True)
    status = get_mongo_status()

    log_audit("UPDATE_MONGODB_CONFIG", details={"connected": status["connected"], "db_name": db_name}, user_id=session.get("user_id"))

    if status["connected"]:
        return jsonify({
            "success": True,
            "message": "Successfully connected to MongoDB Atlas!",
            "status": status
        })
    else:
        return jsonify({
            "success": False,
            "message": f"Saved configuration, but connection check failed: {status.get('error')}",
            "status": status
        }), 400


@settings_bp.route("/mongodb-sync-all", methods=["POST"])
def sync_all_to_mongodb():
    """Syncs existing database records (users, buses, camera configs, master stops) to MongoDB Atlas."""
    from app.mongodb import get_mongo_client, sync_document_to_mongo
    client = get_mongo_client(timeout_ms=5000)
    if client is None:
        return jsonify({"error": "MongoDB Atlas is not connected. Please configure MONGO_URI in .env first."}), 400

    synced_counts = {}
    from app.models import User, Bus, CameraAngleConfig, MasterRouteStop, MasterUPRoute, Journey, PassengerIdentity

    # 1. Users
    u_count = 0
    for u in User.query.all():
        if sync_document_to_mongo("users", u.id, u.to_dict()):
            u_count += 1
    synced_counts["users"] = u_count

    # 2. Buses
    b_count = 0
    for b in Bus.query.all():
        if sync_document_to_mongo("buses", b.id, b.to_dict()):
            b_count += 1
    synced_counts["buses"] = b_count

    # 3. Camera Configs
    c_count = 0
    for c in CameraAngleConfig.query.all():
        if sync_document_to_mongo("camera_angle_configs", c.id, c.to_dict()):
            c_count += 1
    synced_counts["camera_angle_configs"] = c_count

    # 4. Master UP Routes
    r_count = 0
    for r in MasterUPRoute.query.all():
        if sync_document_to_mongo("master_up_routes", r.id, r.to_dict()):
            r_count += 1
    synced_counts["master_up_routes"] = r_count

    # 5. Master Stops
    s_count = 0
    for s in MasterRouteStop.query.all():
        if sync_document_to_mongo("master_route_stops", s.id, s.to_dict()):
            s_count += 1
    synced_counts["master_route_stops"] = s_count

    log_audit("SYNC_ALL_TO_MONGODB", details=synced_counts, user_id=session.get("user_id"))

    return jsonify({
        "success": True,
        "message": f"Successfully synced documents to MongoDB Atlas!",
        "synced": synced_counts
    })


@settings_bp.route("/audit-logs", methods=["GET"])
def get_audit_logs():
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 25))
    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        "total": logs.total,
        "page": logs.page,
        "pages": logs.pages,
        "items": [log.to_dict() for log in logs.items]
    })


@settings_bp.route("/run-demo-simulation", methods=["POST"])
def run_demo_simulation():
    """
    Demo simulation is strictly disabled.
    Rule: No demo, mock, dummy, or fabricated data allowed.
    """
    return jsonify({
        "success": False,
        "error": "Demo data generation and simulation are strictly disabled. Only genuine transit data and real camera clips are permitted."
    }), 403


@settings_bp.route("/clear-demo-data", methods=["POST"])
def clear_demo_data():
    """
    Remove all demo data, demo buses, demo simulation journeys, dummy passengers,
    and associated observations, leaving only genuine user-created buses and records.
    """
    user_id = session.get("user_id")

    # 1. Find demo buses
    demo_bus_nums = ["BUS-101", "BUS-102", "DEMO-EXPRESS-101", "TEST-BUS-999"]
    demo_buses = Bus.query.filter(
        (Bus.bus_number.in_(demo_bus_nums)) | 
        (Bus.registration_number.in_(["SF-7829-AI", "SF-4190-AI", "AI-BUS-2026", "TEST-REG-999"]))
    ).all()
    demo_bus_ids = [b.id for b in demo_buses]

    # Clean up assignments and logs for these demo buses
    if demo_bus_ids:
        from app.models import OperatorBusAssignment, BusLocationLog, RouteStop, User
        OperatorBusAssignment.query.filter(OperatorBusAssignment.bus_id.in_(demo_bus_ids)).delete(synchronize_session=False)
        BusLocationLog.query.filter(BusLocationLog.bus_id.in_(demo_bus_ids)).delete(synchronize_session=False)
        RouteStop.query.filter(RouteStop.bus_id.in_(demo_bus_ids)).delete(synchronize_session=False)
        User.query.filter(User.current_bus_id.in_(demo_bus_ids)).update({"current_bus_id": None}, synchronize_session=False)

    # 2. Find demo journeys (journey_code like DEMO-% or JNY-GEARY-%)
    demo_journeys = Journey.query.filter(
        (Journey.journey_code.like("DEMO-%")) | 
        (Journey.journey_code == "JNY-GEARY-001") |
        (Journey.bus_id.in_(demo_bus_ids))
    ).all()
    demo_journey_ids = [j.id for j in demo_journeys]

    deleted_passengers = 0
    deleted_observations = 0
    deleted_uploads = 0

    if demo_journey_ids:
        deleted_passengers = PassengerIdentity.query.filter(PassengerIdentity.journey_id.in_(demo_journey_ids)).delete(synchronize_session=False)
        deleted_observations = PassengerObservation.query.filter(PassengerObservation.journey_id.in_(demo_journey_ids)).delete(synchronize_session=False)
        PresenceEvent.query.filter(PresenceEvent.journey_id.in_(demo_journey_ids)).delete(synchronize_session=False)
        GPSLocation.query.filter(GPSLocation.journey_id.in_(demo_journey_ids)).delete(synchronize_session=False)
        ManualReview.query.filter(ManualReview.journey_id.in_(demo_journey_ids)).delete(synchronize_session=False)
        deleted_uploads = Upload.query.filter(Upload.journey_id.in_(demo_journey_ids)).delete(synchronize_session=False)
        Journey.query.filter(Journey.id.in_(demo_journey_ids)).delete(synchronize_session=False)

    # Also clean any unlinked or dummy passenger identities (P001, P002, P003, P004)
    dummy_p = PassengerIdentity.query.filter(PassengerIdentity.person_id.in_(["P001", "P002", "P003", "P004"])).delete(synchronize_session=False)
    deleted_passengers += dummy_p

    # Delete the demo buses
    deleted_buses_count = len(demo_buses)
    for b in demo_buses:
        db.session.delete(b)

    # If there is a real bus left (e.g. UP14AW0307 / Gaurav Transport), make sure users are linked to it
    from app.models import User
    real_bus = Bus.query.filter(Bus.registration_number == "UP14AW0307").first()
    if not real_bus:
        real_bus = Bus.query.first()

    if real_bus:
        User.query.filter(User.current_bus_id.is_(None)).update({"current_bus_id": real_bus.id}, synchronize_session=False)
        # Ensure there is an active journey for the real bus if none exists
        active_j = Journey.query.filter_by(bus_id=real_bus.id, status="active").first()
        if not active_j:
            new_j = Journey(
                journey_code=f"JNY-{real_bus.registration_number.replace('-', '')}-001",
                bus_id=real_bus.id,
                origin=real_bus.starting_point or "Koshambi Bus Stand",
                destination=real_bus.destination or "Sector-1 Vaishali Terminal",
                status="active",
                start_time=datetime.utcnow(),
                created_by_id=user_id
            )
            db.session.add(new_j)

    db.session.commit()

    log_audit("CLEAR_DEMO_DATA", details={
        "deleted_buses": deleted_buses_count,
        "deleted_journeys": len(demo_journey_ids),
        "deleted_passengers": deleted_passengers
    }, user_id=user_id)

    return jsonify({
        "success": True,
        "message": f"Demo data removed successfully! Deleted {deleted_buses_count} demo buses, {len(demo_journey_ids)} demo journeys, and {deleted_passengers} dummy passenger records.",
        "deleted_buses": deleted_buses_count,
        "deleted_journeys": len(demo_journey_ids),
        "deleted_passengers": deleted_passengers
    })


@settings_bp.route("/purge-all-data", methods=["POST", "DELETE"])
def purge_all_passenger_data():
    """
    Completely purges all passenger identities, observations, presence events, merge history,
    local crop files, smartbus-data exports, and remote server images.
    Leaves system in a completely clean, lightweight, running state.
    """
    from flask import has_request_context
    user_id = session.get("user_id") if has_request_context() else None
    import os
    import shutil
    import json
    from app.models import PassengerMergeRecord, CloudSyncLog
    from app.services.cloud_sync_service import fetch_remote_images, delete_remote_images_bulk

    # 1. Counts before deletion
    p_count = PassengerIdentity.query.count()
    obs_count = PassengerObservation.query.count()
    merge_count = PassengerMergeRecord.query.count()

    # 2. Database purge
    PassengerMergeRecord.query.delete(synchronize_session=False)
    ManualReview.query.delete(synchronize_session=False)
    PresenceEvent.query.delete(synchronize_session=False)
    GPSLocation.query.delete(synchronize_session=False)
    PassengerObservation.query.delete(synchronize_session=False)
    PassengerIdentity.query.delete(synchronize_session=False)
    Upload.query.delete(synchronize_session=False)
    CloudSyncLog.query.delete(synchronize_session=False)

    # 3. Purge local crops directory
    crops_dir = Path(Config.DATA_DIR) / "crops"
    deleted_local_files = 0
    if crops_dir.exists():
        for f in crops_dir.glob("*.*"):
            try:
                f.unlink()
                deleted_local_files += 1
            except Exception:
                pass

    # 4. Purge smartbus-data/journeys/
    base_dir = getattr(Config, "SMARTBUS_DATA_DIR", Path(Config.BASE_DIR) / "smartbus-data")
    jny_dir = base_dir / "journeys"
    if jny_dir.exists():
        for d in jny_dir.iterdir():
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)

    manifest_p = base_dir / "manifest.json"
    with open(manifest_p, "w", encoding="utf-8") as mf:
        json.dump({
            "last_synced_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "published_journeys": []
        }, mf, indent=2)

    checksum_p = base_dir / "image_checksums.json"
    with open(checksum_p, "w", encoding="utf-8") as cf:
        json.dump({}, cf, indent=2)

    # 5. Remote image server purge
    remote_deleted = 0
    try:
        remote_data = fetch_remote_images(limit=1000, offset=0)
        items = remote_data.get("images", []) if isinstance(remote_data, dict) else []
        filenames = [item.get("filename") for item in items if item.get("filename")]
        if filenames:
            del_res = delete_remote_images_bulk(filenames, force=True, user_id=user_id)
            remote_deleted = del_res.get("deleted_count", 0)
    except Exception as e:
        print(f"[Remote Purge Warning] {e}")

    db.session.commit()

    log_audit("PURGE_ALL_PASSENGER_DATA", details={
        "deleted_passengers": p_count,
        "deleted_observations": obs_count,
        "deleted_merges": merge_count,
        "deleted_local_files": deleted_local_files,
        "remote_deleted": remote_deleted
    }, user_id=user_id)

    return jsonify({
        "success": True,
        "message": f"Successfully purged all passenger data! Cleared {p_count} passengers, {obs_count} observations, {merge_count} merge logs, {deleted_local_files} local crop files, and {remote_deleted} remote images.",
        "deleted_passengers": p_count,
        "deleted_observations": obs_count,
        "deleted_merges": merge_count,
        "deleted_local_files": deleted_local_files,
        "remote_deleted": remote_deleted
    })


def _get_dir_size_bytes(directory_path):
    """Calculates recursive directory size in bytes."""
    total = 0
    p = Path(directory_path)
    if p.exists():
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except Exception:
                    pass
    return total


@settings_bp.route("/data-analysis", methods=["GET"])
def get_data_analysis():
    """
    Returns comprehensive storage and historical data breakdown for admin analysis.
    """
    now = datetime.utcnow()

    # Disk storage measurements
    uploads_bytes = _get_dir_size_bytes(Config.UPLOAD_FOLDER)
    crops_bytes = _get_dir_size_bytes(Config.CROP_FOLDER)
    db_file = Path(Config.BASE_DIR) / "data" / "bus_tracker.db"
    db_bytes = db_file.stat().st_size if db_file.exists() else 0
    total_bytes = uploads_bytes + crops_bytes + db_bytes

    # Record counts
    total_journeys = Journey.query.count()
    active_journeys = Journey.query.filter_by(status="active").count()
    completed_journeys = Journey.query.filter_by(status="completed").count()

    journeys_7d = Journey.query.filter(Journey.created_at < now - timedelta(days=7)).count()
    journeys_15d = Journey.query.filter(Journey.created_at < now - timedelta(days=15)).count()
    journeys_30d = Journey.query.filter(Journey.created_at < now - timedelta(days=30)).count()

    total_uploads = Upload.query.count()
    total_passengers = PassengerIdentity.query.count()
    total_observations = PassengerObservation.query.count()
    total_presence_events = PresenceEvent.query.count()
    total_audit_logs = AuditLog.query.count()
    total_gps = GPSLocation.query.count()

    # Journey list breakdown with estimated size
    journeys = Journey.query.order_by(Journey.created_at.desc()).limit(50).all()
    journey_list = []
    for j in journeys:
        # Calculate size of photos in this journey
        j_uploads = Upload.query.filter_by(journey_id=j.id).all()
        j_size = sum(u.file_size or 0 for u in j_uploads)
        age_days = (now - j.created_at).days if j.created_at else 0
        journey_list.append({
            "id": j.id,
            "journey_code": j.journey_code,
            "bus_number": j.bus.registration_number or j.bus.bus_number if j.bus else "Unassigned",
            "bus_name": j.bus.bus_name if j.bus else "Bus",
            "route": f"{j.origin} -> {j.destination}",
            "status": j.status,
            "start_time": j.start_time.strftime("%Y-%m-%d %H:%M") if j.start_time else "-",
            "end_time": j.end_time.strftime("%Y-%m-%d %H:%M") if j.end_time else "-",
            "photo_count": len(j_uploads),
            "uploads_count": len(j_uploads),
            "passenger_count": len(j.passengers) if j.passengers else 0,
            "passengers_count": len(j.passengers) if j.passengers else 0,
            "size_mb": round(j_size / (1024 * 1024), 2),
            "age_days": age_days,
            "is_active": (j.status == "active")
        })

    return jsonify({
        "storage": {
            "uploads_mb": round(uploads_bytes / (1024 * 1024), 2),
            "crops_mb": round(crops_bytes / (1024 * 1024), 2),
            "db_mb": round(db_bytes / (1024 * 1024), 2),
            "total_mb": round(total_bytes / (1024 * 1024), 2),
            "uploads_dir_mb": round(uploads_bytes / (1024 * 1024), 2),
            "crops_dir_mb": round(crops_bytes / (1024 * 1024), 2),
            "database_file_mb": round(db_bytes / (1024 * 1024), 2),
            "total_storage_mb": round(total_bytes / (1024 * 1024), 2),
            "uploads_file_count": total_uploads,
            "crops_file_count": total_observations
        },
        "counts": {
            "total_journeys": total_journeys,
            "active_journeys": active_journeys,
            "completed_journeys": completed_journeys,
            "journeys_older_7d": journeys_7d,
            "journeys_older_15d": journeys_15d,
            "journeys_older_30d": journeys_30d,
            "total_uploads": total_uploads,
            "total_passengers": total_passengers,
            "total_observations": total_observations,
            "total_presence_events": total_presence_events,
            "total_audit_logs": total_audit_logs,
            "total_gps": total_gps
        },
        "journeys": journey_list
    })


@settings_bp.route("/data-cleanup", methods=["POST"])
def cleanup_historical_data():
    """
    Safely delete historical journey data and reclaim physical storage:
    Body:
    {
        "cleanup_type": "older_than_days" | "all_completed" | "single_journey",
        "days": 7 | 14 | 30 | 60,
        "journey_id": 12,
        "delete_disk_files": true,
        "delete_audit_logs": false
    }
    """
    user_id = session.get("user_id")
    data = request.get_json() or {}

    cleanup_type = data.get("cleanup_type", "older_than_days")
    days = int(data.get("days", 30))
    target_journey_id = data.get("journey_id")
    delete_disk_files = data.get("delete_disk_files", True)
    delete_audit_logs = data.get("delete_audit_logs", False)

    now = datetime.utcnow()
    query = Journey.query

    # Target selection
    if cleanup_type == "single_journey" and target_journey_id:
        try:
            j_id_int = int(target_journey_id)
            query = query.filter_by(id=j_id_int)
        except (ValueError, TypeError):
            query = query.filter_by(id=target_journey_id)
    elif cleanup_type in ["all_data", "all_history"]:
        query = query # matches all journeys
    elif cleanup_type == "all_completed":
        query = query.filter_by(status="completed")
    else: # older_than_days
        cutoff = now - timedelta(days=days)
        query = query.filter(Journey.created_at < cutoff)

    target_journeys = query.all()
    if not target_journeys:
        return jsonify({
            "success": True,
            "message": "No historical data matched the selected cleanup criteria.",
            "deleted_journeys": 0,
            "deleted_uploads": 0,
            "deleted_passengers": 0,
            "freed_mb": 0.0
        })

    deleted_journeys_count = len(target_journeys)
    deleted_uploads_count = 0
    deleted_passengers_count = 0
    deleted_files_count = 0
    freed_bytes = 0

    journey_ids = [j.id for j in target_journeys]

    # 1. Process files on disk for uploads & crops
    for j_id in journey_ids:
        uploads = Upload.query.filter_by(journey_id=j_id).all()
        for u in uploads:
            deleted_uploads_count += 1
            if delete_disk_files:
                for file_field in [u.file_path, u.annotated_path]:
                    if file_field:
                        p = Path(file_field)
                        if not p.is_absolute():
                            p = Config.BASE_DIR / file_field
                        if p.exists() and p.is_file():
                            try:
                                freed_bytes += p.stat().st_size
                                p.unlink()
                                deleted_files_count += 1
                            except Exception:
                                pass

        # Delete observation crops
        observations = PassengerObservation.query.filter_by(journey_id=j_id).all()
        for obs in observations:
            if delete_disk_files and obs.crop_path:
                p = Path(obs.crop_path)
                if not p.is_absolute():
                    p = Config.BASE_DIR / obs.crop_path
                if p.exists() and p.is_file():
                    try:
                        freed_bytes += p.stat().st_size
                        p.unlink()
                        deleted_files_count += 1
                    except Exception:
                        pass

        # Count passengers and collect remote filenames
        passengers = PassengerIdentity.query.filter_by(journey_id=j_id).all()
        deleted_passengers_count += len(passengers)
        remote_fns = [f"{p.person_id}_face.jpg" for p in passengers if p.person_id]
        if remote_fns:
            try:
                from app.services.cloud_sync_service import delete_remote_images_bulk
                delete_remote_images_bulk(remote_fns, force=True)
            except Exception:
                pass

        # Clean smartbus-data folder
        try:
            base_dir = getattr(Config, "SMARTBUS_DATA_DIR", Path(Config.BASE_DIR) / "smartbus-data")
            j_folder = base_dir / "journeys" / f"journey_{j_id:03d}"
            if j_folder.exists():
                shutil.rmtree(j_folder, ignore_errors=True)
            manifest_p = base_dir / "manifest.json"
            if manifest_p.is_file():
                with open(manifest_p, "r", encoding="utf-8") as mf:
                    m_data = json.load(mf)
                m_data["published_journeys"] = [pj for pj in m_data.get("published_journeys", []) if pj.get("journey_id") != j_id]
                with open(manifest_p, "w", encoding="utf-8") as mf:
                    json.dump(m_data, mf, indent=2)
        except Exception:
            pass

        # 2. Database cascade cleanup
        from app.models import PassengerMergeRecord
        PassengerMergeRecord.query.filter_by(journey_id=j_id).delete(synchronize_session=False)
        BusLocationLog.query.filter_by(journey_id=j_id).delete(synchronize_session=False)
        ManualReview.query.filter_by(journey_id=j_id).delete(synchronize_session=False)
        PresenceEvent.query.filter_by(journey_id=j_id).delete(synchronize_session=False)
        GPSLocation.query.filter_by(journey_id=j_id).delete(synchronize_session=False)
        PassengerObservation.query.filter_by(journey_id=j_id).delete(synchronize_session=False)
        PassengerIdentity.query.filter_by(journey_id=j_id).delete(synchronize_session=False)
        Upload.query.filter_by(journey_id=j_id).delete(synchronize_session=False)
        Journey.query.filter_by(id=j_id).delete(synchronize_session=False)

    # 3. Optional Audit Log Pruning
    deleted_audit_logs_count = 0
    if delete_audit_logs and days > 0:
        audit_cutoff = now - timedelta(days=days)
        deleted_audit_logs_count = AuditLog.query.filter(AuditLog.created_at < audit_cutoff).delete(synchronize_session=False)

    db.session.commit()

    db.session.commit()

    freed_mb = round(freed_bytes / (1024 * 1024), 2)

    # Log security audit trail
    log_audit("CLEANUP_HISTORICAL_DATA", details={
        "cleanup_type": cleanup_type,
        "criteria_days": days,
        "deleted_journeys": deleted_journeys_count,
        "deleted_uploads": deleted_uploads_count,
        "deleted_passengers": deleted_passengers_count,
        "deleted_files": deleted_files_count,
        "deleted_audit_logs": deleted_audit_logs_count,
        "freed_mb": freed_mb
    }, user_id=user_id)

    return jsonify({
        "success": True,
        "message": f"Successfully deleted {deleted_journeys_count} historical journeys ({deleted_uploads_count} photos, {deleted_passengers_count} passenger profiles) and freed {freed_mb} MB.",
        "deleted_journeys": deleted_journeys_count,
        "deleted_uploads": deleted_uploads_count,
        "deleted_passengers": deleted_passengers_count,
        "deleted_files": deleted_files_count,
        "deleted_audit_logs": deleted_audit_logs_count,
        "freed_mb": freed_mb
    })

