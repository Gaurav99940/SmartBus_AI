import os
import uuid
from pathlib import Path
from datetime import datetime
from flask import Blueprint, request, jsonify, session, current_app, send_from_directory
from werkzeug.utils import secure_filename

from app.config import Config
from app.database import db
from app.models import Upload, Journey, GPSLocation, CameraAngleConfig, PassengerObservation
from app.services.exif_service import extract_metadata, compute_file_hash
from app.ai.face_pipeline import detect_and_extract_faces
from app.services.journey_service import (
    process_upload_for_journey, process_multi_angle_batch, review_observation
)
from app.services.audit_service import log_audit

uploads_bp = Blueprint("uploads", __name__, url_prefix="/api/uploads")

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in Config.ALLOWED_EXTENSIONS


@uploads_bp.route("/inspect-metadata", methods=["POST"])
def inspect_metadata():
    """
    Pre-upload inspection endpoint.
    Extracts EXIF metadata without committing to database,
    allowing operator to verify or adjust GPS coordinates and timestamp in the UI.
    """
    if "photo" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["photo"]
    if file.filename == "" or not allowed_file(file.filename):
        return jsonify({"error": "Invalid file format. Please upload JPG, PNG or WEBP."}), 400

    clean_orig = secure_filename(file.filename) or "temp_file.jpg"
    ext = clean_orig.rsplit(".", 1)[1].lower() if "." in clean_orig else "jpg"
    temp_name = f"temp_{uuid.uuid4().hex[:10]}.{ext}"
    temp_path = Config.UPLOAD_FOLDER / temp_name
    Config.UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
    file.save(str(temp_path))

    is_video = ext in ("mp4", "avi", "mov", "mkv", "wmv", "webm", "m4v")
    if is_video:
        from app.services.media_service import extract_media_metadata
        v_meta = extract_media_metadata(str(temp_path))
        capture_dt = v_meta.get("start_time")
        meta = {
            "has_exif": capture_dt is not None,
            "capture_timestamp": capture_dt,
            "latitude": None,
            "longitude": None,
            "gps_accuracy": None,
            "device_make": "CCTV / Video Device",
            "device_model": ext.upper(),
            "warnings": []
        }
    else:
        meta = extract_metadata(str(temp_path))

    file_hash = compute_file_hash(str(temp_path))
    
    # Clean up temp
    try:
        os.remove(temp_path)
    except Exception:
        pass

    return jsonify({
        "has_exif": meta["has_exif"],
        "capture_timestamp": meta["capture_timestamp"].strftime("%Y-%m-%d %H:%M:%S") if meta["capture_timestamp"] else None,
        "latitude": meta["latitude"],
        "longitude": meta["longitude"],
        "gps_accuracy": meta["gps_accuracy"],
        "device_make": meta["device_make"],
        "device_model": meta["device_model"],
        "warnings": meta["warnings"],
        "file_hash": file_hash
    })


@uploads_bp.route("", methods=["POST"])
def upload_bus_photo():
    """
    Full upload and AI processing endpoint.
    Accepts:
      - 'photo': multipart file
      - 'journey_id': int (required)
      - 'capture_timestamp': optional string (YYYY-MM-DD HH:MM:SS)
      - 'latitude': optional float
      - 'longitude': optional float
      - 'location_source': optional ('EXIF', 'DEVICE_API', 'MANUAL_ENTRY', 'MAP_PICKER')
    """
    if "photo" not in request.files:
        return jsonify({"error": "No photo provided in request."}), 400

    file = request.files["photo"]
    if file.filename == "" or not allowed_file(file.filename):
        return jsonify({"error": "Invalid file type. Allowed: PNG, JPG, JPEG, WEBP."}), 400

    journey_id = request.form.get("journey_id")
    if not journey_id:
        return jsonify({"error": "journey_id is required."}), 400

    journey = Journey.query.get(journey_id)
    if not journey:
        return jsonify({"error": f"Journey #{journey_id} does not exist."}), 404

    # Generate persistent unique storage filename
    Config.UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
    clean_orig_name = secure_filename(file.filename) or "bus_photo.jpg"
    ext = clean_orig_name.rsplit(".", 1)[1].lower() if "." in clean_orig_name else "jpg"
    unique_filename = f"bus_{journey.id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.{ext}"
    saved_path = Config.UPLOAD_FOLDER / unique_filename
    file.save(str(saved_path))
    file_size = os.path.getsize(saved_path)

    # 1. Compute SHA-256 file hash to check duplicates
    file_hash = compute_file_hash(str(saved_path))
    existing_duplicate = Upload.query.filter_by(journey_id=journey.id, file_hash=file_hash).first()
    duplicate_warning = None
    if existing_duplicate:
        duplicate_warning = f"Notice: Identical photo was already uploaded for this journey (Upload #{existing_duplicate.id} at {existing_duplicate.upload_timestamp.strftime('%H:%M:%S')})."

    # 2. Extract EXIF metadata
    meta = extract_metadata(str(saved_path))
    warnings = list(meta["warnings"])
    if duplicate_warning:
        warnings.insert(0, duplicate_warning)

    # Resolve timestamp: Manual override > EXIF > Current server time
    manual_time_str = request.form.get("capture_timestamp")
    final_capture_time = None
    if manual_time_str:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                final_capture_time = datetime.strptime(manual_time_str.strip(), fmt)
                break
            except ValueError:
                pass

    if not final_capture_time:
        final_capture_time = meta["capture_timestamp"] or datetime.utcnow()

    # Resolve GPS: Manual / Device override > EXIF > None
    final_lat = None
    final_lon = None
    loc_source = request.form.get("location_source") or "NONE"

    req_lat = request.form.get("latitude")
    req_lon = request.form.get("longitude")
    if req_lat and req_lon:
        try:
            final_lat = float(req_lat)
            final_lon = float(req_lon)
            if not loc_source or loc_source == "NONE":
                loc_source = "MANUAL_ENTRY"
        except ValueError:
            pass

    if final_lat is None and meta["latitude"] is not None:
        final_lat = meta["latitude"]
        final_lon = meta["longitude"]
        loc_source = "EXIF"

    # 3. Create Upload DB record
    user_id = session.get("user_id")
    upload = Upload(
        journey_id=journey.id,
        file_path=str(saved_path),
        original_filename=clean_orig_name,
        file_size=file_size,
        file_hash=file_hash,
        capture_timestamp=final_capture_time,
        upload_timestamp=datetime.utcnow(),
        latitude=final_lat,
        longitude=final_lon,
        gps_accuracy=meta.get("gps_accuracy"),
        location_source=loc_source,
        status="processing"
    )
    db.session.add(upload)
    db.session.flush()

    # Record GPS location if available
    if final_lat is not None and final_lon is not None:
        gps_record = GPSLocation(
            upload_id=upload.id,
            journey_id=journey.id,
            latitude=final_lat,
            longitude=final_lon,
            accuracy=meta.get("gps_accuracy"),
            location_source=loc_source,
            recorded_at=final_capture_time
        )
        db.session.add(gps_record)

    db.session.commit()

    # 4. Run AI Detection & Face Recognition pipeline
    try:
        # Color eligibility evaluation: Automatically skip black-and-white footage before AI processing
        from app.ai.color_filter import evaluate_media_color_eligibility
        color_eval = evaluate_media_color_eligibility(str(saved_path))
        upload.is_color = color_eval["is_color"]
        upload.color_score = color_eval["color_score"]
        upload.color_classification_details_json = json.dumps(color_eval.get("details", {}))

        if not color_eval["is_color"]:
            upload.status = "SKIPPED_BLACK_AND_WHITE"
            upload.skip_reason = color_eval["skip_reason"]
            upload.processing_notes = f"Skipped: {color_eval['skip_reason']}"
            db.session.commit()
            return jsonify({
                "success": True,
                "upload": upload.to_dict(),
                "status": "SKIPPED_BLACK_AND_WHITE",
                "is_color": False,
                "color_score": color_eval["color_score"],
                "skip_reason": color_eval["skip_reason"],
                "passengers_detected": 0,
                "matches": [],
                "warnings": [color_eval["skip_reason"]],
                "annotated_url": None
            }), 200

        detected_faces, _ = detect_and_extract_faces(str(saved_path))
        result = process_upload_for_journey(upload.id, detected_faces, user_id=user_id)
        
        return jsonify({
            "success": True,
            "upload": upload.to_dict(),
            "passengers_detected": result["passengers_detected"],
            "matches": result["matches"],
            "warnings": warnings,
            "annotated_url": f"/data/{upload.annotated_path}" if upload.annotated_path else None
        }), 201

    except Exception as e:
        upload.status = "failed"
        upload.processing_notes = f"AI Processing Error: {str(e)}"
        db.session.commit()
        return jsonify({
            "error": f"Failed to process photo with AI: {str(e)}",
            "upload_id": upload.id
        }), 500


@uploads_bp.route("/<int:upload_id>", methods=["GET"])
def get_upload(upload_id):
    upload = Upload.query.get_or_404(upload_id)
    obs = [o.to_dict() for o in upload.observations]
    data = upload.to_dict()
    data["observations"] = obs
    return jsonify(data)


@uploads_bp.route("", methods=["GET"])
def list_uploads():
    journey_id = request.args.get("journey_id")
    query = Upload.query
    if journey_id:
        query = query.filter_by(journey_id=journey_id)
    uploads = query.order_by(Upload.capture_timestamp.desc()).all()
    return jsonify([u.to_dict() for u in uploads])


@uploads_bp.route("/seating-config", methods=["GET"])
def get_seating_configs():
    """Returns seating area configs for Angle A, B, and C."""
    bus_id = request.args.get("bus_id")
    query = CameraAngleConfig.query
    if bus_id:
        configs = query.filter_by(bus_id=bus_id).order_by(CameraAngleConfig.camera_angle).all()
        if not configs:
            configs = CameraAngleConfig.query.filter_by(bus_id=None).order_by(CameraAngleConfig.camera_angle).all()
    else:
        configs = query.order_by(CameraAngleConfig.camera_angle).all()
    return jsonify([c.to_dict() for c in configs])


@uploads_bp.route("/seating-config", methods=["POST"])
def update_seating_config():
    """Allows operator or admin to configure seating section labels, capacity, and ROI."""
    data = request.get_json() or {}
    angle = (data.get("camera_angle") or "").upper().strip()
    if angle not in ["A", "B", "C"]:
        return jsonify({"error": "Valid camera_angle ('A', 'B', 'C') is required."}), 400

    cfg = CameraAngleConfig.query.filter_by(camera_angle=angle, bus_id=data.get("bus_id")).first()
    if not cfg:
        cfg = CameraAngleConfig(camera_angle=angle, bus_id=data.get("bus_id"))
        db.session.add(cfg)

    if "title" in data and data["title"]:
        cfg.title = data["title"].strip()
    if "seating_area_label" in data and data["seating_area_label"]:
        cfg.seating_area_label = data["seating_area_label"].strip()
    if "target_seats" in data:
        cfg.target_seats = data["target_seats"].strip()
    if "target_capacity" in data:
        try:
            cfg.target_capacity = int(data["target_capacity"])
        except (ValueError, TypeError):
            pass
    if "roi" in data:
        cfg.roi_json = json.dumps(data["roi"]) if isinstance(data["roi"], dict) else str(data["roi"])

    cfg.updated_at = datetime.utcnow()
    db.session.commit()

    log_audit("UPDATE_SEATING_CONFIG", details=data, user_id=session.get("user_id"))

    return jsonify({
        "success": True,
        "message": f"Seating configuration for Angle {angle} updated successfully.",
        "config": cfg.to_dict()
    })


@uploads_bp.route("/multi-angle", methods=["POST"])
def upload_multi_angle_bus_photos():
    """
    Submits up to three interior camera-angle photographs (Angle A, Angle B, Angle C)
    for the same bus journey in one submission or partial submissions.
    """
    journey_id = request.form.get("journey_id")
    if not journey_id:
        return jsonify({"error": "journey_id is required."}), 400

    journey = Journey.query.get(journey_id)
    if not journey:
        return jsonify({"error": f"Journey #{journey_id} does not exist."}), 404

    # Check which angle files are present
    angle_files = {}
    for angle_char in ["A", "B", "C"]:
        field_key = f"photo_{angle_char.lower()}" # e.g. photo_a, photo_b, photo_c
        alt_key = f"photo_{angle_char}"
        file_obj = request.files.get(field_key) or request.files.get(alt_key)
        if file_obj and file_obj.filename != "":
            if not allowed_file(file_obj.filename):
                return jsonify({"error": f"Invalid file type for Angle {angle_char}. Allowed: JPG, PNG, WEBP."}), 400
            angle_files[angle_char] = file_obj

    # If only single legacy 'photo' field provided, treat as Angle A
    if len(angle_files) == 0 and "photo" in request.files:
        p_file = request.files["photo"]
        if p_file and p_file.filename != "" and allowed_file(p_file.filename):
            angle_files["A"] = p_file

    if len(angle_files) == 0:
        return jsonify({"error": "Please select at least one bus camera-angle photo (Angle A, B, or C) to upload."}), 400

    batch_id = uuid.uuid4().hex[:12]
    user_id = session.get("user_id")
    Config.UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

    # Master GPS coordinates & Route Stop
    req_lat = request.form.get("latitude")
    req_lon = request.form.get("longitude")
    loc_source = request.form.get("location_source") or "NONE"
    master_lat = float(req_lat) if req_lat else None
    master_lon = float(req_lon) if req_lon else None

    req_stop_id = request.form.get("stop_id") or request.form.get("route_stop_id")
    final_stop_id = None
    if req_stop_id:
        try:
            final_stop_id = int(req_stop_id)
        except (ValueError, TypeError):
            final_stop_id = None

    if final_stop_id is None and master_lat is not None and master_lon is not None:
        from app.models import RouteStop
        bus_stops = RouteStop.query.filter_by(bus_id=journey.bus_id).all() if journey.bus_id else []
        if not bus_stops:
            bus_stops = RouteStop.query.all()
        if bus_stops:
            closest_stop = min(bus_stops, key=lambda s: (s.latitude - master_lat)**2 + (s.longitude - master_lon)**2)
            final_stop_id = closest_stop.id

    angle_uploads = {}
    timestamp_formats = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M")

    for angle, f_obj in angle_files.items():
        clean_name = secure_filename(f_obj.filename) or f"angle_{angle}.jpg"
        ext = clean_name.rsplit(".", 1)[1].lower() if "." in clean_name else "jpg"
        unique_fname = f"bus_{journey.id}_batch_{batch_id}_angle_{angle}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}.{ext}"
        saved_path = Config.UPLOAD_FOLDER / unique_fname
        f_obj.save(str(saved_path))
        f_size = os.path.getsize(saved_path)

        # File hash & EXIF/video metadata
        f_hash = compute_file_hash(str(saved_path))
        is_video = ext in ("mp4", "avi", "mov", "mkv", "wmv", "webm", "m4v")
        effective_analysis_path = str(saved_path)

        if is_video:
            from app.services.media_service import extract_frame_at_timestamp, extract_media_metadata
            v_meta = extract_media_metadata(str(saved_path))
            v_start = v_meta.get("start_time") or datetime.utcnow()
            meta = {
                "capture_timestamp": v_start,
                "latitude": None,
                "longitude": None,
                "gps_accuracy": None
            }
            # Extract first/middle frame for AI analysis
            extracted, f_err = extract_frame_at_timestamp(str(saved_path), v_start, v_start)
            if extracted and extracted.get("frame_path"):
                effective_analysis_path = extracted["frame_path"]
        else:
            meta = extract_metadata(str(saved_path))

        # Check duplicate
        existing_dup = Upload.query.filter_by(journey_id=journey.id, file_hash=f_hash).first()
        dup_note = None
        if existing_dup:
            dup_note = f"Notice: Identical photo was already uploaded for this journey (Upload #{existing_dup.id})."

        # Resolve angle capture timestamp
        angle_time_str = request.form.get(f"capture_timestamp_{angle.lower()}") or request.form.get("capture_timestamp")
        final_capture_time = None
        if angle_time_str:
            for fmt in timestamp_formats:
                try:
                    final_capture_time = datetime.strptime(angle_time_str.strip(), fmt)
                    break
                except ValueError:
                    pass

        if not final_capture_time:
            final_capture_time = meta["capture_timestamp"] or datetime.utcnow()

        # Resolve GPS
        angle_lat = master_lat
        angle_lon = master_lon
        final_loc_src = loc_source

        if angle_lat is None and meta["latitude"] is not None:
            angle_lat = meta["latitude"]
            angle_lon = meta["longitude"]
            final_loc_src = "EXIF"

        # Create Upload DB record
        upload_rec = Upload(
            journey_id=journey.id,
            bus_id=journey.bus_id,
            camera_angle=angle,
            batch_id=batch_id,
            file_path=effective_analysis_path,
            original_filename=clean_name,
            file_size=f_size,
            file_hash=f_hash,
            capture_timestamp=final_capture_time,
            upload_timestamp=datetime.utcnow(),
            latitude=angle_lat,
            longitude=angle_lon,
            gps_accuracy=meta.get("gps_accuracy"),
            location_source=final_loc_src,
            route_stop_id=final_stop_id,
            status="processing"
        )
        if dup_note:
            upload_rec.processing_notes = dup_note
        db.session.add(upload_rec)
        db.session.flush()

        # GPS Location
        if angle_lat is not None and angle_lon is not None:
            gps_rec = GPSLocation(
                upload_id=upload_rec.id,
                journey_id=journey.id,
                latitude=angle_lat,
                longitude=angle_lon,
                accuracy=meta.get("gps_accuracy"),
                location_source=final_loc_src,
                recorded_at=final_capture_time
            )
            db.session.add(gps_rec)

        angle_uploads[angle] = upload_rec

    db.session.commit()

    # Run multi-angle AI pipeline with resilient error handling
    try:
        result = process_multi_angle_batch(
            journey_id=journey.id,
            batch_id=batch_id,
            angle_uploads=angle_uploads,
            user_id=user_id,
            stop_id=final_stop_id
        )
        return jsonify(result), 201
    except Exception as e:
        import traceback
        traceback.print_exc()
        db.session.rollback()
        for u in angle_uploads.values():
            try:
                u.status = "failed"
                u.processing_notes = f"Multi-angle AI processing error: {str(e)}"
            except Exception:
                pass
        try:
            db.session.commit()
        except Exception:
            pass
        return jsonify({"success": False, "error": f"AI Pipeline error: {str(e)}"}), 500


@uploads_bp.route("/observations/<int:obs_id>/review", methods=["POST"])
def review_observation_endpoint(obs_id):
    """
    Operator action to review an individual passenger detection:
    Body:
    {
        "action": "CONFIRM" | "FALSE_POSITIVE" | "LINK_PASSENGER" | "REQUEST_REVIEW",
        "target_person_id": "P001" (optional, required for LINK_PASSENGER),
        "comment": "Verified seated in Row 2"
    }
    """
    data = request.get_json() or {}
    action = data.get("action")
    target_person_id = data.get("target_person_id")
    comment = data.get("comment", "")
    reviewer_id = session.get("user_id")

    if not action:
        return jsonify({"error": "Review action is required."}), 400

    try:
        updated_obs = review_observation(
            observation_id=obs_id,
            action=action,
            target_person_id=target_person_id,
            comment=comment,
            reviewer_id=reviewer_id
        )
        return jsonify({
            "success": True,
            "message": f"Observation #{obs_id} review updated with action '{action}'.",
            "observation": updated_obs.to_dict()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@uploads_bp.route("/manual-annotation", methods=["POST"])
def save_manual_annotation():
    """
    Saves manually drawn bounding box annotations from the interactive canvas.
    Payload:
    {
        "upload_id": 12,
        "camera_angle": "A",
        "annotations": [
            {
                "label": "P_MANUAL_1",
                "bbox": [x_min, y_min, x_max, y_max], # or [x, y, w, h]
                "crop_data": "data:image/jpeg;base64,..."
            }
        ],
        "comment": "Manually added missed passenger"
    }
    """
    import base64
    from app.models import PassengerIdentity, PresenceEvent

    data = request.get_json() or {}
    upload_id = data.get("upload_id")
    camera_angle = data.get("camera_angle", "A")
    comment = data.get("comment", "Manual bounding box addition")
    reviewer_id = session.get("user_id")

    if not upload_id:
        latest = Upload.query.order_by(Upload.id.desc()).first()
        if latest:
            upload_id = latest.id
        else:
            return jsonify({"error": "No uploads available to annotate. Please upload an interior photo first."}), 400

    upload = Upload.query.get(upload_id)
    if not upload:
        return jsonify({"error": f"Upload #{upload_id} not found."}), 404

    # Allow single annotation or list of annotations
    annotations = data.get("annotations")
    if not annotations:
        if "bbox" in data:
            annotations = [{
                "label": data.get("label", "P_MANUAL_1"),
                "bbox": data.get("bbox"),
                "crop_data": data.get("crop_data")
            }]
        else:
            return jsonify({"error": "No annotations provided."}), 400

    created_obs = []
    Config.CROP_FOLDER.mkdir(parents=True, exist_ok=True)

    for item in annotations:
        raw_label = (item.get("label") or "").strip()
        if not raw_label:
            from app.ai.matcher import generate_next_person_id
            label = generate_next_person_id(upload.journey_id)
        else:
            label = raw_label
        raw_box = item.get("bbox") or [0, 0, 50, 50]
        crop_data_str = item.get("crop_data")

        # Convert bbox to [x, y, w, h]
        if len(raw_box) == 4:
            b0, b1, b2, b3 = raw_box
            # If coordinates are [x_min, y_min, x_max, y_max]
            if b2 > b0 and b3 > b1 and (b2 - b0 != b2 or b3 - b1 != b3):
                x = int(round(min(b0, b2)))
                y = int(round(min(b1, b3)))
                w = int(round(abs(b2 - b0)))
                h = int(round(abs(b3 - b1)))
            else:
                x = int(round(b0))
                y = int(round(b1))
                w = int(round(b2))
                h = int(round(b3))
        else:
            x, y, w, h = 0, 0, 50, 50

        # Save thumbnail crop
        crop_rel_path = None
        if crop_data_str and "base64," in crop_data_str:
            try:
                header, encoded = crop_data_str.split("base64,", 1)
                crop_bytes = base64.b64decode(encoded)
                crop_filename = f"crop_manual_{upload.id}_{uuid.uuid4().hex[:6]}.jpg"
                crop_disk_path = Config.CROP_FOLDER / crop_filename
                with open(crop_disk_path, "wb") as f:
                    f.write(crop_bytes)
                crop_rel_path = f"crops/{crop_filename}"
            except Exception as e:
                print(f"[Manual Annotation Warning] Failed to decode crop: {e}")

        # If no client crop was provided, crop from original image
        if not crop_rel_path and os.path.exists(upload.file_path):
            try:
                import cv2
                orig_img = cv2.imread(upload.file_path)
                if orig_img is not None:
                    img_h, img_w = orig_img.shape[:2]
                    cx1 = max(0, min(img_w - 1, x))
                    cy1 = max(0, min(img_h - 1, y))
                    cx2 = max(0, min(img_w, x + w))
                    cy2 = max(0, min(img_h, y + h))
                    if cx2 > cx1 and cy2 > cy1:
                        crop = orig_img[cy1:cy2, cx1:cx2]
                        crop_filename = f"crop_manual_{upload.id}_{uuid.uuid4().hex[:6]}.jpg"
                        crop_disk_path = Config.CROP_FOLDER / crop_filename
                        cv2.imwrite(str(crop_disk_path), crop)
                        crop_rel_path = f"crops/{crop_filename}"
            except Exception as e:
                print(f"[Manual Annotation Warning] Fallback image crop failed: {e}")

        # Check or create PassengerIdentity
        passenger = PassengerIdentity.query.filter_by(
            journey_id=upload.journey_id,
            person_id=label
        ).first()

        if not passenger:
            passenger = PassengerIdentity(
                journey_id=upload.journey_id,
                person_id=label,
                first_seen_time=upload.capture_timestamp or datetime.utcnow(),
                last_seen_time=upload.capture_timestamp or datetime.utcnow(),
                first_seen_upload_id=upload.id,
                last_seen_upload_id=upload.id,
                first_latitude=upload.latitude,
                first_longitude=upload.longitude,
                last_latitude=upload.latitude,
                last_longitude=upload.longitude,
                status="PRESENT",
                total_observations=1,
                thumbnail_path=crop_rel_path,
                manual_notes=comment,
                confirmed_by_id=reviewer_id,
                confirmed_at=datetime.utcnow()
            )
            db.session.add(passenger)
            db.session.flush()
        else:
            passenger.total_observations += 1
            passenger.status = "PRESENT"
            passenger.last_seen_time = upload.capture_timestamp or datetime.utcnow()
            passenger.last_seen_upload_id = upload.id
            if crop_rel_path and not passenger.thumbnail_path:
                passenger.thumbnail_path = crop_rel_path

        # Create Observation
        obs = PassengerObservation(
            upload_id=upload.id,
            journey_id=upload.journey_id,
            passenger_identity_id=passenger.id,
            camera_angle=camera_angle or upload.camera_angle or "A",
            temp_detection_id=raw_label or label,
            bbox_x=x,
            bbox_y=y,
            bbox_w=w,
            bbox_h=h,
            detection_confidence=1.0,
            face_confidence=1.0,
            similarity_score=1.0,
            confidence_label="Manual",
            quality_score=1.0,
            review_status="USER_ADDED",
            is_potential_duplicate=False,
            crop_path=crop_rel_path,
            observation_time=upload.capture_timestamp or datetime.utcnow(),
            created_at=datetime.utcnow()
        )
        db.session.add(obs)
        db.session.flush()

        # Presence event
        event = PresenceEvent(
            journey_id=upload.journey_id,
            passenger_identity_id=passenger.id,
            upload_id=upload.id,
            event_type="MANUAL_OVERRIDE",
            event_time=upload.capture_timestamp or datetime.utcnow(),
            latitude=upload.latitude,
            longitude=upload.longitude,
            notes=f"Manually added bounding box ({label}) in Angle {camera_angle}",
            triggered_by_user_id=reviewer_id
        )
        db.session.add(event)
        created_obs.append(obs)

    # Update upload counts
    upload.passengers_detected_count = len(upload.observations)
    db.session.commit()

    log_audit(
        action="MANUAL_BOUNDING_BOX_ADD",
        entity_type="uploads",
        entity_id=upload.id,
        details={"upload_id": upload.id, "count_added": len(created_obs), "labels": [o.temp_detection_id for o in created_obs]},
        user_id=reviewer_id
    )

    return jsonify({
        "success": True,
        "message": f"Successfully saved {len(created_obs)} manual passenger correction(s).",
        "created_observations": [o.to_dict() for o in created_obs],
        "total_passengers_detected": upload.passengers_detected_count
    }), 201


@uploads_bp.route("/observations/<int:obs_id>", methods=["DELETE"])
def delete_observation(obs_id):
    """
    Deletes an observation (e.g. user removes a manual box or discards false detection).
    """
    obs = PassengerObservation.query.get(obs_id)
    if not obs:
        return jsonify({"error": f"Observation #{obs_id} not found."}), 404

    upload = obs.upload
    upload_id = upload.id if upload else None
    person_label = obs.temp_detection_id or (obs.passenger.person_id if obs.passenger else "Unknown")

    db.session.delete(obs)
    if upload:
        upload.passengers_detected_count = max(0, len(upload.observations) - 1)

    db.session.commit()

    log_audit(
        action="DELETE_OBSERVATION",
        entity_type="passenger_observations",
        entity_id=obs_id,
        details={"upload_id": upload_id, "deleted_label": person_label},
        user_id=session.get("user_id")
    )

    return jsonify({
        "success": True,
        "message": f"Observation #{obs_id} ({person_label}) deleted successfully."
    })


