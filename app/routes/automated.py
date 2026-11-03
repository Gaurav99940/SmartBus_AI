import os
import json
import uuid
from pathlib import Path
from datetime import datetime
from flask import Blueprint, request, jsonify, session, Response, current_app
from werkzeug.utils import secure_filename

from app.config import Config
from app.database import db
from app.models import (
    Journey, SourceFolder, SourceMediaFile, TimetableImport,
    TimetableStop, StopMediaMatch, StopProcessingJob, Upload,
    PassengerIdentity, PassengerObservation, PresenceEvent, Bus
)
from app.services.media_service import scan_directory, extract_media_metadata
from app.services.timetable_service import parse_timetable_file
from app.services.batch_processor import (
    match_stops_to_media, process_single_stop, start_automated_job,
    pause_automated_job, resume_automated_job, cancel_automated_job
)
from app.services.export_service import (
    generate_stop_processing_csv, generate_stop_processing_excel,
    generate_processing_manifest_csv, generate_passenger_attendance_pdf,
    generate_journey_summary_pdf
)
from app.services.audit_service import log_audit

automated_bp = Blueprint("automated", __name__, url_prefix="/api/automated")


# ============================================================================
# 1. LOCAL FOLDER SELECTION & SCANNING
# ============================================================================

@automated_bp.route("/pick-native-folder", methods=["POST"])
def pick_native_folder():
    """
    Spawns a native Windows folder picker dialog on the local machine
    if Tkinter is supported, and returns the selected path.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected_path = filedialog.askdirectory(title="Select Camera Angle Clip Folder")
        root.destroy()
        if selected_path:
            norm_path = os.path.normpath(selected_path)
            return jsonify({"success": True, "folder_path": norm_path})
        return jsonify({"success": False, "cancelled": True})
    except Exception as e:
        return jsonify({"success": False, "error": f"Native picker error: {str(e)}"}), 500


@automated_bp.route("/scan-folder", methods=["POST"])
def scan_folder_endpoint():
    """
    Scans a local directory for Angle A, B, or C clips and images,
    creates/updates SourceFolder and SourceMediaFile DB records.
    """
    data = request.get_json() or {}
    folder_path = (data.get("folder_path") or "").strip()
    camera_angle = (data.get("camera_angle") or "A").upper()
    journey_id_raw = data.get("journey_id")
    journey_id = None
    if journey_id_raw and str(journey_id_raw).lower() not in ("auto_create", "null", "undefined", ""):
        try:
            journey_id = int(journey_id_raw)
        except Exception:
            journey_id = None

    if not journey_id:
        active_j = Journey.query.filter_by(status="in_progress").first() or Journey.query.order_by(Journey.id.desc()).first()
        if active_j:
            journey_id = active_j.id

    if not folder_path:
        return jsonify({"success": False, "error": "folder_path is required"}), 400

    # Path traversal protection: normalize and verify existence
    norm_path = os.path.normpath(folder_path)
    p = Path(norm_path)
    if not p.exists() or not p.is_dir():
        return jsonify({"success": False, "error": f"Folder does not exist or is not a directory: {norm_path}"}), 400

    # Determine base_date from timetable if available for time-only filenames
    base_date = None
    timetable_id_raw = data.get("timetable_id")
    if timetable_id_raw:
        try:
            t_obj = TimetableImport.query.get(int(timetable_id_raw))
            if t_obj and t_obj.service_date:
                base_date = t_obj.service_date
        except Exception:
            pass
    if not base_date:
        latest_tt = TimetableImport.query.order_by(TimetableImport.id.desc()).first()
        if latest_tt and latest_tt.service_date:
            base_date = latest_tt.service_date

    scan_res = scan_directory(norm_path, base_date=base_date)
    if not scan_res["success"]:
        return jsonify(scan_res), 400

    # Create or update SourceFolder in DB
    s_folder = SourceFolder.query.filter_by(
        folder_path=scan_res["folder_path"],
        camera_angle=camera_angle
    ).first()

    if not s_folder:
        s_folder = SourceFolder(
            journey_id=journey_id,
            camera_angle=camera_angle,
            folder_path=scan_res["folder_path"],
            folder_name=scan_res["folder_name"],
            clips_count=scan_res["clips_count"],
            images_count=scan_res["images_count"],
            status="ready",
            last_scanned_at=datetime.utcnow()
        )
        db.session.add(s_folder)
        db.session.flush()
    else:
        s_folder.clips_count = scan_res["clips_count"]
        s_folder.images_count = scan_res["images_count"]
        s_folder.last_scanned_at = datetime.utcnow()
        s_folder.status = "ready"
        if journey_id:
            s_folder.journey_id = journey_id

    # Sync discovered media files
    existing_files = {mf.file_path: mf for mf in s_folder.media_files}
    for file_info in scan_res["files"]:
        f_path = file_info["file_path"]
        if f_path in existing_files:
            mf = existing_files[f_path]
            mf.start_time = file_info["start_time"]
            mf.end_time = file_info["end_time"]
            mf.duration_seconds = file_info["duration_seconds"]
            mf.fps = file_info["fps"]
            mf.total_frames = file_info["total_frames"]
            mf.file_size = file_info["file_size"]
            mf.is_timestamp_reliable = file_info["is_timestamp_reliable"]
            mf.timestamp_source = file_info["timestamp_source"]
        else:
            mf = SourceMediaFile(
                source_folder_id=s_folder.id,
                journey_id=journey_id,
                camera_angle=camera_angle,
                file_path=f_path,
                filename=file_info["filename"],
                file_ext=file_info["file_ext"],
                media_type=file_info["media_type"],
                file_size=file_info["file_size"],
                file_hash=file_info["file_hash"],
                start_time=file_info["start_time"],
                end_time=file_info["end_time"],
                duration_seconds=file_info["duration_seconds"],
                fps=file_info["fps"],
                total_frames=file_info["total_frames"],
                width=file_info["width"],
                height=file_info["height"],
                timestamp_source=file_info["timestamp_source"],
                is_timestamp_reliable=file_info["is_timestamp_reliable"],
                status="ready"
            )
            db.session.add(mf)

    db.session.commit()

    return jsonify({
        "success": True,
        "folder": s_folder.to_dict(),
        "files_count": len(scan_res["files"]),
        "clips_count": scan_res["clips_count"],
        "images_count": scan_res["images_count"],
        "files": [f for f in scan_res["files"][:50]]  # Preview first 50 files
    })


@automated_bp.route("/folder-manifest/<int:folder_id>", methods=["GET"])
def get_folder_manifest(folder_id):
    """Returns the full list of discovered files in a source folder."""
    folder = SourceFolder.query.get(folder_id)
    if not folder:
        return jsonify({"success": False, "error": "Source folder not found"}), 404
    files = [mf.to_dict() for mf in folder.media_files]
    return jsonify({"success": True, "folder": folder.to_dict(), "files": files})


# ============================================================================
# 2. TIMETABLE IMPORT & VALIDATION
# ============================================================================

@automated_bp.route("/import-timetable", methods=["POST"])
def import_timetable():
    """
    Uploads and parses an Excel (.xlsx, .xls) or CSV timetable file.
    """
    if "timetable_file" not in request.files:
        return jsonify({"success": False, "error": "No timetable file uploaded"}), 400

    f = request.files["timetable_file"]
    if not f or not f.filename:
        return jsonify({"success": False, "error": "Empty filename"}), 400

    journey_id_raw = request.form.get("journey_id")
    journey_id = None
    if journey_id_raw and str(journey_id_raw).lower() not in ("auto_create", "null", "undefined", ""):
        try:
            journey_id = int(journey_id_raw)
        except Exception:
            journey_id = None

    if not journey_id:
        active_j = Journey.query.filter_by(status="in_progress").first() or Journey.query.order_by(Journey.id.desc()).first()
        if active_j:
            journey_id = active_j.id

    service_date = request.form.get("service_date")
    timezone = request.form.get("timezone", "UTC")
    tolerance_minutes = int(request.form.get("tolerance_minutes", 3))
    time_window_mode = request.form.get("time_window_mode", "ARRIVAL_DEPARTURE")
    col_mapping_str = request.form.get("column_mapping")
    custom_col_map = json.loads(col_mapping_str) if col_mapping_str else None

    clean_name = secure_filename(f.filename) or "timetable.xlsx"
    upload_dir = Config.UPLOAD_FOLDER / "timetables"
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_path = upload_dir / f"timetable_{int(datetime.utcnow().timestamp())}_{clean_name}"
    f.save(str(saved_path))

    parse_result = parse_timetable_file(
        str(saved_path),
        column_mapping=custom_col_map,
        service_date=service_date,
        timezone=timezone,
        tolerance_minutes=tolerance_minutes,
        time_window_mode=time_window_mode
    )

    if not parse_result["success"]:
        return jsonify(parse_result), 400

    # Save TimetableImport record
    s_date_obj = None
    if parse_result.get("service_date"):
        try:
            s_date_obj = datetime.strptime(parse_result["service_date"], "%Y-%m-%d").date()
        except Exception:
            pass

    tt_import = TimetableImport(
        journey_id=journey_id,
        filename=clean_name,
        file_path=str(saved_path),
        file_size=os.path.getsize(saved_path),
        service_date=s_date_obj,
        timezone=timezone,
        tolerance_minutes=tolerance_minutes,
        time_window_mode=time_window_mode,
        column_mapping_json=json.dumps(parse_result["column_mapping"]),
        total_stops=parse_result["total_stops"],
        status="valid" if not parse_result.get("validation_errors") else "invalid",
        validation_errors_json=json.dumps(parse_result.get("validation_errors", []))
    )
    db.session.add(tt_import)
    db.session.flush()

    # Save TimetableStop records
    for s_info in parse_result["stops"]:
        stop_rec = TimetableStop(
            timetable_id=tt_import.id,
            stop_number=s_info["stop_number"],
            stop_name=s_info["stop_name"],
            scheduled_arrival=s_info["scheduled_arrival"],
            scheduled_departure=s_info["scheduled_departure"],
            actual_arrival=s_info["actual_arrival"],
            actual_departure=s_info["actual_departure"],
            window_start=s_info["window_start"],
            window_end=s_info["window_end"],
            coverage_status="NOT_COVERED",
            processing_status="pending"
        )
        db.session.add(stop_rec)

    db.session.commit()

    return jsonify({
        "success": True,
        "timetable_id": tt_import.id,
        "filename": clean_name,
        "total_stops": tt_import.total_stops,
        "columns": parse_result["columns"],
        "column_mapping": parse_result["column_mapping"],
        "service_date": parse_result["service_date"],
        "tolerance_minutes": tolerance_minutes,
        "time_window_mode": time_window_mode,
        "stops": [s.to_dict() for s in tt_import.stops]
    })


@automated_bp.route("/timetable/<int:timetable_id>/preview", methods=["GET"])
def preview_timetable(timetable_id):
    """Returns preview of timetable stops and column mapping."""
    tt = TimetableImport.query.get(timetable_id)
    if not tt:
        return jsonify({"success": False, "error": "Timetable not found"}), 404
    return jsonify({
        "success": True,
        "timetable": tt.to_dict(),
        "stops": [s.to_dict() for s in tt.stops]
    })


# ============================================================================
# 3. AUTOMATIC STOP-WISE CLIP MATCHING
# ============================================================================

@automated_bp.route("/match-clips", methods=["POST"])
def match_clips_endpoint():
    """
    Runs automated stop-wise clip matching across Angles A, B, C.
    """
    data = request.get_json() or {}
    timetable_id = data.get("timetable_id")
    if not timetable_id:
        return jsonify({"success": False, "error": "timetable_id is required"}), 400

    success, msg = match_stops_to_media(timetable_id)
    if not success:
        return jsonify({"success": False, "error": msg}), 400

    tt = TimetableImport.query.get(timetable_id)
    stops = [s.to_dict() for s in tt.stops]

    return jsonify({
        "success": True,
        "message": msg,
        "timetable_id": timetable_id,
        "stops": stops
    })


@automated_bp.route("/stops/<int:stop_id>/manual-match", methods=["POST"])
def manual_override_match(stop_id):
    """
    Allows the operator to manually override/select a clip for a specific stop & angle.
    Records manual change in audit log.
    """
    data = request.get_json() or {}
    camera_angle = (data.get("camera_angle") or "A").upper()
    media_file_id = data.get("media_file_id")
    comment = data.get("comment", "Operator manual clip override")

    stop = TimetableStop.query.get(stop_id)
    if not stop:
        return jsonify({"success": False, "error": "Stop not found"}), 404

    mf = SourceMediaFile.query.get(media_file_id) if media_file_id else None

    match = StopMediaMatch.query.filter_by(stop_id=stop.id, camera_angle=camera_angle).first()
    if not match:
        match = StopMediaMatch(stop_id=stop.id, camera_angle=camera_angle)
        db.session.add(match)

    if mf:
        match.source_media_file_id = mf.id
        match.source_filename = mf.filename
        match.source_folder = mf.file_path
        match.media_type = mf.media_type
        match.capture_timestamp = mf.start_time
        match.status = "matched"
        match.match_reason = f"MANUAL_OVERRIDE: {comment}"
        match.is_manual_override = True
    else:
        match.source_media_file_id = None
        match.status = "missing"
        match.match_reason = f"MANUAL_CLEAR: {comment}"
        match.is_manual_override = True

    db.session.commit()

    log_audit(
        action="MANUAL_CLIP_OVERRIDE",
        entity_type="TimetableStop",
        entity_id=stop.id,
        details={"camera_angle": camera_angle, "media_file_id": media_file_id, "comment": comment},
        user_id=session.get("user_id")
    )

    return jsonify({"success": True, "stop": stop.to_dict()})


# ============================================================================
# 4. PROCESSING CONTROLS: SINGLE STOP & BATCH JOB QUEUE
# ============================================================================

@automated_bp.route("/stops/<int:stop_id>/process", methods=["POST"])
def process_single_stop_endpoint(stop_id):
    """
    Processes a single stop on demand.
    """
    user_id = session.get("user_id")
    result = process_single_stop(stop_id, user_id=user_id)
    return jsonify(result), (200 if result["success"] else 400)


@automated_bp.route("/jobs/start", methods=["POST"])
def start_job():
    """
    Starts automated multi-angle batch processing job.
    """
    data = request.get_json() or {}
    journey_id_raw = data.get("journey_id")
    journey_id = None
    if journey_id_raw and str(journey_id_raw).lower() not in ("auto_create", "null", "undefined", ""):
        try:
            journey_id = int(journey_id_raw)
        except Exception:
            journey_id = None

    if not journey_id:
        active_j = Journey.query.filter_by(status="in_progress").first() or Journey.query.order_by(Journey.id.desc()).first()
        if active_j:
            journey_id = active_j.id
    timetable_id = data.get("timetable_id")

    if not journey_id or not timetable_id:
        return jsonify({"success": False, "error": "journey_id and timetable_id are required"}), 400

    tt = TimetableImport.query.get(timetable_id)
    if not tt:
        return jsonify({"success": False, "error": "Timetable not found"}), 404

    # Create StopProcessingJob
    job = StopProcessingJob(
        journey_id=journey_id,
        timetable_id=timetable_id,
        status="queued",
        total_stops=len(tt.stops),
        remaining_stops=len(tt.stops),
        job_settings_json=json.dumps(data)
    )
    db.session.add(job)
    db.session.commit()

    user_id = session.get("user_id")
    app = current_app._get_current_object()
    start_automated_job(app, job.id, user_id=user_id)

    return jsonify({"success": True, "job": job.to_dict()})


@automated_bp.route("/jobs/<int:job_id>/status", methods=["GET"])
def get_job_status(job_id):
    """Returns real-time progress and coverage stats of a processing job."""
    job = StopProcessingJob.query.get(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job not found"}), 404
    return jsonify({"success": True, "job": job.to_dict()})


@automated_bp.route("/jobs/<int:job_id>/pause", methods=["POST"])
def pause_job(job_id):
    success, msg = pause_automated_job(job_id)
    return jsonify({"success": success, "message": msg})


@automated_bp.route("/jobs/<int:job_id>/resume", methods=["POST"])
def resume_job(job_id):
    success, msg = resume_automated_job(job_id)
    return jsonify({"success": success, "message": msg})


@automated_bp.route("/jobs/<int:job_id>/cancel", methods=["POST"])
def cancel_job(job_id):
    success, msg = cancel_automated_job(job_id)
    return jsonify({"success": success, "message": msg})


@automated_bp.route("/jobs/<int:job_id>/retry-failed", methods=["POST"])
def retry_failed_stops(job_id):
    """Resets failed stops in the job and resumes processing."""
    job = StopProcessingJob.query.get(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job not found"}), 404

    failed_stops = TimetableStop.query.filter_by(
        timetable_id=job.timetable_id,
        processing_status="failed"
    ).all()

    for s in failed_stops:
        s.processing_status = "pending"
        s.error_message = None

    job.status = "queued"
    db.session.commit()

    user_id = session.get("user_id")
    app = current_app._get_current_object()
    start_automated_job(app, job.id, user_id=user_id)

    return jsonify({"success": True, "message": f"Retrying {len(failed_stops)} failed stops", "job": job.to_dict()})


@automated_bp.route("/reset-journey-passengers", methods=["POST"])
def reset_journey_passengers_endpoint():
    """
    Cleans up old/corrupted passenger identities and observations for the journey
    so automated processing can run cleanly with strict uniqueness and clean timelines.
    """
    data = request.get_json() or {}
    journey_id = data.get("journey_id")
    if not journey_id:
        active_j = Journey.query.filter_by(status="in_progress").first() or Journey.query.order_by(Journey.id.desc()).first()
        journey_id = active_j.id if active_j else 1

    # Delete observations, presence events, and passenger identities
    PassengerObservation.query.filter_by(journey_id=journey_id).delete()
    PresenceEvent.query.filter_by(journey_id=journey_id).delete()
    PassengerIdentity.query.filter_by(journey_id=journey_id).delete()

    # Reset timetable stops processing status
    timetable_id = data.get("timetable_id")
    if timetable_id:
        stops = TimetableStop.query.filter_by(timetable_id=timetable_id).all()
        for s in stops:
            s.processing_status = "pending"
            s.angle_a_count = 0
            s.angle_b_count = 0
            s.angle_c_count = 0
            s.total_raw_detections = 0
            s.estimated_unique_passengers = 0
            s.processed_at = None

    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Passenger identities and timeline records cleanly reset. Ready for re-processing."
    })


@automated_bp.route("/refresh-workspace", methods=["POST"])
def refresh_workspace_endpoint():
    """
    Clears the current analysis workspace state:
    1. Cancels active jobs and resets pending/running stop jobs.
    2. Resets timetable stops counts and processing status to 'pending'.
    3. Cleans current journey passenger identities, observations, and presence events.
    4. Resets uploads status.
    5. Returns clean active session state.
    """
    data = request.get_json() or {}
    journey_id = data.get("journey_id")
    if not journey_id:
        active_j = Journey.query.filter(Journey.status.in_(["active", "in_progress"])).order_by(Journey.id.desc()).first() or Journey.query.order_by(Journey.id.desc()).first()
        journey_id = active_j.id if active_j else None

    clear_uploads = data.get("clear_uploads", True)

    # Cancel any active running jobs
    active_jobs = StopProcessingJob.query.filter(StopProcessingJob.status.in_(["running", "queued", "paused"])).all()
    for job in active_jobs:
        job.status = "cancelled"

    if clear_uploads:
        StopProcessingJob.query.delete()
        StopMediaMatch.query.delete()
        TimetableStop.query.delete()
        TimetableImport.query.delete()
        SourceMediaFile.query.delete()
        SourceFolder.query.delete()
        PassengerObservation.query.delete()
        PresenceEvent.query.delete()
        PassengerIdentity.query.delete()
    else:
        if journey_id:
            PassengerObservation.query.filter_by(journey_id=journey_id).delete()
            PresenceEvent.query.filter_by(journey_id=journey_id).delete()
            PassengerIdentity.query.filter_by(journey_id=journey_id).delete()

        timetable_id = data.get("timetable_id")
        if not timetable_id:
            tt = TimetableImport.query.order_by(TimetableImport.id.desc()).first()
            if tt:
                timetable_id = tt.id

        if timetable_id:
            stops = TimetableStop.query.filter_by(timetable_id=timetable_id).all()
            for s in stops:
                s.processing_status = "pending"
                s.angle_a_count = 0
                s.angle_b_count = 0
                s.angle_c_count = 0
                s.total_raw_detections = 0
                s.estimated_unique_passengers = 0
                s.confirmed_identities = 0
                s.unidentified_passengers = 0
                s.needs_review_count = 0
                s.potential_duplicates = 0
                s.boarded_count = 0
                s.continuing_count = 0
                s.exited_count = 0
                s.occupancy = 0
                s.processed_at = None
                s.error_message = None

    db.session.commit()

    log_audit("REFRESH_WORKSPACE", details={"journey_id": journey_id, "clear_uploads": clear_uploads}, user_id=session.get("user_id"))

    return jsonify({
        "success": True,
        "message": "Workspace refreshed cleanly. Uploaded data, temporary processing state, passenger counts, and timelines have been reset."
    })


@automated_bp.route("/delete-temporary-data", methods=["POST"])
def delete_temporary_data_endpoint():
    """
    Cleans up disk artifacts (temporary extracted frames, passenger crop files, annotated previews)
    and purges leftover demo records from the database without touching genuine user data.
    """
    data = request.get_json() or {}
    files_deleted = 0

    # 1. Clean temporary extracted frames from Config.EXTRACTED_FRAMES_FOLDER
    try:
        frames_dir = Path(Config.EXTRACTED_FRAMES_FOLDER)
        if frames_dir.exists():
            for f in frames_dir.glob("*.jpg"):
                try:
                    f.unlink()
                    files_deleted += 1
                except Exception:
                    pass
            for f in frames_dir.glob("*.png"):
                try:
                    f.unlink()
                    files_deleted += 1
                except Exception:
                    pass
    except Exception as e:
        print(f"[Cleanup] Error deleting frames: {e}")

    # 2. Clean annotated images from uploads folder
    try:
        uploads_dir = Path(Config.UPLOAD_FOLDER)
        if uploads_dir.exists():
            for f in uploads_dir.glob("annotated_batch_*"):
                try:
                    f.unlink()
                    files_deleted += 1
                except Exception:
                    pass
            for f in uploads_dir.glob("annotated_*"):
                try:
                    f.unlink()
                    files_deleted += 1
                except Exception:
                    pass
    except Exception as e:
        print(f"[Cleanup] Error deleting annotated images: {e}")

    # 3. Purge demo SF bus/journey records if present
    demo_purged = []
    try:
        demo_journeys = Journey.query.filter(
            Journey.journey_code.in_(["JNY-GEARY-001", "JNY-SF-DEMO", "JNY-MULTI-TEST"])
        ).all()
        for dj in demo_journeys:
            PassengerObservation.query.filter_by(journey_id=dj.id).delete()
            PresenceEvent.query.filter_by(journey_id=dj.id).delete()
            PassengerIdentity.query.filter_by(journey_id=dj.id).delete()
            Upload.query.filter_by(journey_id=dj.id).delete()
            demo_purged.append(dj.journey_code)
            db.session.delete(dj)

        demo_buses = Bus.query.filter(
            Bus.registration_number.in_(["SF-7829-AI", "SF-4190-AI", "TEST-REG-999"])
        ).all()
        for dbus in demo_buses:
            if not dbus.journeys:
                db.session.delete(dbus)

        db.session.commit()
    except Exception as e:
        print(f"[Cleanup] Demo cleanup warning: {e}")
        db.session.rollback()

    log_audit("DELETE_TEMPORARY_DATA", details={"files_deleted": files_deleted, "demo_purged": demo_purged}, user_id=session.get("user_id"))

    return jsonify({
        "success": True,
        "files_deleted": files_deleted,
        "demo_purged": demo_purged,
        "message": f"Successfully deleted {files_deleted} temporary frame/cache files and purged {len(demo_purged)} demo records."
    })


@automated_bp.route("/active-session-state", methods=["GET"])
def get_active_session_state():
    """
    Returns latest scanned folders, active timetable, and processing job for seamless UI state restoration.
    """
    folders = {}
    for angle in ["A", "B", "C"]:
        f = SourceFolder.query.filter_by(camera_angle=angle).order_by(SourceFolder.last_scanned_at.desc()).first()
        if f:
            folders[angle] = f.to_dict()

    tt = TimetableImport.query.order_by(TimetableImport.id.desc()).first()
    tt_dict = None
    stops_list = []
    if tt:
        tt_dict = tt.to_dict()
        stops_list = [s.to_dict() for s in tt.stops]

    latest_job = StopProcessingJob.query.order_by(StopProcessingJob.id.desc()).first()

    return jsonify({
        "success": True,
        "folders": folders,
        "timetable": tt_dict,
        "stops": stops_list,
        "job": latest_job.to_dict() if latest_job else None
    })


# ============================================================================
# 5. FINAL REPORT & MANIFEST EXPORTS
# ============================================================================

@automated_bp.route("/timetable/<int:timetable_id>/export.csv", methods=["GET"])
def export_timetable_csv(timetable_id):
    csv_data = generate_stop_processing_csv(timetable_id)
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=stop_processing_report_{timetable_id}.csv"}
    )


@automated_bp.route("/timetable/<int:timetable_id>/export.xlsx", methods=["GET"])
def export_timetable_xlsx(timetable_id):
    xlsx_bytes = generate_stop_processing_excel(timetable_id)
    return Response(
        xlsx_bytes,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=stop_processing_report_{timetable_id}.xlsx"}
    )


@automated_bp.route("/timetable/<int:timetable_id>/manifest.csv", methods=["GET"])
def export_manifest(timetable_id):
    csv_data = generate_processing_manifest_csv(timetable_id)
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=processing_manifest_{timetable_id}.csv"}
    )


@automated_bp.route("/timetable/<int:timetable_id>/export.pdf", methods=["GET"])
def export_timetable_pdf(timetable_id):
    tt = TimetableImport.query.get_or_404(timetable_id)
    pdf_bytes = generate_passenger_attendance_pdf(journey_id=tt.journey_id)
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=passenger_attendance_report_{timetable_id}.pdf"}
    )


@automated_bp.route("/timetable/<int:timetable_id>/summary.pdf", methods=["GET"])
def export_timetable_summary_pdf(timetable_id):
    tt = TimetableImport.query.get_or_404(timetable_id)
    pdf_bytes = generate_journey_summary_pdf(journey_id=tt.journey_id)
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=journey_summary_report_{timetable_id}.pdf"}
    )
