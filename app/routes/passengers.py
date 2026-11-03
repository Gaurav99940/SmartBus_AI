from datetime import datetime
from flask import Blueprint, request, jsonify, session
from app.database import db
from app.models import PassengerIdentity, PassengerObservation, PresenceEvent, ManualReview
from app.services.journey_service import update_manual_passenger_status
from app.services.audit_service import log_audit

passengers_bp = Blueprint("passengers", __name__, url_prefix="/api/passengers")

@passengers_bp.route("", methods=["GET"])
def get_passengers():
    journey_id = request.args.get("journey_id")
    status = request.args.get("status")
    search = request.args.get("search")

    query = PassengerIdentity.query
    if journey_id:
        query = query.filter_by(journey_id=journey_id)
    if status:
        query = query.filter_by(status=status)
    if search:
        query = query.filter(PassengerIdentity.person_id.ilike(f"%{search}%"))

    passengers = query.order_by(PassengerIdentity.person_id).all()
    results = []
    for p in passengers:
        d = p.to_dict()
        # Add quick summary metrics
        d["observations_count"] = len(p.observations)
        d["thumbnail_url"] = f"/data/{p.thumbnail_path}" if p.thumbnail_path else None
        results.append(d)

    return jsonify(results)


@passengers_bp.route("/<int:passenger_id>", methods=["GET"])
def get_passenger_details(passenger_id):
    p = PassengerIdentity.query.get_or_404(passenger_id)

    # Fetch ordered presence timeline
    events = PresenceEvent.query.filter_by(passenger_identity_id=p.id).order_by(PresenceEvent.event_time.asc()).all()
    
    # Fetch ordered photo observations
    raw_observations = PassengerObservation.query.filter_by(passenger_identity_id=p.id).order_by(PassengerObservation.observation_time.asc()).all()

    # Biometric consistency verification:
    # Ensure observations presented in the passenger's timeline genuinely belong to this passenger
    from app.ai.matcher import get_passenger_reference_embeddings, cosine_similarity
    import numpy as np
    import json

    refs = get_passenger_reference_embeddings(p)
    anchor_emb = refs[0] if refs else None

    observations = []
    for obs in raw_observations:
        is_face_crop = bool(obs.crop_path and "face_" in obs.crop_path)
        has_conflict = False
        
        # If passenger identity has confirmed face references:
        if anchor_emb is not None:
            if obs.face_embedding_json:
                try:
                    obs_arr = np.array(json.loads(obs.face_embedding_json), dtype=np.float32)
                    sims = [cosine_similarity(obs_arr, r) for r in refs]
                    max_sim = max(sims) if sims else 0.0
                    # If manually merged or matches any pose in the passenger gallery:
                    if obs.duplicate_review_status == "RESOLVED_MERGED":
                        has_conflict = False
                    elif max_sim < 0.35:
                        has_conflict = True
                except Exception:
                    has_conflict = True
            elif not is_face_crop:
                # Body-only crop without face cannot be displayed in face crop timeline
                has_conflict = True

        if has_conflict:
            continue

        observations.append(obs)

    # Fetch reviews
    reviews = ManualReview.query.filter_by(passenger_identity_id=p.id).order_by(ManualReview.reviewed_at.asc()).all()

    data = p.to_dict()
    data["thumbnail_url"] = f"/data/{p.thumbnail_path}" if p.thumbnail_path else None
    # Deduplicate consecutive/redundant presence events for clean timeline
    clean_events = []
    seen_keys = set()
    for ev in events:
        key = (ev.event_type, ev.notes)
        if key not in seen_keys:
            seen_keys.add(key)
            clean_events.append(ev.to_dict())

    data["timeline_events"] = clean_events
    data["observations"] = [
        {
            "id": obs.id,
            "upload_id": obs.upload_id,
            "camera_angle": obs.camera_angle or "A",
            "observation_time": obs.observation_time.strftime("%Y-%m-%d %H:%M:%S") if obs.observation_time else None,
            "bbox": [obs.bbox_x, obs.bbox_y, obs.bbox_w, obs.bbox_h],
            "similarity_score": round(obs.similarity_score, 3) if obs.similarity_score else 0.0,
            "detection_confidence": round(obs.detection_confidence, 3) if obs.detection_confidence else 0.0,
            "confidence_label": obs.confidence_label,
            "review_status": obs.review_status,
            "is_potential_duplicate": obs.is_potential_duplicate,
            "temp_detection_id": obs.temp_detection_id,
            "quality_score": round(obs.quality_score, 2) if obs.quality_score else 1.0,
            "is_blur": obs.is_blur,
            "has_face": bool(obs.crop_path and "face_" in obs.crop_path),
            "is_face_crop": bool(obs.crop_path and "face_" in obs.crop_path),
            "crop_url": f"/data/{obs.crop_path}" if obs.crop_path else None
        }
        for obs in observations
    ]
    data["manual_reviews"] = [r.to_dict() for r in reviews]

    return jsonify(data)


@passengers_bp.route("/<int:passenger_id>/review", methods=["POST"])
def review_passenger(passenger_id):
    """
    Operator manual action:
    Body:
    {
        "status": "EXIT_CONFIRMED" | "PRESENT" | "NEEDS_REVIEW",
        "comment": "Confirmed passenger got off at Stop 2"
    }
    """
    data = request.get_json() or {}
    new_status = data.get("status")
    comment = data.get("comment", "").strip()

    valid_statuses = {"PRESENT", "POSSIBLY_ABSENT", "EXIT_UNCONFIRMED", "EXIT_CONFIRMED", "NEEDS_REVIEW"}
    if new_status not in valid_statuses:
        return jsonify({"error": f"Invalid status. Must be one of {valid_statuses}"}), 400

    reviewer_id = session.get("user_id")
    try:
        updated_p = update_manual_passenger_status(
            passenger_id=passenger_id,
            new_status=new_status,
            comment=comment,
            reviewer_id=reviewer_id
        )
        return jsonify({
            "message": "Passenger review updated successfully.",
            "passenger": updated_p.to_dict()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@passengers_bp.route("/<int:passenger_id>/seat", methods=["POST"])
def update_passenger_seat(passenger_id):
    """
    Update passenger's assigned seat (e.g. "1A", "3C") when they move or operator reassigns.
    """
    data = request.get_json() or {}
    new_seat = data.get("seat", "").strip().upper()
    if not new_seat:
        return jsonify({"error": "Seat number is required."}), 400

    p = PassengerIdentity.query.get_or_404(passenger_id)
    prev_seat = p.get_seat_number()
    p.previous_seat = prev_seat
    p.current_seat = new_seat
    p.updated_at = datetime.utcnow()

    # Log presence event
    ev = PresenceEvent(
        journey_id=p.journey_id,
        passenger_identity_id=p.id,
        event_type="SEAT_CHANGED",
        event_time=datetime.utcnow(),
        notes=f"Passenger {p.person_id} moved seats from {prev_seat} to {new_seat}",
        triggered_by_user_id=session.get("user_id")
    )
    db.session.add(ev)
    db.session.commit()

    log_audit("SEAT_CHANGE", entity_type="PassengerIdentity", entity_id=p.id, details={
        "person_id": p.person_id,
        "previous_seat": prev_seat,
        "new_seat": new_seat
    }, user_id=session.get("user_id"))

    return jsonify({
        "message": f"Seat updated from {prev_seat} to {new_seat}",
        "passenger": p.to_dict()
    })


@passengers_bp.route("/merge", methods=["POST"])
def merge_passengers_endpoint():
    """
    Operator manual workflow to merge two duplicate passenger identities:
    {
        "journey_id": 1,
        "source_person_id": "P007",
        "target_person_id": "P001",
        "comment": "Confirmed same passenger across stops"
    }
    """
    data = request.get_json() or {}
    journey_id = data.get("journey_id")
    source_pid = data.get("source_person_id")
    target_pid = data.get("target_person_id")
    comment = data.get("comment", "")

    if not journey_id or not source_pid or not target_pid:
        return jsonify({"error": "journey_id, source_person_id, and target_person_id are required."}), 400

    from app.services.journey_service import merge_duplicate_passengers
    try:
        res = merge_duplicate_passengers(
            journey_id=int(journey_id),
            source_person_id=str(source_pid).strip(),
            target_person_id=str(target_pid).strip(),
            reviewer_id=session.get("user_id"),
            comment=comment
        )
        return jsonify(res)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@passengers_bp.route("/date-wise-report", methods=["GET"])
def get_date_wise_report_endpoint():
    """
    Returns structured date-wise passenger report:
    - Grouped strictly by date
    - Separate lists for Angle A, Angle B, Angle C (and Unknown Angle)
    - Date-wise crop gallery corresponding 1:1 with that date's detections
    - Total Observations count & Canonical Unique Passengers count
    """
    from app.services.passenger_report_service import get_date_wise_passenger_report
    target_date = request.args.get("date")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    bus_id = request.args.get("bus_id")
    journey_id = request.args.get("journey_id")
    person_id = request.args.get("person_id")
    canonical_id = request.args.get("canonical_id")
    angle = request.args.get("angle")
    filter_status = request.args.get("filter_status")

    try:
        report_data = get_date_wise_passenger_report(
            target_date=target_date,
            start_date=start_date,
            end_date=end_date,
            bus_id=bus_id,
            journey_id=journey_id,
            person_id=person_id,
            canonical_id=canonical_id,
            angle=angle,
            filter_status=filter_status
        )
        return jsonify(report_data)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@passengers_bp.route("/merge-multiple", methods=["POST"])
def merge_multiple_endpoint():
    """
    Operator manual action to merge multiple selected Person IDs:
    Body:
    {
        "selected_person_ids": ["P001", "P007", "P014"],
        "master_person_id": "P001",
        "journey_id": 1, // optional
        "comment": "Verified same passenger in Angle A and Angle B"
    }
    """
    from app.services.passenger_report_service import merge_multiple_passengers
    data = request.get_json() or {}
    selected_pids = data.get("selected_person_ids", [])
    master_pid = data.get("master_person_id")
    journey_id = data.get("journey_id")
    comment = data.get("comment", "")

    if not selected_pids or len(selected_pids) < 2:
        return jsonify({"error": "At least two Person IDs must be selected to merge."}), 400
    if not master_pid:
        return jsonify({"error": "Primary / Master Person ID must be specified."}), 400

    reviewer_id = session.get("user_id")
    try:
        res = merge_multiple_passengers(
            selected_person_ids=selected_pids,
            master_person_id=master_pid,
            journey_id=journey_id,
            reviewer_id=reviewer_id,
            comment=comment
        )
        return jsonify(res)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@passengers_bp.route("/merge-reverse/<int:merge_record_id>", methods=["POST"])
def reverse_merge_endpoint(merge_record_id):
    """
    Operator / admin action to safely reverse an earlier merge operation.
    """
    from app.services.passenger_report_service import reverse_passenger_merge
    data = request.get_json() or {}
    reason = data.get("reason", "Operator requested undo")
    reviewer_id = session.get("user_id")

    try:
        res = reverse_passenger_merge(
            merge_record_id=merge_record_id,
            reviewer_id=reviewer_id,
            reason=reason
        )
        return jsonify(res)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@passengers_bp.route("/merge-history", methods=["GET"])
def get_merge_history_endpoint():
    """
    Returns auditable list of all passenger merges and their reversal statuses.
    """
    from app.services.passenger_report_service import get_merge_history
    journey_id = request.args.get("journey_id")
    try:
        records = get_merge_history(journey_id=journey_id)
        return jsonify({"success": True, "records": records})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@passengers_bp.route("/export-datewise.csv", methods=["GET"])
def export_datewise_csv_endpoint():
    """
    Exports CSV formatted date-wise report with full canonical identity and angle mapping.
    """
    from flask import Response
    from app.services.passenger_report_service import generate_date_wise_report_csv
    target_date = request.args.get("date")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    bus_id = request.args.get("bus_id")
    journey_id = request.args.get("journey_id")
    person_id = request.args.get("person_id")
    canonical_id = request.args.get("canonical_id")
    angle = request.args.get("angle")
    filter_status = request.args.get("filter_status")

    csv_data = generate_date_wise_report_csv(
        target_date=target_date,
        start_date=start_date,
        end_date=end_date,
        bus_id=bus_id,
        journey_id=journey_id,
        person_id=person_id,
        canonical_id=canonical_id,
        angle=angle,
        filter_status=filter_status
    )

    fname_date = target_date or (f"{start_date}_to_{end_date}" if start_date and end_date else "all_dates")
    filename = f"passenger_datewise_report_{fname_date}.csv"

    log_audit("EXPORT_DATEWISE_CSV", details={
        "date": target_date,
        "bus_id": bus_id,
        "journey_id": journey_id
    }, user_id=session.get("user_id"))

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@passengers_bp.route("/export-datewise.xlsx", methods=["GET"])
def export_datewise_excel_endpoint():
    """
    Exports beautifully formatted Excel (.xlsx) workbook for the date-wise report.
    """
    from flask import Response
    from app.services.passenger_report_service import generate_date_wise_report_excel
    target_date = request.args.get("date")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    bus_id = request.args.get("bus_id")
    journey_id = request.args.get("journey_id")
    person_id = request.args.get("person_id")
    canonical_id = request.args.get("canonical_id")
    angle = request.args.get("angle")
    filter_status = request.args.get("filter_status")

    excel_data = generate_date_wise_report_excel(
        target_date=target_date,
        start_date=start_date,
        end_date=end_date,
        bus_id=bus_id,
        journey_id=journey_id,
        person_id=person_id,
        canonical_id=canonical_id,
        angle=angle,
        filter_status=filter_status
    )

    fname_date = target_date or (f"{start_date}_to_{end_date}" if start_date and end_date else "all_dates")
    filename = f"passenger_datewise_report_{fname_date}.xlsx"

    log_audit("EXPORT_DATEWISE_EXCEL", details={
        "date": target_date,
        "bus_id": bus_id,
        "journey_id": journey_id
    }, user_id=session.get("user_id"))

    return Response(
        excel_data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@passengers_bp.route("/export-datewise.pdf", methods=["GET"])
def export_datewise_pdf_endpoint():
    """
    Exports certified landscape PDF report with embedded face photo crops and date/angle breakdown.
    """
    from flask import Response
    from app.services.passenger_report_service import generate_date_wise_report_pdf
    target_date = request.args.get("date")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    bus_id = request.args.get("bus_id")
    journey_id = request.args.get("journey_id")
    person_id = request.args.get("person_id")
    canonical_id = request.args.get("canonical_id")
    angle = request.args.get("angle")
    filter_status = request.args.get("filter_status")

    pdf_data = generate_date_wise_report_pdf(
        target_date=target_date,
        start_date=start_date,
        end_date=end_date,
        bus_id=bus_id,
        journey_id=journey_id,
        person_id=person_id,
        canonical_id=canonical_id,
        angle=angle,
        filter_status=filter_status
    )

    fname_date = target_date or (f"{start_date}_to_{end_date}" if start_date and end_date else "all_dates")
    filename = f"passenger_datewise_report_{fname_date}.pdf"

    log_audit("EXPORT_DATEWISE_PDF", details={
        "date": target_date,
        "bus_id": bus_id,
        "journey_id": journey_id
    }, user_id=session.get("user_id"))

    return Response(
        pdf_data,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@passengers_bp.route("/provenance/<person_id>", methods=["GET"])
def get_passenger_provenance_endpoint(person_id):
    """
    Returns full provenance details and transition history for a given Person ID:
    previous ID, new master ID, merger timestamp, reviewer, rationale, and observation count.
    """
    from app.services.passenger_report_service import get_identity_provenance
    try:
        data = get_identity_provenance(person_id)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 404




