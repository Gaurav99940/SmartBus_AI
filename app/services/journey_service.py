import os
from pathlib import Path
import json
from datetime import datetime
import numpy as np
from app.config import Config
from app.database import db
from app.models import (
    Journey, Upload, PassengerIdentity, PassengerObservation,
    PresenceEvent, GPSLocation, ManualReview, CameraAngleConfig
)
from app.ai.matcher import (
    match_faces_to_journey_identities, update_passenger_embedding,
    update_passenger_gallery_and_embedding, update_passenger_clothes,
    generate_next_person_id, cosine_similarity,
    match_angle_detections_to_passengers
)
from app.ai.face_pipeline import render_annotated_image
from app.ai.person_pipeline import detect_persons_and_faces, render_multi_angle_annotated_hud
from app.services.audit_service import log_audit

def process_upload_for_journey(upload_id, detected_faces=None, user_id=None):
    """
    Main state machine orchestrator for a new photo upload in a journey:
    1. Matches detected faces to known journey passenger identities
    2. Updates or creates PassengerIdentity records
    3. Creates immutable PassengerObservation records
    4. Updates presence state machine (detected vs missed passengers)
    5. Creates PresenceEvents
    6. Renders annotated photo HUD
    7. Updates Upload status and counts
    """
    if detected_faces is None:
        detected_faces = []
    upload = Upload.query.get(upload_id)
    if not upload:
        raise ValueError(f"Upload #{upload_id} not found.")

    journey = Journey.query.get(upload.journey_id)
    if not journey:
        raise ValueError(f"Journey #{upload.journey_id} not found.")

    capture_time = upload.capture_timestamp or datetime.utcnow()
    gps_lat = upload.latitude
    gps_lon = upload.longitude

    # Color eligibility evaluation: Automatically skip black-and-white footage before AI processing
    if upload.file_path and os.path.exists(upload.file_path):
        from app.ai.color_filter import evaluate_media_color_eligibility
        color_eval = evaluate_media_color_eligibility(upload.file_path)
        upload.is_color = color_eval["is_color"]
        upload.color_score = color_eval["color_score"]
        upload.color_classification_details_json = json.dumps(color_eval.get("details", {}))

        if not color_eval["is_color"]:
            upload.status = "SKIPPED_BLACK_AND_WHITE"
            upload.skip_reason = color_eval["skip_reason"]
            upload.processing_notes = f"Skipped: {color_eval['skip_reason']}"
            db.session.commit()
            return {
                "upload_id": upload.id,
                "status": "SKIPPED_BLACK_AND_WHITE",
                "is_color": False,
                "color_score": color_eval["color_score"],
                "skip_reason": color_eval["skip_reason"],
                "passengers_detected": 0,
                "message": "Black-and-white footage skipped before AI inference without altering passenger state."
            }

    # Idempotent cleanup: remove prior observations/events if reprocessing this upload
    if upload.observations:
        PassengerObservation.query.filter_by(upload_id=upload.id).delete()
        PresenceEvent.query.filter_by(upload_id=upload.id).delete()
        db.session.flush()

    # 1. Match detected faces against journey identities
    matches = match_faces_to_journey_identities(
        detected_faces=detected_faces,
        journey_id=journey.id
    )

    detected_identity_ids = set()
    annotation_items = []

    # 2. Process each detection
    for match in matches:
        face = match["detection"]
        pid_str = match["person_id"]
        is_new = match["is_new"]
        sim_score = match["similarity_score"]
        conf_label = match["confidence_label"]
        p_obj = match["passenger_identity"]

        if is_new or p_obj is None:
            # Assign first vacant seat in rows 1-6 (1A..6D) if not explicitly set
            existing_occupied = set(
                p.current_seat for p in journey.passengers 
                if p.current_seat and p.status in ["PRESENT", "POSSIBLY_ABSENT"]
            )
            try:
                num = int(''.join(filter(str.isdigit, pid_str)) or "1")
                row = ((num - 1) // 4) + 1
                col = ["A", "B", "C", "D"][(num - 1) % 4]
                candidate_seat = f"{row}{col}"
            except Exception:
                candidate_seat = "1A"

            if candidate_seat in existing_occupied:
                all_seats = [f"{r}{c}" for r in range(1, 7) for c in ["A", "B", "C", "D"]]
                for s in all_seats:
                    if s not in existing_occupied:
                        candidate_seat = s
                        break

            # Create new persistent passenger identity for this journey
            p_obj = PassengerIdentity(
                journey_id=journey.id,
                person_id=pid_str,
                current_seat=candidate_seat,
                first_seen_time=capture_time,
                last_seen_time=capture_time,
                first_seen_upload_id=upload.id,
                last_seen_upload_id=upload.id,
                first_latitude=gps_lat,
                first_longitude=gps_lon,
                last_latitude=gps_lat,
                last_longitude=gps_lon,
                status="PRESENT",
                total_observations=1,
                consecutive_missed_count=0,
                representative_embedding_json=json.dumps(face["embedding"]) if face.get("embedding") else None,
                representative_clothes_json=json.dumps(face.get("clothing_signature")) if face.get("clothing_signature") else None,
                gallery_embeddings_json=json.dumps([face["embedding"]]) if face.get("embedding") else None,
                embedding_model_version="facenet_vggface2_512",
                embedding_sample_count=1,
                thumbnail_path=face.get("crop_path"),
                manual_notes="Initial provisional boarding detection",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
            db.session.add(p_obj)
            db.session.flush() # Populate p_obj.id

            # Log provisional boarding event
            boarding_event = PresenceEvent(
                journey_id=journey.id,
                passenger_identity_id=p_obj.id,
                upload_id=upload.id,
                event_type="BOARDING_PROVISIONAL",
                event_time=capture_time,
                latitude=gps_lat,
                longitude=gps_lon,
                notes=f"Provisional boarding recorded at photo capture ({pid_str}) on seat {candidate_seat}",
                triggered_by_user_id=user_id
            )
            db.session.add(boarding_event)

        else:
            # Existing passenger detected
            p_obj.last_seen_time = capture_time
            p_obj.last_seen_upload_id = upload.id
            if gps_lat is not None and gps_lon is not None:
                p_obj.last_latitude = gps_lat
                p_obj.last_longitude = gps_lon
            p_obj.total_observations += 1
            p_obj.consecutive_missed_count = 0
            
            # Transition back to PRESENT if was POSSIBLY_ABSENT or EXIT_UNCONFIRMED
            prev_status = p_obj.status
            p_obj.status = "PRESENT"
            p_obj.updated_at = datetime.utcnow()

            # Ensure current seat is populated if missing
            if not p_obj.current_seat:
                p_obj.current_seat = p_obj.get_seat_number()

            # Smoothly update representative biometric embedding & face thumbnail
            if face.get("embedding"):
                update_passenger_embedding(p_obj, face["embedding"])
            if face.get("clothing_signature"):
                update_passenger_clothes(p_obj, face["clothing_signature"])
            if face.get("crop_path"):
                p_obj.thumbnail_path = face["crop_path"]

            # Log confirmed presence event
            presence_event = PresenceEvent(
                journey_id=journey.id,
                passenger_identity_id=p_obj.id,
                upload_id=upload.id,
                event_type="PRESENT_CONFIRMED",
                event_time=capture_time,
                latitude=gps_lat,
                longitude=gps_lon,
                notes=f"Confirmed present in photo #{upload.id}. Similarity: {sim_score:.2f} ({conf_label})" + 
                      (f" (recovered from {prev_status})" if prev_status != "PRESENT" else ""),
                triggered_by_user_id=user_id
            )
            db.session.add(presence_event)

        detected_identity_ids.add(p_obj.id)

        # 3. Create immutable observation record
        observation = PassengerObservation(
            upload_id=upload.id,
            journey_id=journey.id,
            passenger_identity_id=p_obj.id,
            bbox_x=face["bbox"][0],
            bbox_y=face["bbox"][1],
            bbox_w=face["bbox"][2],
            bbox_h=face["bbox"][3],
            face_confidence=face["detector_prob"],
            similarity_score=sim_score,
            confidence_label=conf_label,
            quality_score=face["quality"]["quality_score"],
            is_blur=face["quality"]["is_blur"],
            crop_path=face["crop_path"],
            clothing_signature_json=json.dumps(face.get("clothing_signature")) if face.get("clothing_signature") else None,
            face_embedding_json=json.dumps(face["embedding"]) if face.get("embedding") else None,
            embedding_model_version="facenet_vggface2_512",
            observation_time=capture_time,
            created_at=datetime.utcnow()
        )
        db.session.add(observation)

        annotation_items.append({
            "bbox": face["bbox"],
            "person_id": p_obj.person_id,
            "similarity_score": sim_score if not is_new else 0.0,
            "confidence_label": conf_label,
            "is_blur": face["quality"]["is_blur"]
        })

    # 4. Handle missed passengers in this journey (Temporal state machine)
    # Never immediately declare exit!
    all_journey_passengers = PassengerIdentity.query.filter_by(journey_id=journey.id).all()
    missed_limit = Config.MISSED_OBSERVATIONS_FOR_EXIT_UNCONFIRMED

    for p in all_journey_passengers:
        if p.id not in detected_identity_ids and p.status != "EXIT_CONFIRMED":
            p.consecutive_missed_count += 1
            p.updated_at = datetime.utcnow()

            if p.consecutive_missed_count >= missed_limit:
                # Mark as Exit Unconfirmed
                p.status = "EXIT_UNCONFIRMED"
                event = PresenceEvent(
                    journey_id=journey.id,
                    passenger_identity_id=p.id,
                    upload_id=upload.id,
                    event_type="EXIT_UNCONFIRMED",
                    event_time=capture_time,
                    latitude=gps_lat,
                    longitude=gps_lon,
                    notes=f"Passenger not visible for {p.consecutive_missed_count} consecutive photos. Flagged as unconfirmed exit awaiting operator review.",
                    triggered_by_user_id=user_id
                )
                db.session.add(event)
            else:
                # Mark as Possibly Absent / Temporarily Occluded
                p.status = "POSSIBLY_ABSENT"
                event = PresenceEvent(
                    journey_id=journey.id,
                    passenger_identity_id=p.id,
                    upload_id=upload.id,
                    event_type="POSSIBLY_ABSENT",
                    event_time=capture_time,
                    latitude=gps_lat,
                    longitude=gps_lon,
                    notes=f"Not detected in photo #{upload.id} (missed count = {p.consecutive_missed_count}). Passenger may be turned away, occluded, or temporarily moving.",
                    triggered_by_user_id=user_id
                )
                db.session.add(event)

    # 5. Render HUD annotated image
    try:
        annotated_filename = f"annotated_{upload.id}.jpg"
        annotated_abs_path = Config.UPLOAD_FOLDER / annotated_filename
        render_annotated_image(upload.file_path, annotation_items, annotated_abs_path)
        upload.annotated_path = f"uploads/{annotated_filename}"
    except Exception as e:
        print(f"[JourneyService] Annotation rendering warning: {e}")

    # 6. Finalize upload record
    upload.status = "processed"
    upload.passengers_detected_count = len(detected_faces)
    upload.processing_notes = f"Detected {len(detected_faces)} visible passengers ({len(matches)} processed)."
    
    db.session.commit()

    # Log audit
    log_audit(
        action="PROCESS_BUS_PHOTO",
        entity_type="Upload",
        entity_id=upload.id,
        details={
            "journey_id": journey.id,
            "passengers_detected": len(detected_faces),
            "capture_timestamp": capture_time.isoformat(),
            "gps": {"lat": gps_lat, "lon": gps_lon}
        },
        user_id=user_id
    )

    return {
        "upload_id": upload.id,
        "passengers_detected": len(detected_faces),
        "annotated_path": upload.annotated_path,
        "matches": [
            {
                "person_id": m["person_id"],
                "similarity_score": round(m["similarity_score"], 3),
                "confidence_label": m["confidence_label"],
                "bbox": m["detection"]["bbox"],
                "crop_path": m["detection"]["crop_path"]
            }
            for m in matches
        ]
    }


def update_manual_passenger_status(passenger_id, new_status, comment, reviewer_id):
    """
    Operator manual review action to confirm exit, verify presence, or correct records.
    """
    p = PassengerIdentity.query.get(passenger_id)
    if not p:
        raise ValueError("Passenger record not found.")

    prev_status = p.status
    p.status = new_status
    p.manual_notes = comment
    p.confirmed_by_id = reviewer_id
    p.confirmed_at = datetime.utcnow()
    p.updated_at = datetime.utcnow()

    # Record review entry
    review = ManualReview(
        journey_id=p.journey_id,
        passenger_identity_id=p.id,
        reviewer_id=reviewer_id,
        review_type="CONFIRM_EXIT" if new_status == "EXIT_CONFIRMED" else "MANUAL_STATUS_CHANGE",
        previous_status=prev_status,
        new_status=new_status,
        comment=comment,
        reviewed_at=datetime.utcnow()
    )
    db.session.add(review)

    # Record event in presence timeline
    event = PresenceEvent(
        journey_id=p.journey_id,
        passenger_identity_id=p.id,
        event_type="EXIT_CONFIRMED" if new_status == "EXIT_CONFIRMED" else "MANUAL_OVERRIDE",
        event_time=datetime.utcnow(),
        latitude=p.last_latitude,
        longitude=p.last_longitude,
        notes=f"Manual status change from '{prev_status}' to '{new_status}': {comment}",
        triggered_by_user_id=reviewer_id
    )
    db.session.add(event)
    db.session.commit()

    log_audit(
        action="MANUAL_PASSENGER_REVIEW",
        entity_type="PassengerIdentity",
        entity_id=p.id,
        details={
            "person_id": p.person_id,
            "previous_status": prev_status,
            "new_status": new_status,
            "comment": comment
        },
        user_id=reviewer_id
    )

    return p


def process_multi_angle_batch(journey_id, batch_id, angle_uploads, user_id=None, stop_id=None):
    """
    Main multi-angle processing orchestrator for Angles A, B, C (or any submitted subset):
    1. Analyzes each submitted angle photo independently with detect_persons_and_faces
    2. Respects individual capture timestamps and EXIF GPS per angle
    3. Matches clear face embeddings to journey-scoped identities (P001, P002, etc.)
    4. Evaluates intra-batch cross-angle duplicates (same passenger seen from multiple angles)
    5. Cautiously flags uncertain cross-angle duplicates for review without forcing identity merges
    6. Creates immutable PassengerObservation records for all visible human bodies
    7. Updates presence state machine based on combined multi-angle evidence (never marks exit based on single angle)
    8. Renders sleek HUD annotated preview images for each angle
    9. Calculates consolidated per-angle counts and provisional unique passenger count
    10. Gracefully handles single angle processing failures without discarding other angles
    """
    journey = Journey.query.get(journey_id)
    if not journey:
        raise ValueError(f"Journey #{journey_id} not found.")

    bus = journey.bus
    angle_summaries = {}
    successful_angle_detections = {} # angle -> list of detection dicts
    all_submitted_angles = ["A", "B", "C"]

    # Resolve stop details if stop_id was provided
    timetable_stop = None
    current_stop_num = 1
    current_stop_name = "Assigned Stop"
    if stop_id:
        from app.models import TimetableStop, RouteStop
        timetable_stop = TimetableStop.query.get(stop_id)
        if not timetable_stop:
            timetable_stop = RouteStop.query.get(stop_id)
        if timetable_stop:
            current_stop_num = getattr(timetable_stop, "stop_number", getattr(timetable_stop, "stop_order", 1))
            current_stop_name = getattr(timetable_stop, "stop_name", f"Stop #{current_stop_num}")
    
    # 1. Process each submitted angle independently
    for angle in all_submitted_angles:
        upload = angle_uploads.get(angle)
        if not upload:
            continue

        # Idempotent cleanup: remove prior observations/events if reprocessing this upload
        if upload.observations:
            PassengerObservation.query.filter_by(upload_id=upload.id).delete()
            PresenceEvent.query.filter_by(upload_id=upload.id).delete()
            db.session.flush()

        upload.batch_id = batch_id
        upload.camera_angle = angle
        if bus:
            upload.bus_id = bus.id
        if stop_id and not upload.route_stop_id:
            upload.route_stop_id = stop_id

        # Fetch seating area configuration for this angle
        cfg = CameraAngleConfig.query.filter_by(camera_angle=angle).first()
        roi = cfg.to_dict()["roi"] if cfg else None
        seating_label = cfg.seating_area_label if cfg else f"Section {angle}"

        try:
            import os
            if not upload.file_path or not os.path.exists(upload.file_path):
                raise FileNotFoundError(f"Media file not found: {upload.file_path}")

            # Color eligibility evaluation: Automatically skip black-and-white footage before AI processing
            from app.ai.color_filter import evaluate_media_color_eligibility
            color_eval = evaluate_media_color_eligibility(upload.file_path)
            upload.is_color = color_eval["is_color"]
            upload.color_score = color_eval["color_score"]
            upload.color_classification_details_json = json.dumps(color_eval.get("details", {}))

            if not color_eval["is_color"]:
                upload.status = "SKIPPED_BLACK_AND_WHITE"
                upload.skip_reason = color_eval["skip_reason"]
                upload.processing_notes = f"Skipped: {color_eval['skip_reason']}"
                db.session.commit()
                angle_summaries[angle] = {
                    "upload_id": upload.id,
                    "status": "SKIPPED_BLACK_AND_WHITE",
                    "camera_angle": angle,
                    "title": f"Angle {angle} — {seating_label} (Skipped B&W)",
                    "seating_label": seating_label,
                    "is_color": False,
                    "color_score": color_eval["color_score"],
                    "skip_reason": color_eval["skip_reason"],
                    "detected": 0,
                    "high_confidence": 0,
                    "needs_review": 0,
                    "uncertain": 0,
                    "annotated_url": None,
                    "warnings": [color_eval["skip_reason"]],
                    "detections": []
                }
                continue

            # Person detection (YOLO body detection + FaceNet recognition)
            det_result = detect_persons_and_faces(
                upload.file_path,
                camera_angle=angle,
                roi_config=roi
            )
            detections = det_result["detections"]
            successful_angle_detections[angle] = {
                "upload": upload,
                "detections": detections,
                "warnings": det_result["warnings"],
                "raw_count": det_result["raw_count"],
                "high_conf_count": det_result["high_confidence_count"],
                "needs_review_count": det_result["needs_review_count"],
                "uncertain_count": det_result["uncertain_count"],
                "seating_label": seating_label
            }
        except Exception as e:
            print(f"[MultiAngle] Error processing Angle {angle}: {e}")
            upload.status = "failed"
            upload.error_details = str(e)
            upload.processing_notes = f"AI Processing Error for Angle {angle}: {str(e)}"
            db.session.commit()
            angle_summaries[angle] = {
                "status": "failed",
                "error": str(e),
                "detected": 0,
                "high_confidence": 0,
                "needs_review": 0,
                "uncertain": 0,
                "annotated_url": None,
                "warnings": [f"Processing failed: {str(e)}"]
            }

    # 2. Multi-Modal Passenger Re-Identification across Stops & Angles
    # Matches existing passengers using combined FaceNet embedding + Spatial Bbox / Seat + Appearance Color Profile
    allocated_batch_pids = set()
    detected_identity_ids = set()
    already_matched_pids = set()
    in_progress_profiles = []

    for angle, data in successful_angle_detections.items():
        data["detections"] = match_angle_detections_to_passengers(
            detections=data["detections"],
            camera_angle=angle,
            journey_id=journey.id,
            allocated_pids=allocated_batch_pids,
            already_matched_pids=already_matched_pids,
            in_progress_profiles=in_progress_profiles
        )
        for det in data["detections"]:
            if det.get("person_id") and det.get("embedding"):
                in_progress_profiles.append({
                    "person_id": det["person_id"],
                    "embedding": det["embedding"],
                    "clothing_signature": det.get("clothing_signature"),
                    "crop_path": det.get("crop_path"),
                    "camera_angle": angle,
                    "passenger_identity": det.get("passenger_identity")
                })

    # Cross-angle duplicate unification within the same batch using Connected Component Clustering (DSU)
    # Angles A, B, C provide complementary views of the bus cabin.
    # Detections across angles that represent the same passenger (face similarity >= 0.58,
    # or supported by face >= 0.52 + clothing >= 0.65) are unified into the same passenger identity.
    # Invariant: A cluster may contain AT MOST ONE detection per camera angle.
    angle_keys = [k for k in all_submitted_angles if k in successful_angle_detections]

    all_nodes = []
    for ang in angle_keys:
        for idx in range(len(successful_angle_detections[ang]["detections"])):
            all_nodes.append((ang, idx))

    dsu_parent = {node: node for node in all_nodes}
    dsu_rank = {node: 0 for node in all_nodes}
    comp_angles = {node: {node[0]} for node in all_nodes}
    comp_members = {node: [node] for node in all_nodes}

    def dsu_find(n):
        if dsu_parent[n] != n:
            dsu_parent[n] = dsu_find(dsu_parent[n])
        return dsu_parent[n]

    def dsu_union(n1, n2):
        r1 = dsu_find(n1)
        r2 = dsu_find(n2)
        if r1 == r2:
            return True
        # Invariant: cannot merge if the components share any camera angle
        if comp_angles[r1].intersection(comp_angles[r2]):
            return False

        # Invariant: cannot merge if both components already have distinct DB passenger identities
        p1 = next((successful_angle_detections[a]["detections"][i].get("passenger_identity") for a, i in comp_members[r1] if successful_angle_detections[a]["detections"][i].get("passenger_identity")), None)
        p2 = next((successful_angle_detections[a]["detections"][i].get("passenger_identity") for a, i in comp_members[r2] if successful_angle_detections[a]["detections"][i].get("passenger_identity")), None)
        if p1 and p2 and p1.id != p2.id:
            return False

        # Invariant: STRICT BIOMETRIC CONSISTENCY.
        # If any member in component 1 has a face embedding, and any member in component 2 has a face embedding,
        # they MUST NOT have a biometric conflict (< 0.52).
        for a1, i1 in comp_members[r1]:
            e1 = successful_angle_detections[a1]["detections"][i1].get("embedding")
            if e1 is None:
                continue
            e1_arr = np.array(e1, dtype=np.float32)
            for a2, i2 in comp_members[r2]:
                e2 = successful_angle_detections[a2]["detections"][i2].get("embedding")
                if e2 is None:
                    continue
                e2_arr = np.array(e2, dtype=np.float32)
                sim = cosine_similarity(e1_arr, e2_arr)
                if sim < 0.52:
                    return False

        # Invariant: If either component links to an existing DB passenger, verify against that passenger's anchor face
        target_db_p = p1 or p2
        if target_db_p:
            other_members = comp_members[r2 if p1 else r1]
            for a_o, i_o in other_members:
                e_o = successful_angle_detections[a_o]["detections"][i_o].get("embedding")
                if e_o is not None:
                    has_conflict, _, _ = check_biometric_identity_conflict(e_o, target_db_p)
                    if has_conflict:
                        return False

        if dsu_rank[r1] < dsu_rank[r2]:
            r1, r2 = r2, r1
        dsu_parent[r2] = r1
        comp_angles[r1].update(comp_angles[r2])
        comp_members[r1].extend(comp_members[r2])
        if dsu_rank[r1] == dsu_rank[r2]:
            dsu_rank[r1] += 1
        return True

    # Find candidate cross-angle pairs
    cross_candidates = []
    for i in range(len(angle_keys)):
        for j in range(i + 1, len(angle_keys)):
            ang1 = angle_keys[i]
            ang2 = angle_keys[j]

            dets1 = successful_angle_detections[ang1]["detections"]
            dets2 = successful_angle_detections[ang2]["detections"]

            for idx1, det1 in enumerate(dets1):
                emb1 = np.array(det1["embedding"], dtype=np.float32) if det1.get("embedding") is not None else None
                cs1 = det1.get("clothing_signature")

                for idx2, det2 in enumerate(dets2):
                    emb2 = np.array(det2["embedding"], dtype=np.float32) if det2.get("embedding") is not None else None
                    cs2 = det2.get("clothing_signature")

                    f_sim = cosine_similarity(emb1, emb2) if (emb1 is not None and emb2 is not None) else 0.0
                    c_sim = cosine_similarity(cs1, cs2) if (cs1 is not None and cs2 is not None) else 0.0

                    # Unified cross-angle matching criteria
                    is_match = False
                    score = 0.0
                    if emb1 is not None and emb2 is not None:
                        if f_sim >= 0.65:
                            score = 0.85 * f_sim + 0.15 * max(0.0, c_sim)
                            is_match = True
                        elif f_sim >= 0.58 and c_sim >= 0.45:
                            score = 0.70 * f_sim + 0.30 * c_sim
                            is_match = True
                        elif f_sim >= 0.52 and c_sim >= 0.70:
                            score = 0.60 * f_sim + 0.40 * c_sim
                            is_match = True

                    if is_match and score >= 0.58:
                        cross_candidates.append((score, (ang1, idx1), (ang2, idx2)))

    # Sort descending by similarity score
    cross_candidates.sort(key=lambda x: x[0], reverse=True)

    for score, n1, n2 in cross_candidates:
        dsu_union(n1, n2)

    # Resolve unified identities per connected component
    unique_roots = set(dsu_find(node) for node in all_nodes)
    for root in unique_roots:
        members = comp_members[root]
        # Check if any member has an existing DB identity
        target_p = None
        for a, i in members:
            p = successful_angle_detections[a]["detections"][i].get("passenger_identity")
            if p:
                target_p = p
                break

        if target_p:
            target_pid = target_p.person_id
        else:
            # Single unified provisional ID for the cluster
            target_pid = min(successful_angle_detections[a]["detections"][i]["person_id"] for a, i in members)
            # Free unused provisional IDs allocated during independent angle pass
            for a, i in members:
                m_pid = successful_angle_detections[a]["detections"][i]["person_id"]
                if m_pid != target_pid and m_pid in allocated_batch_pids:
                    allocated_batch_pids.remove(m_pid)

        for a, i in members:
            det = successful_angle_detections[a]["detections"][i]
            det["person_id"] = target_pid
            det["passenger_identity"] = target_p
            det["is_cross_merged"] = (len(members) > 1)

    # 1-to-1 constraint within each individual camera angle
    # Across different angles, the SAME passenger ID is preserved and links to the same identity.
    for angle, data in successful_angle_detections.items():
        seen_angle_pids = set()
        for det in data["detections"]:
            pid = det.get("person_id")
            if not pid or pid in seen_angle_pids:
                # Intra-angle duplicate collision: allocate a fresh unique person ID for this detection
                new_pid = generate_next_person_id(journey.id, allocated_ids=allocated_batch_pids)
                allocated_batch_pids.add(new_pid)
                det["person_id"] = new_pid
                det["passenger_identity"] = None
                det["is_cross_merged"] = False
                pid = new_pid
            seen_angle_pids.add(pid)

    # 3. Create or update PassengerIdentity records for all detections
    existing_occupied_seats = set(
        p.current_seat for p in journey.passengers 
        if p.current_seat and p.status in ["PRESENT", "POSSIBLY_ABSENT"]
    )
    all_bus_seats = [f"{r}{c}" for r in range(1, 11) for c in ["A", "B", "C", "D"]]

    created_identities_map = {} # pid_str -> PassengerIdentity obj
    logged_presence_stop_pids = set() # Guarantees at most 1 presence milestone per passenger per stop

    for angle, data in successful_angle_detections.items():
        upload = data["upload"]
        cap_time = upload.capture_timestamp or datetime.utcnow()
        lat = upload.latitude
        lon = upload.longitude

        for det in data["detections"]:
            pid_str = det.get("person_id")
            if not pid_str:
                continue

            p_obj = det.get("passenger_identity")
            if p_obj:
                # Update existing passenger with stop progression
                dep_time = None
                if timetable_stop:
                    dep_time = getattr(timetable_stop, "actual_departure", None) or getattr(timetable_stop, "scheduled_departure", None) or getattr(timetable_stop, "window_end", None)
                latest_time = dep_time or cap_time

                p_obj.last_seen_time = latest_time
                p_obj.last_seen_upload_id = upload.id
                if lat is not None and lon is not None:
                    p_obj.last_latitude = lat
                    p_obj.last_longitude = lon

                # Update camera angle tracking
                if not p_obj.first_observed_angle:
                    p_obj.first_observed_angle = angle
                p_obj.last_observed_angle = angle
                det_angles = p_obj.get_detected_angles()
                if angle not in det_angles:
                    det_angles.append(angle)
                    p_obj.detected_angles_json = json.dumps(det_angles)

                # Update observed interval duration
                if p_obj.first_seen_time and p_obj.last_seen_time:
                    diff_sec = (p_obj.last_seen_time - p_obj.first_seen_time).total_seconds()
                    p_obj.observed_interval_seconds = max(0.0, diff_sec)

                # Update stop-wise exit location and stops traveled
                p_obj.exit_stop_id = timetable_stop.id if timetable_stop else upload.route_stop_id
                p_obj.exit_stop_name = current_stop_name
                p_obj.exit_stop_number = current_stop_num
                b_num = p_obj.boarding_stop_number or current_stop_num

                try:
                    t_list = json.loads(p_obj.traveled_stops_json) if p_obj.traveled_stops_json else []
                except Exception:
                    t_list = []
                entry = next((ts for ts in t_list if ts.get("stop_number") == current_stop_num), None)
                if entry:
                    e_angles = entry.get("angles", [])
                    if angle not in e_angles:
                        e_angles.append(angle)
                        entry["angles"] = e_angles
                else:
                    t_list.append({
                        "stop_number": current_stop_num,
                        "stop_name": current_stop_name,
                        "time": latest_time.strftime("%Y-%m-%d %H:%M:%S"),
                        "angles": [angle]
                    })
                p_obj.traveled_stops_json = json.dumps(t_list)
                p_obj.stops_traveled = max(len(t_list), current_stop_num - b_num + 1)

                p_obj.total_observations += 1
                p_obj.consecutive_missed_count = 0
                prev_status = p_obj.status
                p_obj.status = "PRESENT"
                p_obj.updated_at = datetime.utcnow()
                if upload.route_stop_id and not p_obj.boarding_stop_id:
                    p_obj.boarding_stop_id = upload.route_stop_id
                    p_obj.boarding_stop_name = current_stop_name
                    p_obj.boarding_stop_number = current_stop_num

                if det.get("confidence_label") == "Needs Review" or det.get("is_potential_duplicate", False):
                    p_obj.uncertain_match = True

                if det.get("embedding"):
                    update_passenger_gallery_and_embedding(p_obj, det["embedding"])
                if det.get("clothing_signature"):
                    update_passenger_clothes(p_obj, det["clothing_signature"])
                # Preserve best quality frontal face thumbnail; don't overwrite with occluded/faceless crop
                if det.get("crop_path"):
                    if not p_obj.thumbnail_path:
                        p_obj.thumbnail_path = det["crop_path"]
                    elif det.get("embedding") is not None and det.get("detection_confidence", 0) >= 0.75:
                        p_obj.thumbnail_path = det["crop_path"]

                # Log presence event: at most 1 presence milestone per stop for each passenger!
                stop_already_logged = (p_obj.id in logged_presence_stop_pids)
                if not stop_already_logged and upload.route_stop_id:
                    stop_already_logged = PresenceEvent.query.filter_by(
                        passenger_identity_id=p_obj.id,
                        route_stop_id=upload.route_stop_id
                    ).first() is not None

                if not stop_already_logged:
                    logged_presence_stop_pids.add(p_obj.id)
                    ev = PresenceEvent(
                        journey_id=journey.id,
                        passenger_identity_id=p_obj.id,
                        upload_id=upload.id,
                        route_stop_id=upload.route_stop_id,
                        event_type="PRESENT_CONFIRMED",
                        event_time=latest_time,
                        latitude=lat,
                        longitude=lon,
                        notes=f"In Transit: Stop #{current_stop_num} ({current_stop_name}) [Seat {p_obj.get_seat_number()}] (Traveled {p_obj.stops_traveled} stops)" + (f" (recovered from {prev_status})" if prev_status != "PRESENT" else ""),
                        triggered_by_user_id=user_id
                    )
                    db.session.add(ev)
                detected_identity_ids.add(p_obj.id)
            elif pid_str in created_identities_map:
                # Re-use newly created identity from this batch (cross-angle detection of same newly boarded passenger)
                p_obj = created_identities_map[pid_str]
                det["passenger_identity"] = p_obj
                p_obj.total_observations += 1
                p_obj.last_seen_time = max(p_obj.last_seen_time, cap_time)
                if p_obj.first_seen_time and p_obj.last_seen_time:
                    diff_sec = (p_obj.last_seen_time - p_obj.first_seen_time).total_seconds()
                    p_obj.observed_interval_seconds = max(0.0, diff_sec)
                p_obj.last_observed_angle = angle
                det_angles = p_obj.get_detected_angles()
                if angle not in det_angles:
                    det_angles.append(angle)
                    p_obj.detected_angles_json = json.dumps(det_angles)
                if det.get("confidence_label") == "Needs Review" or det.get("is_potential_duplicate", False):
                    p_obj.uncertain_match = True
                if det.get("embedding"):
                    update_passenger_gallery_and_embedding(p_obj, det["embedding"])
                if det.get("clothing_signature"):
                    update_passenger_clothes(p_obj, det["clothing_signature"])
                if det.get("crop_path"):
                    if not p_obj.thumbnail_path:
                        p_obj.thumbnail_path = det["crop_path"]
                    elif det.get("embedding") is not None and det.get("detection_confidence", 0) >= 0.75:
                        p_obj.thumbnail_path = det["crop_path"]
                try:
                    t_list = json.loads(p_obj.traveled_stops_json) if p_obj.traveled_stops_json else []
                    entry = next((ts for ts in t_list if ts.get("stop_number") == current_stop_num), None)
                    if entry:
                        e_angles = entry.get("angles", [])
                        if angle not in e_angles:
                            e_angles.append(angle)
                            entry["angles"] = e_angles
                        p_obj.traveled_stops_json = json.dumps(t_list)
                except Exception:
                    pass
                detected_identity_ids.add(p_obj.id)
            else:
                # Assign seat based on camera angle zone (A: front rows 1-2, B: mid rows 3-4, C: rear rows 5-8)
                preferred_rows = [1, 2] if angle == "A" else ([3, 4] if angle == "B" else [5, 6, 7, 8])
                assigned_seat = None
                for r in preferred_rows:
                    for c in ["A", "B", "C", "D"]:
                        cand = f"{r}{c}"
                        if cand not in existing_occupied_seats:
                            assigned_seat = cand
                            break
                    if assigned_seat:
                        break

                if not assigned_seat:
                    for s in all_bus_seats:
                        if s not in existing_occupied_seats:
                            assigned_seat = s
                            break

                if assigned_seat:
                    existing_occupied_seats.add(assigned_seat)

                arr_time = None
                if timetable_stop:
                    arr_time = getattr(timetable_stop, "actual_arrival", None) or getattr(timetable_stop, "scheduled_arrival", None) or getattr(timetable_stop, "window_start", None)
                first_time = arr_time or cap_time

                new_p = PassengerIdentity(
                    journey_id=journey.id,
                    person_id=pid_str,
                    current_seat=assigned_seat or "1A",
                    boarding_stop_id=timetable_stop.id if timetable_stop else upload.route_stop_id,
                    boarding_stop_name=current_stop_name,
                    boarding_stop_number=current_stop_num,
                    exit_stop_id=timetable_stop.id if timetable_stop else upload.route_stop_id,
                    exit_stop_name=current_stop_name,
                    exit_stop_number=current_stop_num,
                    first_observed_angle=angle,
                    last_observed_angle=angle,
                    detected_angles_json=json.dumps([angle]),
                    observed_interval_seconds=0.0,
                    uncertain_match=(det.get("confidence_label") == "Needs Review" or det.get("is_potential_duplicate", False)),
                    stops_traveled=1,
                    traveled_stops_json=json.dumps([{
                        "stop_number": current_stop_num,
                        "stop_name": current_stop_name,
                        "time": first_time.strftime("%Y-%m-%d %H:%M:%S"),
                        "angles": [angle]
                    }]),
                    first_seen_time=first_time,
                    last_seen_time=first_time,
                    first_seen_upload_id=upload.id,
                    last_seen_upload_id=upload.id,
                    first_latitude=lat,
                    first_longitude=lon,
                    last_latitude=lat,
                    last_longitude=lon,
                    status="PRESENT",
                    total_observations=1,
                    consecutive_missed_count=0,
                    representative_embedding_json=json.dumps(det["embedding"]) if det.get("embedding") else None,
                    representative_clothes_json=json.dumps(det.get("clothing_signature")) if det.get("clothing_signature") else None,
                    gallery_embeddings_json=json.dumps([det["embedding"]]) if det.get("embedding") else None,
                    embedding_model_version="facenet_vggface2_512",
                    embedding_sample_count=1,
                    thumbnail_path=det.get("crop_path"),
                    manual_notes=f"Observation-based boarding estimate at Stop #{current_stop_num} ({current_stop_name}) from Angle {angle}",
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow()
                )
                db.session.add(new_p)
                db.session.flush()

                created_identities_map[pid_str] = new_p
                det["passenger_identity"] = new_p
                detected_identity_ids.add(new_p.id)

                # Boarding event
                b_ev = PresenceEvent(
                    journey_id=journey.id,
                    passenger_identity_id=new_p.id,
                    upload_id=upload.id,
                    route_stop_id=upload.route_stop_id,
                    event_type="BOARDING_PROVISIONAL",
                    event_time=first_time,
                    latitude=lat,
                    longitude=lon,
                    notes=f"Boarded at Stop #{current_stop_num} ({current_stop_name}) from Angle {angle} on seat {assigned_seat or '1A'}",
                    triggered_by_user_id=user_id
                )
                db.session.add(b_ev)

    # 4. Cross-Angle Duplicate Detection
    potential_duplicate_count = 0

    # 5. Save PassengerObservation records and render HUD annotated images
    total_raw_detections = 0
    total_uncertain = 0

    for angle, data in successful_angle_detections.items():
        upload = data["upload"]
        cap_time = upload.capture_timestamp or datetime.utcnow()
        detections = data["detections"]
        total_raw_detections += len(detections)

        hud_items = []
        angle_uncertain = 0

        for det in detections:
            p_obj = det.get("passenger_identity")
            conf_label = det.get("confidence_label", "Medium")
            is_potential_dup = det.get("is_potential_duplicate", False)
            dup_status = det.get("duplicate_review_status")
            review_status = det.get("review_status", "CONFIRMED")
            if is_potential_dup or not p_obj or conf_label == "Needs Review":
                angle_uncertain += 1
                total_uncertain += 1

            obs = PassengerObservation(
                upload_id=upload.id,
                journey_id=journey.id,
                passenger_identity_id=p_obj.id if p_obj else None,
                route_stop_id=upload.route_stop_id,
                camera_angle=angle,
                temp_detection_id=det.get("temp_id") if not p_obj else None,
                bbox_x=det["bbox"][0],
                bbox_y=det["bbox"][1],
                bbox_w=det["bbox"][2],
                bbox_h=det["bbox"][3],
                person_bbox_json=json.dumps(det.get("person_bbox")),
                detection_confidence=det.get("detection_confidence", 0.0),
                face_confidence=det.get("face_confidence", 0.0),
                similarity_score=det.get("similarity_score", 0.0),
                confidence_label=conf_label,
                quality_score=det["quality"]["quality_score"],
                is_blur=det["quality"]["is_blur"],
                review_status=review_status,
                is_potential_duplicate=is_potential_dup,
                duplicate_review_status=dup_status,
                crop_path=det.get("crop_path"),
                clothing_signature_json=json.dumps(det.get("clothing_signature")) if det.get("clothing_signature") else None,
                face_embedding_json=json.dumps(det["embedding"]) if det.get("embedding") else None,
                embedding_model_version="facenet_vggface2_512",
                observation_time=cap_time,
                created_at=datetime.utcnow()
            )
            db.session.add(obs)

            hud_items.append({
                "bbox": det["bbox"],
                "person_bbox": det.get("person_bbox"),
                "face_bbox": det.get("face_bbox"),
                "person_id": p_obj.person_id if p_obj else None,
                "temp_id": det.get("temp_id"),
                "similarity_score": det.get("similarity_score", 0.0),
                "detection_confidence": det.get("detection_confidence", 0.0),
                "confidence_label": conf_label,
                "review_status": review_status,
                "is_potential_duplicate": is_potential_dup
            })

        # Render HUD annotated image
        annotated_filename = f"annotated_batch_{batch_id}_{angle}_{upload.id}.jpg"
        annotated_abs_path = Config.UPLOAD_FOLDER / annotated_filename
        try:
            render_multi_angle_annotated_hud(
                upload.file_path,
                hud_items,
                annotated_abs_path,
                camera_angle=angle,
                seating_label=data["seating_label"]
            )
            upload.annotated_path = f"uploads/{annotated_filename}"
        except Exception as e:
            print(f"[MultiAngle] HUD rendering warning for Angle {angle}: {e}")

        upload.status = "processed"
        upload.passengers_detected_count = len(detections)
        upload.uncertain_detections_count = angle_uncertain
        notes_prefix = f"{upload.processing_notes} | " if (upload.processing_notes and "Identical photo" in upload.processing_notes) else ""
        upload.processing_notes = f"{notes_prefix}Detected {len(detections)} visible passengers in Angle {angle} ({data['high_conf_count']} high-confidence, {angle_uncertain} uncertain/review)."

        angle_summaries[angle] = {
            "upload_id": upload.id,
            "status": "processed",
            "camera_angle": angle,
            "title": f"Angle {angle} — {data['seating_label']}",
            "seating_label": data["seating_label"],
            "detected": len(detections),
            "high_confidence": data["high_conf_count"],
            "needs_review": data["needs_review_count"],
            "uncertain": angle_uncertain,
            "annotated_url": f"/data/{upload.annotated_path}" if upload.annotated_path else None,
            "warnings": data["warnings"],
            "capture_timestamp": upload.capture_timestamp.strftime("%Y-%m-%d %H:%M:%S") if upload.capture_timestamp else None,
            "detections": [
                {
                    "temp_id": d.get("temp_id"),
                    "person_id": d["passenger_identity"].person_id if d.get("passenger_identity") else "Unidentified",
                    "confidence_label": d.get("confidence_label"),
                    "detection_confidence": d.get("detection_confidence"),
                    "similarity_score": d.get("similarity_score", 0.0),
                    "crop_path": d.get("crop_path"),
                    "bbox": d["bbox"],
                    "review_status": d.get("review_status"),
                    "is_potential_duplicate": d.get("is_potential_duplicate", False)
                }
                for d in detections
            ]
        }

    # 6. Update presence state machine for absent passengers
    # COMBINED EVIDENCE: Do NOT mark absent based on absence in one angle!
    # CRITICAL INVARIANT: If NO eligible color angles were processed in this batch
    # (e.g. all submitted angles were skipped as B&W), DO NOT update passenger absence state!
    all_journey_passengers = PassengerIdentity.query.filter_by(journey_id=journey.id).all()
    if successful_angle_detections:
        missed_limit = Config.MISSED_OBSERVATIONS_FOR_EXIT_UNCONFIRMED
        now_ts = datetime.utcnow()

        for p in all_journey_passengers:
            if p.id not in detected_identity_ids and p.status != "EXIT_CONFIRMED":
                p.consecutive_missed_count += 1
                p.updated_at = now_ts
                if p.consecutive_missed_count >= missed_limit:
                    p.status = "EXIT_CONFIRMED"
                    # Exit time is departure of the LAST STOP they were actually seen!
                    exit_time = p.last_seen_time or now_ts
                    ev = PresenceEvent(
                        journey_id=journey.id,
                        passenger_identity_id=p.id,
                        event_type="EXIT_CONFIRMED",
                        event_time=exit_time,
                        notes=f"Alighted after Stop #{p.exit_stop_number or current_stop_num} ({p.exit_stop_name or current_stop_name}). Total stops traveled: {p.stops_traveled or 1}.",
                        triggered_by_user_id=user_id
                    )
                    db.session.add(ev)
                else:
                    p.status = "POSSIBLY_ABSENT"

    # 7. Finalize counts and provisional calculations
    confirmed_unique = len(detected_identity_ids)
    provisional_unique = confirmed_unique + total_uncertain
    db.session.commit()

    # Sync updated journey, uploads, and passengers to MongoDB Atlas
    try:
        from app.mongodb import sync_document_to_mongo
        sync_document_to_mongo("journeys", journey.id, journey.to_dict())
        for angle, data in successful_angle_detections.items():
            u = data["upload"]
            sync_document_to_mongo("uploads", u.id, u.to_dict())
        for p in all_journey_passengers:
            sync_document_to_mongo("passenger_identities", p.id, p.to_dict())
    except Exception as e:
        print(f"[MongoDB Sync Warning] {e}")

    # Log audit
    log_audit(
        action="PROCESS_MULTI_ANGLE_UPLOAD",
        entity_type="Journey",
        entity_id=journey.id,
        details={
            "batch_id": batch_id,
            "angles_submitted": list(successful_angle_detections.keys()),
            "total_raw_detections": total_raw_detections,
            "confirmed_unique": confirmed_unique,
            "provisional_unique_count": provisional_unique,
            "potential_duplicates": potential_duplicate_count,
            "uncertain_or_unidentified": total_uncertain
        },
        user_id=user_id
    )

    is_all_skipped_bw = len(successful_angle_detections) == 0 and any(s.get("status") == "SKIPPED_BLACK_AND_WHITE" for s in angle_summaries.values())

    return {
        "success": True,
        "batch_id": batch_id,
        "journey_id": journey.id,
        "status": "SKIPPED_BLACK_AND_WHITE" if is_all_skipped_bw else "COMPLETED",
        "is_all_skipped_bw": is_all_skipped_bw,
        "angle_summaries": angle_summaries,
        "consolidated_counts": {
            "angle_a_count": angle_summaries.get("A", {}).get("detected", 0),
            "angle_b_count": angle_summaries.get("B", {}).get("detected", 0),
            "angle_c_count": angle_summaries.get("C", {}).get("detected", 0),
            "total_raw_detections": total_raw_detections,
            "potential_duplicates": potential_duplicate_count,
            "confirmed_unique": confirmed_unique,
            "uncertain_or_unidentified": total_uncertain,
            "skipped_black_and_white_count": sum(1 for s in angle_summaries.values() if s.get("status") == "SKIPPED_BLACK_AND_WHITE"),
            "passengers_requiring_review": sum(s.get("needs_review", 0) for s in angle_summaries.values()),
            "provisional_unique_count": provisional_unique,
            "is_provisional": (
                total_uncertain > 0 or 
                potential_duplicate_count > 0 or 
                len(successful_angle_detections) < 3 or 
                sum(s.get("needs_review", 0) for s in angle_summaries.values()) > 0
            )
        }
    }


def review_observation(observation_id, action, target_person_id=None, comment=None, reviewer_id=None):
    """
    Operator manual review for an individual detection observation:
    action:
      - 'CONFIRM': confirm detection as valid passenger
      - 'FALSE_POSITIVE': mark detection as erroneous (false positive)
      - 'LINK_PASSENGER': link to existing passenger (e.g. 'P001')
      - 'REQUEST_REVIEW': flag detection for supervisor review
    """
    obs = PassengerObservation.query.get(observation_id)
    if not obs:
        raise ValueError(f"Observation #{observation_id} not found.")

    prev_status = obs.review_status

    if action == "CONFIRM":
        obs.review_status = "CONFIRMED"
        obs.is_potential_duplicate = False
        obs.duplicate_review_status = "RESOLVED_UNIQUE"
    elif action == "FALSE_POSITIVE":
        obs.review_status = "FALSE_POSITIVE"
        obs.is_potential_duplicate = False
        obs.duplicate_review_status = "RESOLVED_REJECTED"
    elif action == "LINK_PASSENGER":
        if not target_person_id:
            raise ValueError("Target Person ID is required to link observation.")
        p = PassengerIdentity.query.filter_by(journey_id=obs.journey_id, person_id=target_person_id).first()
        if not p:
            raise ValueError(f"Passenger {target_person_id} not found in this journey.")
        obs.passenger_identity_id = p.id
        obs.review_status = "CONFIRMED"
        obs.is_potential_duplicate = False
        obs.duplicate_review_status = "RESOLVED_MERGED"
    elif action == "REQUEST_REVIEW":
        obs.review_status = "NEEDS_REVIEW"
    else:
        raise ValueError(f"Invalid review action '{action}'.")

    db.session.commit()

    log_audit(
        action="REVIEW_PASSENGER_OBSERVATION",
        entity_type="PassengerObservation",
        entity_id=obs.id,
        details={
            "action": action,
            "previous_status": prev_status,
            "new_status": obs.review_status,
            "target_person_id": target_person_id,
            "comment": comment
        },
        user_id=reviewer_id
    )

    return obs


def merge_duplicate_passengers(journey_id, source_person_id, target_person_id, reviewer_id=None, comment=None):
    """
    Safely merges a duplicate PassengerIdentity (e.g. source 'P007' into target 'P001'):
    Delegates to centralized merge_multiple_passengers for consistent canonical resolution,
    safe snapshotting, and full reversal support.
    """
    from app.services.passenger_report_service import merge_multiple_passengers
    res = merge_multiple_passengers(
        selected_person_ids=[source_person_id, target_person_id],
        master_person_id=target_person_id,
        journey_id=journey_id,
        reviewer_id=reviewer_id,
        comment=comment
    )
    return {
        "success": True,
        "target_person_id": res["master_person_id"],
        "observations_transferred": res["observations_transferred"],
        "message": res["message"]
    }


def audit_and_reconcile_journey_identities(journey_id, reviewer_id=None):
    """
    Audits and corrects existing identity records for a journey:
    1. Identifies and disassociates biometrically conflicting observations within each Person ID
       (preventing different passengers from being grouped under one ID).
    2. Identifies duplicate IDs belonging to the same real passenger (high face similarity >= 0.60,
       or >= 0.55 + matching clothing) and consolidates them under the canonical Master Person ID.
    3. Guarantees ONE real passenger = ONE persistent canonical Person ID.
    4. Preserves all original observations without data loss and records an audit trail.
    """
    import uuid
    from app.ai.matcher import (
        cosine_similarity, get_passenger_reference_embeddings,
        update_passenger_gallery_and_embedding, generate_next_person_id,
        THRESH_CONFLICT_CUTOFF
    )
    from app.models import PassengerMergeRecord, AuditLog

    journey = Journey.query.get(journey_id)
    if not journey:
        raise ValueError(f"Journey #{journey_id} not found.")

    conflicts_fixed = 0
    duplicates_consolidated = 0

    # -------------------------------------------------------------
    # Step 0: Revert Prior Active Merges to Clean Slate
    # -------------------------------------------------------------
    prior_merges = PassengerMergeRecord.query.filter_by(journey_id=journey_id, status="ACTIVE").all()
    for pm in prior_merges:
        subsumed = PassengerIdentity.query.get(pm.merged_identity_id)
        if subsumed and pm.snapshot_json:
            try:
                snap = json.loads(pm.snapshot_json)
                oids = snap.get("observations_transferred") or snap.get("transferred_observation_ids") or []
                for oid in oids:
                    obs = PassengerObservation.query.get(oid)
                    if obs:
                        obs.passenger_identity_id = subsumed.id
                subsumed.is_merged = False
                subsumed.canonical_identity_id = None
                subsumed.canonical_person_id = None
                subsumed.status = "PRESENT"
            except Exception:
                pass
        pm.status = "REVERSED"
        pm.reversed_at = datetime.utcnow()
        pm.reversal_reason = "Automated complete-linkage biometric re-audit"

    db.session.flush()

    # -------------------------------------------------------------
    # Step 1: Complete-Linkage Biometric Conflict Disassociation
    # -------------------------------------------------------------
    passengers = PassengerIdentity.query.filter_by(journey_id=journey_id, is_merged=False).order_by(PassengerIdentity.id.asc()).all()
    allocated_pids = set(p.person_id for p in PassengerIdentity.query.filter_by(journey_id=journey_id).all())

    for p in passengers:
        obs_with_face = [o for o in p.observations if o.face_embedding_json]
        if len(obs_with_face) < 2:
            continue

        faces = []
        for o in obs_with_face:
            try:
                arr = np.array(json.loads(o.face_embedding_json), dtype=np.float32)
                arr /= np.linalg.norm(arr)
                faces.append((o, arr))
            except Exception:
                pass

        if len(faces) < 2:
            continue

        n = len(faces)
        sim_mat = np.zeros((n, n))
        for i in range(n):
            for j in range(n):
                sim_mat[i, j] = cosine_similarity(faces[i][1], faces[j][1])

        in_degree = (sim_mat >= 0.70).sum(axis=1)
        best_idx = int(np.argmax(in_degree))
        anchor_obs, anchor_arr = faces[best_idx]

        kept = [(anchor_obs, anchor_arr)]
        kept_obs_ids = {anchor_obs.id}
        cand_indices = [i for i in range(n) if i != best_idx]
        cand_indices.sort(key=lambda idx: sim_mat[best_idx, idx], reverse=True)

        separated = []
        for c_idx in cand_indices:
            c_obs, c_arr = faces[c_idx]
            all_sims = [cosine_similarity(c_arr, k_arr) for _, k_arr in kept]
            # Strict multi-stage validation: Face similarity >= 0.70 required to stay in same ID
            if min(all_sims) >= 0.70 and sim_mat[best_idx, c_idx] >= 0.72:
                kept.append((c_obs, c_arr))
                kept_obs_ids.add(c_obs.id)
            else:
                separated.append((c_obs, c_arr))

        # Reassign or spawn new identity for separated conflicting faces
        for outlier_obs, outlier_arr in separated:
            # Check if outlier matches another existing clean passenger with strict threshold >= 0.75
            matched_p = None
            best_match_sim = 0.0
            for other_p in passengers:
                if other_p.id == p.id:
                    continue
                other_refs = get_passenger_reference_embeddings(other_p)
                if not other_refs:
                    continue
                o_sim = cosine_similarity(outlier_arr, other_refs[0])
                if o_sim >= 0.75:
                    all_sims = [cosine_similarity(outlier_arr, ref) for ref in other_refs]
                    if min(all_sims) >= 0.70 and o_sim > best_match_sim:
                        best_match_sim = o_sim
                        matched_p = other_p

            if matched_p:
                outlier_obs.passenger_identity_id = matched_p.id
                outlier_obs.review_status = "CONFIRMED"
                outlier_obs.similarity_score = float(best_match_sim)
            else:
                new_pid = generate_next_person_id(journey_id, allocated_ids=allocated_pids)
                allocated_pids.add(new_pid)

                new_identity = PassengerIdentity(
                    journey_id=journey_id,
                    person_id=new_pid,
                    first_seen_time=outlier_obs.observation_time or datetime.utcnow(),
                    last_seen_time=outlier_obs.observation_time or datetime.utcnow(),
                    first_seen_upload_id=outlier_obs.upload_id,
                    last_seen_upload_id=outlier_obs.upload_id,
                    first_observed_angle=outlier_obs.camera_angle or "A",
                    last_observed_angle=outlier_obs.camera_angle or "A",
                    detected_angles_json=json.dumps([outlier_obs.camera_angle or "A"]),
                    status="PRESENT",
                    total_observations=1,
                    representative_embedding_json=outlier_obs.face_embedding_json,
                    representative_clothes_json=outlier_obs.clothing_signature_json,
                    gallery_embeddings_json=json.dumps([json.loads(outlier_obs.face_embedding_json)]),
                    thumbnail_path=outlier_obs.crop_path,
                    stops_traveled=1,
                    traveled_stops_json=json.dumps([{
                        "stop_number": getattr(outlier_obs, "route_stop_id", 1) or 1,
                        "stop_name": f"Stop {outlier_obs.route_stop_id or 1}",
                        "time": (outlier_obs.observation_time or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S"),
                        "angles": [outlier_obs.camera_angle or "A"]
                    }]),
                    manual_notes=f"Disassociated from {p.person_id} due to biometric identity conflict"
                )
                db.session.add(new_identity)
                db.session.flush()

                outlier_obs.passenger_identity_id = new_identity.id
                outlier_obs.review_status = "CONFIRMED"

            p.total_observations = max(1, p.total_observations - 1)
            conflicts_fixed += 1

            log_audit(
                "DISASSOCIATE_BIOMETRIC_CONFLICT",
                details={
                    "original_person_id": p.person_id,
                    "observation_id": outlier_obs.id,
                    "target_identity_id": outlier_obs.passenger_identity_id,
                    "reason": "Complete-linkage biometric conflict (< 0.70 similarity)"
                },
                user_id=reviewer_id
            )

        # Refresh representative embedding, gallery and thumbnail for p
        clean_gallery = [k_arr.tolist() for _, k_arr in kept[:8]]
        if clean_gallery:
            p.gallery_embeddings_json = json.dumps(clean_gallery)
            p.representative_embedding_json = json.dumps(clean_gallery[0])
            for k_obs, _ in kept:
                if k_obs.crop_path and "face_" in k_obs.crop_path:
                    p.thumbnail_path = k_obs.crop_path
                    break

    db.session.flush()

    # -------------------------------------------------------------
    # Step 2: Complete-Linkage Consolidation of Duplicate IDs
    # -------------------------------------------------------------
    active_passengers = PassengerIdentity.query.filter_by(journey_id=journey_id, is_merged=False).order_by(PassengerIdentity.id.asc()).all()

    p_data = {}
    for p in active_passengers:
        refs = get_passenger_reference_embeddings(p)
        obs_ids = [o.id for o in p.observations]
        slot_set = set((o.upload_id, o.camera_angle or "A") for o in p.observations)
        p_data[p.id] = {
            "passenger": p,
            "refs": refs,
            "obs_ids": obs_ids,
            "slots": slot_set
        }

    pids_list = list(p_data.keys())
    candidate_merges = []

    for i in range(len(pids_list)):
        pid1 = pids_list[i]
        d1 = p_data[pid1]
        if not d1["refs"]:
            continue

        for j in range(i + 1, len(pids_list)):
            pid2 = pids_list[j]
            d2 = p_data[pid2]
            if not d2["refs"]:
                continue

            # Cannot merge if simultaneous in same upload and camera angle
            if d1["slots"].intersection(d2["slots"]):
                continue

            # Anchor similarity must be high (>= 0.75)
            anchor_sim = cosine_similarity(d1["refs"][0], d2["refs"][0])
            if anchor_sim < 0.75:
                continue

            # Complete linkage: ALL pairs across reference poses must have similarity >= 0.70
            all_pairs = [cosine_similarity(r1, r2) for r1 in d1["refs"] for r2 in d2["refs"]]
            min_sim = min(all_pairs)
            max_sim = max(all_pairs)

            if min_sim >= 0.70 and max_sim >= 0.75:
                candidate_merges.append((max_sim, pid1, pid2))

    candidate_merges.sort(key=lambda x: x[0], reverse=True)

    merged_into = {}
    def get_canonical(pid):
        visited = []
        curr = pid
        while curr in merged_into:
            visited.append(curr)
            curr = merged_into[curr]
        for v in visited:
            merged_into[v] = curr
        return curr

    for max_sim, p1, p2 in candidate_merges:
        c1 = get_canonical(p1)
        c2 = get_canonical(p2)
        if c1 == c2:
            continue

        d1 = p_data[c1]
        d2 = p_data[c2]

        # Simultaneous check
        if d1["slots"].intersection(d2["slots"]):
            continue

        # Complete linkage between clusters: strictly require min similarity >= 0.68
        all_pairs = [cosine_similarity(r1, r2) for r1 in d1["refs"] for r2 in d2["refs"]]
        if min(all_pairs) < 0.68:
            continue

        # Consolidate c2 into c1
        master = d1["passenger"]
        subsumed = d2["passenger"]
        transferred_oids = list(d2["obs_ids"])

        for oid in transferred_oids:
            obs = PassengerObservation.query.get(oid)
            if obs:
                obs.passenger_identity_id = master.id
                obs.review_status = "CONFIRMED"

        for ev in subsumed.events:
            ev.passenger_identity_id = master.id

        subsumed.is_merged = True
        subsumed.canonical_identity_id = master.id
        subsumed.canonical_person_id = master.person_id
        subsumed.merged_at = datetime.utcnow()
        subsumed.status = "MERGED_ARCHIVED"

        # Update master gallery and references
        for ref in d2["refs"]:
            update_passenger_gallery_and_embedding(master, ref, alpha=0.90)

        # Merge traveled stops
        try:
            t1 = json.loads(master.traveled_stops_json) if master.traveled_stops_json else []
            t2 = json.loads(subsumed.traveled_stops_json) if subsumed.traveled_stops_json else []
            seen_stops = {s.get("stop_number") for s in t1}
            for s in t2:
                if s.get("stop_number") not in seen_stops:
                    seen_stops.add(s.get("stop_number"))
                    t1.append(s)
            t1.sort(key=lambda s: s.get("stop_number", 0))
            master.traveled_stops_json = json.dumps(t1)
            master.stops_traveled = max(len(t1), master.stops_traveled or 1)
        except Exception:
            pass

        master.total_observations += len(transferred_oids)
        if subsumed.first_seen_time and (not master.first_seen_time or subsumed.first_seen_time < master.first_seen_time):
            master.first_seen_time = subsumed.first_seen_time
        if subsumed.last_seen_time and (not master.last_seen_time or subsumed.last_seen_time > master.last_seen_time):
            master.last_seen_time = subsumed.last_seen_time

        if subsumed.thumbnail_path and "face_" in subsumed.thumbnail_path and not ("face_" in (master.thumbnail_path or "")):
            master.thumbnail_path = subsumed.thumbnail_path

        mrg_record = PassengerMergeRecord(
            journey_id=journey_id,
            master_identity_id=master.id,
            master_person_id=master.person_id,
            merged_identity_id=subsumed.id,
            merged_person_id=subsumed.person_id,
            merge_timestamp=datetime.utcnow(),
            merged_by_user_id=reviewer_id,
            merge_reason="Verified complete-linkage biometric match (anchor sim >= 0.75, min sim >= 0.70)",
            status="ACTIVE",
            audit_reference=f"AUDIT-RECON-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}",
            snapshot_json=json.dumps({
                "subsumed_person_id": subsumed.person_id,
                "master_person_id": master.person_id,
                "observations_transferred": transferred_oids
            })
        )
        db.session.add(mrg_record)
        duplicates_consolidated += 1

        merged_into[c2] = c1
        d1["refs"].extend(d2["refs"])
        d1["obs_ids"].extend(transferred_oids)
        d1["slots"].update(d2["slots"])

        log_audit(
            "CONSOLIDATE_DUPLICATE_IDENTITY",
            details={
                "master_person_id": master.person_id,
                "merged_person_id": subsumed.person_id,
                "observations_transferred_count": len(transferred_oids)
            },
            user_id=reviewer_id
        )

    db.session.commit()

    # Recalculate summary metrics
    total_obs = db.session.query(PassengerObservation).filter_by(journey_id=journey_id).count()
    canonical_unique = db.session.query(PassengerIdentity).filter_by(journey_id=journey_id, is_merged=False).count()
    repeated_obs = max(0, total_obs - canonical_unique)
    matched_passengers = repeated_obs
    new_passengers = canonical_unique
    uncertain_count = db.session.query(PassengerObservation).filter_by(journey_id=journey_id, review_status="NEEDS_REVIEW").count()

    return {
        "success": True,
        "message": f"Complete-linkage reconciliation finished: {duplicates_consolidated} duplicate identities consolidated, {conflicts_fixed} conflicts resolved.",
        "stats": {
            "total_passenger_observations": total_obs,
            "total_unique_passengers": canonical_unique,
            "total_repeated_observations": repeated_obs,
            "total_new_passengers": new_passengers,
            "total_matched_passengers": matched_passengers,
            "total_uncertain_identities": uncertain_count,
            "total_identity_conflicts": conflicts_fixed,
            "total_manual_reviews": duplicates_consolidated + conflicts_fixed
        }
    }


