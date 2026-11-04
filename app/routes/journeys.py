import os
import shutil
import json
from pathlib import Path
from datetime import datetime
from flask import Blueprint, request, jsonify, session
from app.config import Config
from app.database import db
from app.models import Journey, Bus, PassengerIdentity, Upload, PassengerObservation, PresenceEvent, GPSLocation, ManualReview
from app.services.audit_service import log_audit

journeys_bp = Blueprint("journeys", __name__, url_prefix="/api/journeys")

@journeys_bp.route("", methods=["GET"])
def get_journeys():
    status = request.args.get("status")
    query = Journey.query
    if status:
        query = query.filter_by(status=status)
    journeys = query.order_by(Journey.created_at.desc()).all()
    return jsonify([j.to_dict() for j in journeys])


@journeys_bp.route("/<int:journey_id>", methods=["GET"])
def get_journey(journey_id):
    journey = Journey.query.get_or_404(journey_id)
    
    # Calculate stats
    passengers = PassengerIdentity.query.filter_by(journey_id=journey_id).all()
    present_count = sum(1 for p in passengers if p.status == "PRESENT")
    possibly_absent_count = sum(1 for p in passengers if p.status in ("POSSIBLY_ABSENT", "EXIT_UNCONFIRMED"))
    exit_confirmed_count = sum(1 for p in passengers if p.status == "EXIT_CONFIRMED")
    
    data = journey.to_dict()
    data.update({
        "stats": {
            "total_passengers": len(passengers),
            "present": present_count,
            "possibly_absent": possibly_absent_count,
            "exit_confirmed": exit_confirmed_count,
            "total_uploads": Upload.query.filter_by(journey_id=journey_id).count()
        }
    })
    return jsonify(data)


@journeys_bp.route("", methods=["POST"])
def create_journey():
    data = request.get_json() or {}
    bus_id = data.get("bus_id")
    bus_number = data.get("bus_number")
    origin = (data.get("origin") or "").strip()
    destination = (data.get("destination") or "").strip()

    # Auto find or lookup bus
    bus = None
    if bus_id:
        bus = db.session.get(Bus, int(bus_id))
    elif bus_number:
        bus = Bus.query.filter_by(bus_number=bus_number).first()

    if not bus:
        if not bus_number:
            bus_number = "BUS-101"
        bus = Bus.query.filter_by(bus_number=bus_number).first()
        if not bus:
            bus = Bus(
                bus_number=bus_number,
                route_name=f"{origin or 'Origin'} - {destination or 'Destination'}",
                starting_point=origin or "Main Terminal",
                destination=destination or "Central Station"
            )
            db.session.add(bus)
            db.session.flush()

    bus_id = bus.id
    if not origin:
        origin = bus.starting_point or (bus.route_name.split("-")[0].strip() if bus.route_name and "-" in bus.route_name else "Main Terminal")
    if not destination:
        destination = bus.destination or (bus.route_name.split("-")[-1].strip() if bus.route_name and "-" in bus.route_name else "City Center")

    # Auto generate journey code
    now = datetime.utcnow()
    count_today = Journey.query.count() + 1
    code = f"JNY-{now.strftime('%Y%m%d')}-{count_today:03d}"

    journey = Journey(
        journey_code=code,
        bus_id=bus_id,
        origin=origin,
        destination=destination,
        status="active",
        start_time=now,
        created_by_id=session.get("user_id")
    )
    db.session.add(journey)
    db.session.commit()

    try:
        from app.mongodb import sync_document_to_mongo
        sync_document_to_mongo("journeys", journey.id, journey.to_dict())
    except Exception:
        pass

    log_audit("CREATE_JOURNEY", entity_type="Journey", entity_id=journey.id, details={"code": code, "origin": origin, "destination": destination}, user_id=session.get("user_id"))

    return jsonify({"message": "Journey created successfully", "journey": journey.to_dict()}), 201


@journeys_bp.route("/<int:journey_id>/end", methods=["POST"])
def end_journey(journey_id):
    journey = Journey.query.get_or_404(journey_id)
    journey.status = "completed"
    journey.end_time = datetime.utcnow()
    db.session.commit()

    try:
        from app.mongodb import sync_document_to_mongo
        sync_document_to_mongo("journeys", journey.id, journey.to_dict())
    except Exception:
        pass

    log_audit("END_JOURNEY", entity_type="Journey", entity_id=journey.id, user_id=session.get("user_id"))
    return jsonify({"message": "Journey ended successfully", "journey": journey.to_dict()})


@journeys_bp.route("/buses", methods=["GET"])
def get_buses():
    buses = Bus.query.filter_by(is_active=True).all()
    return jsonify([b.to_dict() for b in buses])


@journeys_bp.route("/<int:journey_id>", methods=["DELETE"])
def delete_journey(journey_id):
    """
    Permanently delete a journey, all associated passenger attendance records,
    GPS locations, observations, and physical photo crops from disk.
    """
    user_id = session.get("user_id")
    journey = Journey.query.get_or_404(journey_id)
    journey_code = journey.journey_code

    freed_bytes = 0
    deleted_files = 0

    # 1. Purge physical files on disk
    uploads = Upload.query.filter_by(journey_id=journey.id).all()
    for u in uploads:
        for file_field in [u.file_path, u.annotated_path]:
            if file_field:
                p = Path(file_field)
                if not p.is_absolute():
                    p = Config.BASE_DIR / file_field
                if p.exists() and p.is_file():
                    try:
                        freed_bytes += p.stat().st_size
                        p.unlink()
                        deleted_files += 1
                    except Exception:
                        pass

    observations = PassengerObservation.query.filter_by(journey_id=journey.id).all()
    for obs in observations:
        if obs.crop_path:
            p = Path(obs.crop_path)
            if not p.is_absolute():
                p = Config.BASE_DIR / obs.crop_path
            if p.exists() and p.is_file():
                try:
                    freed_bytes += p.stat().st_size
                    p.unlink()
                    deleted_files += 1
                except Exception:
                    pass

    # 2. Purge database records & collect remote image filenames
    passengers = PassengerIdentity.query.filter_by(journey_id=journey.id).all()
    p_ids = [p.id for p in passengers]
    remote_filenames_to_delete = []
    for p in passengers:
        if p.person_id:
            remote_filenames_to_delete.append(f"{p.person_id}_face.jpg")
        if p.thumbnail_path and "http" in p.thumbnail_path:
            remote_filenames_to_delete.append(os.path.basename(p.thumbnail_path))

    # Delete remote images from server
    if remote_filenames_to_delete:
        try:
            from app.services.cloud_sync_service import delete_remote_images_bulk
            delete_remote_images_bulk(list(set(remote_filenames_to_delete)), force=True)
        except Exception:
            pass

    # Purge smartbus-data folder for this journey
    try:
        base_dir = getattr(Config, "SMARTBUS_DATA_DIR", Path(Config.BASE_DIR) / "smartbus-data")
        jny_folder = base_dir / "journeys" / f"journey_{journey.id:03d}"
        if jny_folder.exists() and jny_folder.is_dir():
            shutil.rmtree(jny_folder, ignore_errors=True)

        # Update manifest.json
        manifest_p = base_dir / "manifest.json"
        if manifest_p.is_file():
            with open(manifest_p, "r", encoding="utf-8") as mf:
                m_data = json.load(mf)
            m_data["published_journeys"] = [pj for pj in m_data.get("published_journeys", []) if pj.get("journey_id") != journey.id]
            with open(manifest_p, "w", encoding="utf-8") as mf:
                json.dump(m_data, mf, indent=2)
    except Exception:
        pass

    if p_ids:
        ManualReview.query.filter(ManualReview.passenger_identity_id.in_(p_ids)).delete(synchronize_session=False)

    from app.models import PassengerMergeRecord
    PassengerMergeRecord.query.filter_by(journey_id=journey.id).delete(synchronize_session=False)
    PresenceEvent.query.filter_by(journey_id=journey.id).delete(synchronize_session=False)
    GPSLocation.query.filter_by(journey_id=journey.id).delete(synchronize_session=False)
    PassengerObservation.query.filter_by(journey_id=journey.id).delete(synchronize_session=False)
    PassengerIdentity.query.filter_by(journey_id=journey.id).delete(synchronize_session=False)
    Upload.query.filter_by(journey_id=journey.id).delete(synchronize_session=False)

    db.session.delete(journey)
    db.session.commit()

    freed_mb = round(freed_bytes / (1024 * 1024), 2)

    log_audit("DELETE_JOURNEY", entity_type="Journey", entity_id=journey_id, details={
        "journey_code": journey_code,
        "deleted_files": deleted_files,
        "freed_mb": freed_mb
    }, user_id=user_id)

    return jsonify({
        "success": True,
        "message": f"Journey {journey_code} and all associated photos, crops, and passenger history were deleted successfully.",
        "deleted_files": deleted_files,
        "freed_mb": freed_mb
    })

