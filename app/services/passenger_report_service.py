import io
import csv
import json
import uuid
from datetime import datetime, date
from pathlib import Path
from sqlalchemy import or_, func, desc
from PIL import Image as PILImage, ImageDraw
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage, KeepTogether, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

from app.config import Config
from app.database import db
from app.models import (
    PassengerIdentity, PassengerObservation, PresenceEvent,
    PassengerMergeRecord, Journey, Bus, Upload, User
)
from app.services.journey_service import update_passenger_embedding
from app.services.audit_service import log_audit


def get_date_wise_passenger_report(
    target_date=None,
    start_date=None,
    end_date=None,
    bus_id=None,
    journey_id=None,
    person_id=None,
    canonical_id=None,
    angle=None,
    filter_status=None
):
    """
    Generates structured date-wise passenger report:
    Every date has:
    - Summary counts: total observations, unique canonical passengers, angle A/B/C counts
    - Date-wise crop gallery (exact 1:1 match with report observations for that date)
    - Independent Angle A, Angle B, Angle C (and Unknown Angle) passenger lists
    """
    query = db.session.query(
        PassengerObservation, PassengerIdentity, Journey, Bus, Upload
    ).outerjoin(
        PassengerIdentity, PassengerObservation.passenger_identity_id == PassengerIdentity.id
    ).join(
        Journey, PassengerObservation.journey_id == Journey.id
    ).outerjoin(
        Bus, Journey.bus_id == Bus.id
    ).outerjoin(
        Upload, PassengerObservation.upload_id == Upload.id
    )

    # 1. Date Filtering
    # Use observation_time or upload.capture_timestamp
    obs_date_expr = func.date(func.coalesce(PassengerObservation.observation_time, Upload.capture_timestamp))

    if target_date:
        if isinstance(target_date, str) and target_date.strip():
            query = query.filter(obs_date_expr == target_date.strip())
    else:
        if start_date and str(start_date).strip():
            query = query.filter(obs_date_expr >= str(start_date).strip())
        if end_date and str(end_date).strip():
            query = query.filter(obs_date_expr <= str(end_date).strip())

    # 2. Bus & Journey Filtering
    if bus_id:
        try:
            bid = int(bus_id)
            query = query.filter(Journey.bus_id == bid)
        except (ValueError, TypeError):
            pass

    if journey_id:
        try:
            jid = int(journey_id)
            query = query.filter(PassengerObservation.journey_id == jid)
        except (ValueError, TypeError):
            pass

    # 3. Angle Filter
    if angle:
        ang = str(angle).strip().upper()
        if ang in ["A", "B", "C"]:
            query = query.filter(PassengerObservation.camera_angle == ang)
        elif ang in ["UNKNOWN", "UNKNOWN ANGLE"]:
            query = query.filter(or_(
                PassengerObservation.camera_angle.is_(None),
                ~PassengerObservation.camera_angle.in_(["A", "B", "C"])
            ))

    # 4. Search Person ID & Canonical Person ID
    if person_id and str(person_id).strip():
        term = f"%{str(person_id).strip()}%"
        query = query.filter(or_(
            PassengerIdentity.person_id.ilike(term),
            PassengerObservation.temp_detection_id.ilike(term)
        ))

    if canonical_id and str(canonical_id).strip():
        term = f"%{str(canonical_id).strip()}%"
        query = query.filter(or_(
            PassengerIdentity.canonical_person_id.ilike(term),
            PassengerIdentity.person_id.ilike(term)
        ))

    # 5. Filter status
    if filter_status:
        st = str(filter_status).strip().upper()
        if st == "MERGED":
            query = query.filter(PassengerIdentity.is_merged == True)
        elif st == "UNMERGED":
            query = query.filter(or_(
                PassengerIdentity.is_merged == False,
                PassengerIdentity.is_merged.is_(None)
            ))
        elif st == "NEEDS_REVIEW":
            query = query.filter(or_(
                PassengerObservation.review_status == "NEEDS_REVIEW",
                PassengerIdentity.status == "NEEDS_REVIEW",
                PassengerIdentity.uncertain_match == True
            ))

    # Order by observation time descending
    rows = query.order_by(desc(PassengerObservation.observation_time), desc(PassengerObservation.id)).all()

    # Available dates, buses, journeys for UI dropdowns
    all_dates_query = db.session.query(
        func.distinct(func.date(func.coalesce(PassengerObservation.observation_time, Upload.capture_timestamp)))
    ).outerjoin(Upload, PassengerObservation.upload_id == Upload.id).filter(
        PassengerObservation.observation_time.isnot(None)
    ).order_by(desc(func.date(func.coalesce(PassengerObservation.observation_time, Upload.capture_timestamp)))).all()

    available_dates = [d[0] for d in all_dates_query if d and d[0]]

    buses = Bus.query.order_by(Bus.bus_number).all()
    available_buses = [{"id": b.id, "bus_number": b.bus_number, "bus_name": b.bus_name or b.bus_number} for b in buses]

    journeys = Journey.query.order_by(desc(Journey.id)).limit(50).all()
    available_journeys = [{"id": j.id, "journey_code": j.journey_code, "route": f"{j.origin} -> {j.destination}"} for j in journeys]

    # Group records by Date
    date_groups = {}

    for obs, p, j, b, u in rows:
        obs_dt = obs.observation_time or u.capture_timestamp or u.upload_timestamp
        date_key = obs_dt.strftime("%Y-%m-%d") if obs_dt else "Unknown Date"
        formatted_date = obs_dt.strftime("%d %B %Y") if obs_dt else "Unknown Date"

        if date_key not in date_groups:
            date_groups[date_key] = {
                "date": date_key,
                "date_formatted": formatted_date,
                "observations": [],
                "unique_canonical_ids": set(),
                "unique_passengers_map": {},
                "unique_gallery_map": {},
                "gallery_crops": [],
                "angle_sections": {
                    "A": [],
                    "B": [],
                    "C": [],
                    "UNKNOWN": []
                }
            }

        # Determine camera angle accurately
        raw_angle = (obs.camera_angle or "").strip().upper()
        if raw_angle in ["A", "B", "C"]:
            display_angle = f"Angle {raw_angle}"
            section_key = raw_angle
        else:
            display_angle = "Unknown Angle"
            section_key = "UNKNOWN"

        orig_pid = p.person_id if p else (obs.temp_detection_id or "Unidentified")
        canonical_pid = p.get_canonical_person_id() if p else orig_pid
        is_merged = bool((p and (p.is_merged or p.canonical_identity_id is not None)) or (obs.duplicate_review_status == "RESOLVED_MERGED"))

        # Check crop file validity
        crop_url = None
        has_valid_crop = False
        crop_warning = None
        from app.services.cloud_sync_service import resolve_passenger_image_url
        target_crop_ref = obs.crop_path or (p.thumbnail_path if p else None)
        if target_crop_ref or orig_pid:
            crop_url = resolve_passenger_image_url(target_crop_ref, orig_pid)
            has_valid_crop = bool(crop_url)
            if not has_valid_crop:
                crop_warning = "Crop file missing from disk"
        else:
            crop_warning = "No face crop generated for this observation"

        det_conf = round(float(obs.detection_confidence), 3) if obs.detection_confidence is not None else 0.0
        face_conf = round(float(obs.face_confidence), 3) if obs.face_confidence is not None else 0.0
        sim_score = round(float(obs.similarity_score), 3) if obs.similarity_score is not None else 0.0
        id_conf = max(sim_score, face_conf)

        obs_item = {
            "observation_id": obs.id,
            "passenger_id": p.id if p else None,
            "person_id": orig_pid,
            "canonical_person_id": canonical_pid,
            "is_merged": is_merged,
            "camera_angle": display_angle,
            "angle_key": section_key,
            "observation_time": obs_dt.strftime("%Y-%m-%d %H:%M:%S") if obs_dt else "N/A",
            "detection_confidence": det_conf,
            "detection_confidence_pct": round(det_conf * 100, 1),
            "identity_confidence": id_conf,
            "identity_confidence_pct": round(id_conf * 100, 1) if id_conf > 0 else None,
            "crop_url": crop_url,
            "has_valid_crop": has_valid_crop,
            "crop_warning": crop_warning,
            "bus_id": b.id if b else (j.bus_id if j else None),
            "bus_number": b.bus_number if b else (f"Bus #{j.bus_id}" if j and j.bus_id else "N/A"),
            "journey_id": j.id if j else obs.journey_id,
            "journey_code": j.journey_code if j else f"JNY-{obs.journey_id}",
            "origin": j.origin if j else "",
            "destination": j.destination if j else "",
            "status": p.status if p else "UNIDENTIFIED",
            "review_status": obs.review_status or "CONFIRMED",
            "duplicate_review_status": obs.duplicate_review_status,
            "quality_score": round(float(obs.quality_score or 1.0), 2),
            "current_seat": p.get_seat_number() if p else "N/A"
        }

        date_groups[date_key]["observations"].append(obs_item)
        date_groups[date_key]["unique_canonical_ids"].add(canonical_pid)
        date_groups[date_key]["angle_sections"][section_key].append(obs_item)

        # Ensure unique representative card per person in the merge / crop gallery (no repeated IDs)
        ug_map = date_groups[date_key]["unique_gallery_map"]
        if not is_merged:
            if canonical_pid not in ug_map:
                card_item = dict(obs_item)
                card_item["total_detections"] = 1
                card_item["first_seen_time"] = obs_dt.strftime("%Y-%m-%d %H:%M:%S") if obs_dt else "N/A"
                card_item["last_seen_time"] = obs_dt.strftime("%Y-%m-%d %H:%M:%S") if obs_dt else "N/A"
                card_item["stops_traveled"] = p.get_distinct_stops_count() if p else 1
                ug_map[canonical_pid] = card_item
            else:
                existing_card = ug_map[canonical_pid]
                existing_card["total_detections"] = existing_card.get("total_detections", 1) + 1
                if p and p.get_distinct_stops_count() > existing_card.get("stops_traveled", 1):
                    existing_card["stops_traveled"] = p.get_distinct_stops_count()
                if has_valid_crop and (not existing_card.get("has_valid_crop") or det_conf > existing_card.get("detection_confidence", 0)):
                    existing_card["crop_url"] = crop_url
                    existing_card["has_valid_crop"] = True
                    existing_card["observation_id"] = obs.id
                    existing_card["detection_confidence"] = det_conf
                    existing_card["detection_confidence_pct"] = round(det_conf * 100, 1)
                    existing_card["camera_angle"] = display_angle

        # -------------------------------------------------------------
        # Aggregate by Unique Canonical Person (Single ID & Combined Timeline)
        # -------------------------------------------------------------
        up_map = date_groups[date_key]["unique_passengers_map"]
        occurrence_entry = {
            "observation_id": obs.id,
            "time": obs_dt.strftime("%Y-%m-%d %H:%M:%S") if obs_dt else "N/A",
            "time_short": obs_dt.strftime("%H:%M:%S") if obs_dt else "N/A",
            "angle": display_angle,
            "angle_key": section_key,
            "det_conf_pct": round(det_conf * 100, 1),
            "id_conf_pct": round(id_conf * 100, 1) if id_conf > 0 else None,
            "crop_url": crop_url,
            "original_person_id": orig_pid
        }

        if canonical_pid not in up_map:
            up_map[canonical_pid] = {
                "passenger_id": p.id if p else None,
                "person_id": canonical_pid,
                "canonical_person_id": canonical_pid,
                "primary_person_id": canonical_pid,
                "is_merged": False,
                "status": p.status if p else "PRESENT",
                "bus_number": b.bus_number if b else (f"Bus #{j.bus_id}" if j and j.bus_id else "N/A"),
                "journey_code": j.journey_code if j else f"JNY-{obs.journey_id}",
                "current_seat": p.get_seat_number() if p else "N/A",
                "best_crop_url": crop_url,
                "has_valid_crop": has_valid_crop,
                "best_crop_obs_id": obs.id,
                "quality_score": float(obs.quality_score or 1.0),
                "first_seen_dt": obs_dt,
                "last_seen_dt": obs_dt,
                "first_seen_time": obs_dt.strftime("%Y-%m-%d %H:%M:%S") if obs_dt else "N/A",
                "last_seen_time": obs_dt.strftime("%Y-%m-%d %H:%M:%S") if obs_dt else "N/A",
                "first_angle": display_angle,
                "last_angle": display_angle,
                "detected_angles": [display_angle],
                "total_detections": 1,
                "presence_occurrences": [occurrence_entry]
            }
        else:
            # Person repeats on this date: compare and aggregate under the same single Master ID
            existing = up_map[canonical_pid]
            existing["total_detections"] += 1
            if display_angle not in existing["detected_angles"]:
                existing["detected_angles"].append(display_angle)

            if obs_dt and existing["first_seen_dt"] and obs_dt < existing["first_seen_dt"]:
                existing["first_seen_dt"] = obs_dt
                existing["first_seen_time"] = obs_dt.strftime("%Y-%m-%d %H:%M:%S")
                existing["first_angle"] = display_angle
            elif obs_dt and existing["last_seen_dt"] and obs_dt > existing["last_seen_dt"]:
                existing["last_seen_dt"] = obs_dt
                existing["last_seen_time"] = obs_dt.strftime("%Y-%m-%d %H:%M:%S")
                existing["last_angle"] = display_angle

            if has_valid_crop and (not existing["has_valid_crop"] or float(obs.quality_score or 1.0) > existing["quality_score"]):
                existing["best_crop_url"] = crop_url
                existing["has_valid_crop"] = True
                existing["quality_score"] = float(obs.quality_score or 1.0)
                existing["best_crop_obs_id"] = obs.id

            existing["presence_occurrences"].append(occurrence_entry)

    # Build final list of date sections with sequence and counts
    result_dates = []
    for date_key in sorted(date_groups.keys(), reverse=True):
        grp = date_groups[date_key]
        total_obs = len(grp["observations"])
        unique_canonical = len(grp["unique_canonical_ids"])
        angle_a_count = len(grp["angle_sections"]["A"])
        angle_b_count = len(grp["angle_sections"]["B"])
        angle_c_count = len(grp["angle_sections"]["C"])
        unknown_angle_count = len(grp["angle_sections"]["UNKNOWN"])
        merged_count = sum(1 for item in grp["observations"] if item["is_merged"])
        needs_review_count = sum(1 for item in grp["observations"] if item["review_status"] == "NEEDS_REVIEW" or item["status"] == "NEEDS_REVIEW")

        total_repeated_obs = max(0, total_obs - unique_canonical)
        total_new_pass = unique_canonical
        total_matched_pass = total_repeated_obs
        uncertain_identities = needs_review_count
        identity_conflicts = sum(1 for item in grp["observations"] if item.get("has_conflict", False))
        total_manual_rev = needs_review_count + merged_count

        # Sort unique passengers chronologically into sequence
        unique_passengers_list = list(grp["unique_passengers_map"].values())
        unique_passengers_list.sort(key=lambda u: (u["first_seen_dt"] or datetime.min, u["person_id"]))

        seq_by_canonical = {}
        for seq_idx, u_item in enumerate(unique_passengers_list, 1):
            u_item["sequence_no"] = seq_idx
            seq_by_canonical[u_item["canonical_person_id"]] = seq_idx
            u_item["presence_occurrences"].sort(key=lambda o: o["time"])
            
            times_summary = ", ".join([f"{o['time_short']} [{o['angle']}]" for o in u_item["presence_occurrences"][:4]])
            if len(u_item["presence_occurrences"]) > 4:
                times_summary += f", +{len(u_item['presence_occurrences']) - 4} more"
            u_item["presence_summary"] = times_summary
            u_item["angles_str"] = ", ".join(u_item["detected_angles"])

        # Deduplicate gallery_crops strictly to ONE card per unique person ID
        unique_crops_list = list(grp.get("unique_gallery_map", {}).values())
        unique_crops_list.sort(key=lambda c: (seq_by_canonical.get(c["canonical_person_id"], 9999), c["person_id"]))
        for crop_item in unique_crops_list:
            crop_item["sequence_no"] = seq_by_canonical.get(crop_item["canonical_person_id"], 1)

        grp["gallery_crops"] = unique_crops_list

        result_dates.append({
            "date": date_key,
            "date_formatted": grp["date_formatted"],
            "summary": {
                "total_observations": total_obs,
                "total_unique_passengers": unique_canonical,
                "total_passenger_observations": total_obs,
                "total_repeated_observations": total_repeated_obs,
                "total_new_passengers": total_new_pass,
                "total_matched_passengers": total_matched_pass,
                "total_uncertain_identities": uncertain_identities,
                "total_identity_conflicts": identity_conflicts,
                "total_manual_reviews": total_manual_rev,
                "angle_a_count": angle_a_count,
                "angle_b_count": angle_b_count,
                "angle_c_count": angle_c_count,
                "unknown_angle_count": unknown_angle_count,
                "merged_count": merged_count,
                "needs_review_count": needs_review_count
            },
            "unique_passengers": unique_passengers_list,
            "gallery_crops": grp["gallery_crops"],
            "observations": grp["observations"],
            "angle_sections": {
                "angle_a": grp["angle_sections"]["A"],
                "angle_b": grp["angle_sections"]["B"],
                "angle_c": grp["angle_sections"]["C"],
                "unknown_angle": grp["angle_sections"]["UNKNOWN"]
            }
        })

    # Overall dataset totals across all matched dates
    overall_total_obs = sum(d["summary"]["total_observations"] for d in result_dates)
    overall_unique_ids = set()
    for grp in date_groups.values():
        overall_unique_ids.update(grp["unique_canonical_ids"])
    overall_unique = len(overall_unique_ids)
    overall_repeated = max(0, overall_total_obs - overall_unique)

    return {
        "success": True,
        "date_groups": result_dates,
        "total_dates_count": len(result_dates),
        "overall_summary": {
            "total_observations": overall_total_obs,
            "total_unique_passengers": overall_unique,
            "total_passenger_observations": overall_total_obs,
            "total_repeated_observations": overall_repeated,
            "total_new_passengers": overall_unique,
            "total_matched_passengers": overall_repeated,
            "total_uncertain_identities": sum(d["summary"]["total_uncertain_identities"] for d in result_dates),
            "total_identity_conflicts": sum(d["summary"]["total_identity_conflicts"] for d in result_dates),
            "total_manual_reviews": sum(d["summary"]["total_manual_reviews"] for d in result_dates)
        },
        "available_dates": available_dates,
        "available_buses": available_buses,
        "available_journeys": available_journeys
    }


def merge_multiple_passengers(selected_person_ids, master_person_id, journey_id=None, reviewer_id=None, comment=None):
    """
    Safely merges two or more duplicate passenger IDs:
    1. Validates all selected Person IDs exist and are distinct.
    2. Master Person ID must be in selected Person IDs.
    3. Resolves canonical root for any selected ID already merged.
    4. Validates identity scope (same journey / compatible bus).
    5. Prevents circular / cyclic merges.
    6. Reassigns observations and presence events while preserving camera angle and timestamps.
    7. Updates master embeddings, observations count, and stops.
    8. Sets alias identities to 'MERGED_ARCHIVED' with canonical pointer.
    9. Persists snapshot in PassengerMergeRecord for safe undo/reversal.
    10. Logs full provenance in AuditLog.
    """
    if not selected_person_ids or len(selected_person_ids) < 2:
        raise ValueError("At least two distinct Person IDs must be selected to perform a merge.")

    master_person_id = str(master_person_id).strip()
    clean_ids = list(dict.fromkeys(str(pid).strip() for pid in selected_person_ids if str(pid).strip()))

    if len(clean_ids) < 2:
        raise ValueError("Please select at least two different Person IDs to merge.")

    if master_person_id not in clean_ids:
        raise ValueError(f"Master Person ID '{master_person_id}' must be one of the selected IDs.")

    # Fetch all identities
    query = PassengerIdentity.query.filter(PassengerIdentity.person_id.in_(clean_ids))
    if journey_id:
        try:
            jid = int(journey_id)
            query = query.filter(PassengerIdentity.journey_id == jid)
        except (ValueError, TypeError):
            pass

    identities = query.all()
    found_by_pid = {p.person_id: p for p in identities}

    missing = [pid for pid in clean_ids if pid not in found_by_pid]
    if missing:
        raise ValueError(f"Person ID(s) not found in database: {', '.join(missing)}")

    # Resolve master identity (resolve canonical if it was itself an alias)
    chosen_master = found_by_pid[master_person_id]
    canonical_master = chosen_master.get_canonical_identity()

    # Scope validation: Check if different journeys / buses
    distinct_journeys = {p.journey_id for p in identities}
    if len(distinct_journeys) > 1 and not journey_id:
        bus_ids = {p.journey.bus_id for p in identities if p.journey}
        if len(bus_ids) > 1:
            raise ValueError(f"Selected IDs span multiple different buses ({list(bus_ids)}). Incompatible identity scope.")

    # Cycle check
    for p in identities:
        if p.id != canonical_master.id:
            curr = canonical_master
            visited = set()
            while curr and curr.canonical_identity_id:
                if curr.canonical_identity_id == p.id:
                    raise ValueError(f"Cyclic merge prohibited: {canonical_master.person_id} is already linked to {p.person_id}.")
                if curr.id in visited:
                    break
                visited.add(curr.id)
                curr = PassengerIdentity.query.get(curr.canonical_identity_id)

    # Biometric consistency audit logging:
    # Calculate cross-similarity across camera angles for provenance and audit trail
    from app.ai.matcher import (
        get_passenger_reference_embeddings, cosine_similarity,
        update_passenger_gallery_and_embedding, THRESH_CONFLICT_CUTOFF
    )
    master_refs = get_passenger_reference_embeddings(canonical_master)
    cross_angle_notes = []
    for p in identities:
        if p.id == canonical_master.id:
            continue
        p_refs = get_passenger_reference_embeddings(p)
        if master_refs and p_refs:
            cross_sims = [cosine_similarity(r1, r2) for r1 in master_refs for r2 in p_refs]
            max_cross = max(cross_sims)
            if max_cross < THRESH_CONFLICT_CUTOFF:
                cross_angle_notes.append(f"{p.person_id} cross-angle sim {max_cross:.2f}")

    # Generate an audit batch ID
    audit_ref = f"MRG-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    created_records = []
    total_obs_transferred = 0

    for pid in clean_ids:
        p = found_by_pid[pid]
        if p.id == canonical_master.id:
            continue

        # Snapshot current state for reliable reversal
        obs_transferred = []
        for obs in p.observations:
            obs_transferred.append(obs.id)
            obs.passenger_identity_id = canonical_master.id
            obs.review_status = "CONFIRMED"
            obs.duplicate_review_status = "RESOLVED_MERGED"

        # Transfer presence events
        for ev in p.events:
            ev.passenger_identity_id = canonical_master.id

        # Merge gallery embeddings with anti-poisoning
        source_refs = []
        if p.gallery_embeddings_json:
            try:
                source_refs.extend(json.loads(p.gallery_embeddings_json))
            except Exception:
                pass
        if p.representative_embedding_json:
            try:
                source_refs.append(json.loads(p.representative_embedding_json))
            except Exception:
                pass
        for ref in source_refs:
            update_passenger_gallery_and_embedding(canonical_master, ref, alpha=0.90)

        snapshot_data = {
            "alias_identity_id": p.id,
            "alias_person_id": p.person_id,
            "previous_status": p.status,
            "previous_is_merged": p.is_merged,
            "previous_canonical_identity_id": p.canonical_identity_id,
            "previous_canonical_person_id": p.canonical_person_id,
            "previous_manual_notes": p.manual_notes,
            "transferred_observation_ids": obs_transferred,
            "previous_total_observations": p.total_observations,
            "previous_stops_traveled": p.stops_traveled
        }

        # Update alias identity
        p.canonical_identity_id = canonical_master.id
        p.canonical_person_id = canonical_master.person_id
        p.is_merged = True
        p.merged_at = datetime.utcnow()
        p.status = "MERGED_ARCHIVED"
        p.manual_notes = f"Merged into Master {canonical_master.person_id} by reviewer #{reviewer_id or 'System'}. Reason: {comment or 'Consolidated duplicate identity'}"
        p.updated_at = datetime.utcnow()

        # Update canonical master metrics
        transferred_count = len(obs_transferred)
        total_obs_transferred += transferred_count
        canonical_master.total_observations = (canonical_master.total_observations or 0) + transferred_count
        if p.last_seen_time and (not canonical_master.last_seen_time or p.last_seen_time > canonical_master.last_seen_time):
            canonical_master.last_seen_time = p.last_seen_time
            canonical_master.last_seen_upload_id = p.last_seen_upload_id
            canonical_master.last_latitude = p.last_latitude
            canonical_master.last_longitude = p.last_longitude

        if p.stops_traveled and (not canonical_master.stops_traveled or p.stops_traveled > canonical_master.stops_traveled):
            canonical_master.stops_traveled = p.stops_traveled

        if p.thumbnail_path and "face_" in p.thumbnail_path and (not canonical_master.thumbnail_path or "face_" not in canonical_master.thumbnail_path):
            canonical_master.thumbnail_path = p.thumbnail_path

        # Create PassengerMergeRecord
        full_reason = comment or "Manual operator merge"
        if cross_angle_notes:
            full_reason = f"{full_reason} ({'; '.join(cross_angle_notes)})"

        rec = PassengerMergeRecord(
            journey_id=canonical_master.journey_id,
            master_identity_id=canonical_master.id,
            master_person_id=canonical_master.person_id,
            merged_identity_id=p.id,
            merged_person_id=p.person_id,
            merge_timestamp=datetime.utcnow(),
            merged_by_user_id=reviewer_id,
            merge_reason=full_reason,
            status="ACTIVE",
            audit_reference=audit_ref,
            snapshot_json=json.dumps(snapshot_data)
        )
        db.session.add(rec)
        created_records.append(rec)

    canonical_master.updated_at = datetime.utcnow()
    db.session.commit()

    log_audit(
        action="MERGE_PASSENGER_IDENTITIES",
        entity_type="PassengerIdentity",
        entity_id=canonical_master.id,
        details={
            "master_person_id": canonical_master.person_id,
            "merged_person_ids": [pid for pid in clean_ids if pid != canonical_master.person_id],
            "observations_transferred": total_obs_transferred,
            "comment": comment,
            "audit_reference": audit_ref
        },
        user_id=reviewer_id
    )

    return {
        "success": True,
        "master_person_id": canonical_master.person_id,
        "merged_person_ids": [pid for pid in clean_ids if pid != canonical_master.person_id],
        "observations_transferred": total_obs_transferred,
        "audit_reference": audit_ref,
        "records_count": len(created_records),
        "message": f"Successfully merged {len(clean_ids)-1} identity/identities into Master {canonical_master.person_id}."
    }


def reverse_passenger_merge(merge_record_id, reviewer_id=None, reason=None):
    """
    Safely reverses an identity merge:
    1. Restores the alias identity to its previous unmerged status.
    2. Reassigns all transferred observations back to the original alias identity.
    3. Re-adjusts master observation counts.
    4. Sets merge record status to 'REVERSED'.
    5. Logs the reversal in AuditLog.
    """
    rec = PassengerMergeRecord.query.get(merge_record_id)
    if not rec:
        raise ValueError(f"Merge record #{merge_record_id} not found.")

    if rec.status == "REVERSED":
        raise ValueError(f"Merge record #{merge_record_id} has already been reversed.")

    snapshot = {}
    if rec.snapshot_json:
        try:
            snapshot = json.loads(rec.snapshot_json)
        except Exception:
            pass

    master_p = PassengerIdentity.query.get(rec.master_identity_id)
    alias_p = PassengerIdentity.query.get(rec.merged_identity_id)

    if not alias_p:
        raise ValueError(f"Merged identity #{rec.merged_identity_id} not found in database.")

    # 1. Restore observations
    obs_ids = snapshot.get("transferred_observation_ids", [])
    if obs_ids:
        observations = PassengerObservation.query.filter(PassengerObservation.id.in_(obs_ids)).all()
        for obs in observations:
            obs.passenger_identity_id = alias_p.id

    # 2. Restore alias identity state
    alias_p.status = snapshot.get("previous_status", "PRESENT")
    alias_p.is_merged = snapshot.get("previous_is_merged", False)
    alias_p.canonical_identity_id = snapshot.get("previous_canonical_identity_id")
    alias_p.canonical_person_id = snapshot.get("previous_canonical_person_id")
    alias_p.manual_notes = f"Merge reversed by reviewer #{reviewer_id or 'System'}. Reason: {reason or 'Operator requested merge reversal'}"
    alias_p.updated_at = datetime.utcnow()

    # 3. Adjust master passenger observation count
    if master_p:
        transferred_count = len(obs_ids)
        master_p.total_observations = max(1, (master_p.total_observations or 1) - transferred_count)
        master_p.updated_at = datetime.utcnow()

    # 4. Update merge record
    rec.status = "REVERSED"
    rec.reversed_at = datetime.utcnow()
    rec.reversed_by_user_id = reviewer_id
    rec.reversal_reason = reason or "Operator requested undo"

    db.session.commit()

    log_audit(
        action="REVERSE_PASSENGER_MERGE",
        entity_type="PassengerMergeRecord",
        entity_id=rec.id,
        details={
            "master_person_id": rec.master_person_id,
            "merged_person_id": rec.merged_person_id,
            "restored_observations": len(obs_ids),
            "reason": reason
        },
        user_id=reviewer_id
    )

    return {
        "success": True,
        "message": f"Successfully reversed merge of {rec.merged_person_id} from Master {rec.master_person_id}.",
        "restored_person_id": rec.merged_person_id,
        "restored_observations": len(obs_ids)
    }


def get_merge_history(journey_id=None):
    """
    Returns list of all merge operations with audit metadata.
    """
    query = PassengerMergeRecord.query
    if journey_id:
        try:
            jid = int(journey_id)
            query = query.filter(PassengerMergeRecord.journey_id == jid)
        except (ValueError, TypeError):
            pass

    records = query.order_by(desc(PassengerMergeRecord.merge_timestamp)).all()
    return [r.to_dict() for r in records]


def generate_date_wise_report_csv(
    target_date=None,
    start_date=None,
    end_date=None,
    bus_id=None,
    journey_id=None,
    person_id=None,
    canonical_id=None,
    angle=None,
    filter_status=None
):
    """
    Generates privacy-compliant CSV export of the date-wise passenger report.
    Excludes sensitive biometric embeddings.
    """
    report = get_date_wise_passenger_report(
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

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "# SMARTBUS AI — DATE-WISE MULTI-ANGLE PASSENGER TRACKING REPORT",
        f"# Export Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "# Protected biometric embeddings are strictly excluded."
    ])
    writer.writerow([])

    writer.writerow([
        "# SECTION 1: UNIQUE PASSENGERS MANIFEST (IN SEQUENCE — EXACT UNIQUE COUNTS)",
        "# One single canonical ID per physical passenger with consolidated presence timeline across times & angles."
    ])
    manifest_headers = [
        "Report Date", "Sequence #", "Unique Person ID", "Passenger Status",
        "First Seen Time", "First Angle", "Last Seen Time", "Last Angle",
        "Total Detections", "Detected Angles", "Presence Timeline (All Occurrences)",
        "Bus Number", "Journey Code", "Assigned Seat"
    ]
    writer.writerow(manifest_headers)

    for grp in report.get("date_groups", []):
        d_str = grp["date_formatted"]
        for u in grp.get("unique_passengers", []):
            writer.writerow([
                d_str,
                u.get("sequence_no", "-"),
                u.get("person_id", "N/A"),
                u.get("status", "PRESENT"),
                u.get("first_seen_time", "N/A"),
                u.get("first_angle", "N/A"),
                u.get("last_seen_time", "N/A"),
                u.get("last_angle", "N/A"),
                u.get("total_detections", 1),
                u.get("angles_str", "N/A"),
                u.get("presence_summary", "N/A"),
                u.get("bus_number", "N/A"),
                u.get("journey_code", "N/A"),
                u.get("current_seat", "N/A")
            ])

    writer.writerow([])
    writer.writerow(["# SECTION 2: ALL CAMERA ANGLE OBSERVATIONS & AUDIT LOG"])
    headers = [
        "Report Date",
        "Bus Number",
        "Journey Code",
        "Original Person ID",
        "Canonical Person ID",
        "Merge Status",
        "Camera Angle",
        "Detection Timestamp",
        "Detection Confidence (%)",
        "Identity Confidence (%)",
        "Passenger Status",
        "Observation Status",
        "Assigned Seat"
    ]
    writer.writerow(headers)

    for grp in report.get("date_groups", []):
        d_str = grp["date_formatted"]
        for obs in grp.get("observations", []):
            writer.writerow([
                d_str,
                obs.get("bus_number", "N/A"),
                obs.get("journey_code", "N/A"),
                obs.get("person_id", "N/A"),
                obs.get("canonical_person_id", "N/A"),
                "Merged Alias" if obs.get("is_merged") else "Canonical Master",
                obs.get("camera_angle", "Unknown Angle"),
                obs.get("observation_time", "N/A"),
                f"{obs.get('detection_confidence_pct', 0.0)}%",
                f"{obs.get('identity_confidence_pct')}%" if obs.get('identity_confidence_pct') is not None else "N/A",
                obs.get("status", "N/A"),
                obs.get("review_status", "N/A"),
                obs.get("current_seat", "N/A")
            ])

    output.seek(0)
    return output.getvalue()


def generate_date_wise_report_excel(
    target_date=None,
    start_date=None,
    end_date=None,
    bus_id=None,
    journey_id=None,
    person_id=None,
    canonical_id=None,
    angle=None,
    filter_status=None
):
    """
    Generates styled Excel (.xlsx) workbook for the date-wise passenger report.
    Contains:
    - Sheet 1: Detailed Detections
    - Sheet 2: Date & Angle Summary
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    report = get_date_wise_passenger_report(
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

    wb = openpyxl.Workbook()
    
    # Header styling
    header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid") # Dark blue
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    center_align = Alignment(horizontal="center", vertical="center")
    left_align = Alignment(horizontal="left", vertical="center")
    thin_border = Border(
        left=Side(style='thin', color='E2E8F0'),
        right=Side(style='thin', color='E2E8F0'),
        top=Side(style='thin', color='E2E8F0'),
        bottom=Side(style='thin', color='E2E8F0')
    )

    # Sheet 1: Unique Passenger Manifest (In Sequence)
    ws0 = wb.active
    ws0.title = "Unique Passenger Manifest"
    ws0.merge_cells("A1:K1")
    t0 = ws0["A1"]
    t0.value = "SmartBus AI — Unique Passenger Manifest (In Sequence — Consolidated Timelines)"
    t0.font = Font(name="Calibri", size=14, bold=True, color="1E3A8A")
    t0.alignment = left_align
    ws0.row_dimensions[1].height = 25

    manifest_headers = [
        "Report Date", "Seq #", "Unique Person ID", "Passenger Status",
        "Boarding Time (First In)", "First Angle", "Last Seen Time (Out)", "Last Angle",
        "Total Detections", "Detected Angles", "Presence Timeline (All Occurrences)"
    ]
    for col_num, h_text in enumerate(manifest_headers, 1):
        c = ws0.cell(row=3, column=col_num)
        c.value = h_text
        c.fill = header_fill
        c.font = header_font
        c.alignment = center_align
    ws0.row_dimensions[3].height = 24

    row_u = 4
    for grp in report.get("date_groups", []):
        d_str = grp["date_formatted"]
        for u in grp.get("unique_passengers", []):
            vals_u = [
                d_str,
                u.get("sequence_no", "-"),
                u.get("person_id", "N/A"),
                u.get("status", "PRESENT"),
                u.get("first_seen_time", "N/A"),
                u.get("first_angle", "N/A"),
                u.get("last_seen_time", "N/A"),
                u.get("last_angle", "N/A"),
                u.get("total_detections", 1),
                u.get("angles_str", "N/A"),
                u.get("presence_summary", "N/A")
            ]
            for col_num, val in enumerate(vals_u, 1):
                c = ws0.cell(row=row_u, column=col_num)
                c.value = val
                c.border = thin_border
                c.alignment = center_align if col_num in [1, 2, 4, 6, 8, 9] else left_align
            row_u += 1

    for col in ws0.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws0.column_dimensions[col_letter].width = max(max_len + 3, 12)

    # Sheet 2: All Detections
    ws1 = wb.create_sheet(title="Passenger Detections")

    # Title Banner
    ws1.merge_cells("A1:M1")
    title_cell = ws1["A1"]
    title_cell.value = "SmartBus AI — Date-Wise Multi-Angle Passenger Detection Report"
    title_cell.font = Font(name="Calibri", size=14, bold=True, color="1E3A8A")
    title_cell.alignment = left_align
    ws1.row_dimensions[1].height = 25

    headers = [
        "Report Date", "Bus Number", "Journey Code", "Original Person ID",
        "Canonical Person ID", "Merge Status", "Camera Angle", "Detection Timestamp",
        "Detection Conf (%)", "Identity Conf (%)", "Passenger Status", "Observation Status", "Assigned Seat"
    ]

    for col_num, h_text in enumerate(headers, 1):
        c = ws1.cell(row=3, column=col_num)
        c.value = h_text
        c.fill = header_fill
        c.font = header_font
        c.alignment = center_align
    ws1.row_dimensions[3].height = 24

    row_idx = 4
    for grp in report.get("date_groups", []):
        d_str = grp["date_formatted"]
        for obs in grp.get("observations", []):
            vals = [
                d_str,
                obs.get("bus_number", "N/A"),
                obs.get("journey_code", "N/A"),
                obs.get("person_id", "N/A"),
                obs.get("canonical_person_id", "N/A"),
                "Merged Alias" if obs.get("is_merged") else "Canonical Master",
                obs.get("camera_angle", "Unknown Angle"),
                obs.get("observation_time", "N/A"),
                f"{obs.get('detection_confidence_pct', 0.0)}%",
                f"{obs.get('identity_confidence_pct')}%" if obs.get('identity_confidence_pct') is not None else "N/A",
                obs.get("status", "N/A"),
                obs.get("review_status", "N/A"),
                obs.get("current_seat", "N/A")
            ]
            for col_num, val in enumerate(vals, 1):
                c = ws1.cell(row=row_idx, column=col_num)
                c.value = val
                c.border = thin_border
                c.alignment = center_align if col_num in [1, 2, 6, 7, 9, 10, 11, 12, 13] else left_align
            row_idx += 1

    # Auto-adjust column widths
    for col in ws1.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws1.column_dimensions[col_letter].width = max(max_len + 3, 12)

    # Sheet 2: Summary by Date
    ws2 = wb.create_sheet(title="Date & Angle Summary")
    ws2.merge_cells("A1:H1")
    t2 = ws2["A1"]
    t2.value = "SmartBus AI — Date & Angle Passenger Summary"
    t2.font = Font(name="Calibri", size=14, bold=True, color="1E3A8A")
    t2.alignment = left_align
    ws2.row_dimensions[1].height = 25

    sum_headers = [
        "Date", "Formatted Date", "Total Observations", "Unique Passengers (Canonical)",
        "Angle A Detections", "Angle B Detections", "Angle C Detections", "Merged Aliases Count"
    ]
    for col_num, h_text in enumerate(sum_headers, 1):
        c = ws2.cell(row=3, column=col_num)
        c.value = h_text
        c.fill = header_fill
        c.font = header_font
        c.alignment = center_align
    ws2.row_dimensions[3].height = 24

    sum_row = 4
    for grp in report.get("date_groups", []):
        s = grp["summary"]
        vals = [
            grp["date"],
            grp["date_formatted"],
            s["total_observations"],
            s["total_unique_passengers"],
            s["angle_a_count"],
            s["angle_b_count"],
            s["angle_c_count"],
            s["merged_count"]
        ]
        for col_num, val in enumerate(vals, 1):
            c = ws2.cell(row=sum_row, column=col_num)
            c.value = val
            c.border = thin_border
            c.alignment = center_align if col_num > 2 else left_align
        sum_row += 1

    for col in ws2.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws2.column_dimensions[col_letter].width = max(max_len + 4, 15)

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    return stream.getvalue()


def _get_obs_face_flowable(crop_path, person_id, is_merged=False, status="PRESENT", size=(26, 26)):
    """
    Returns a ReportLab Image flowable for a passenger observation face crop.
    Uses PIL to read and center-crop the face from disk.
    If unavailable or missing, generates a clean circular avatar icon with initials and ring.
    """
    target_size = (64, 64)
    crop_found = False
    img_buf = io.BytesIO()

    # Determine status ring color
    if is_merged:
        ring_color = (245, 158, 11)  # Amber
        bg_color = (254, 243, 199)
    elif "PRESENT" in (status or "").upper():
        ring_color = (16, 185, 129)  # Emerald green
        bg_color = (209, 250, 229)
    elif "EXIT" in (status or "").upper():
        ring_color = (239, 68, 68)   # Rose red
        bg_color = (254, 226, 226)
    else:
        ring_color = (59, 130, 246)  # Blue
        bg_color = (239, 246, 255)

    if crop_path:
        clean_path = str(crop_path).replace("\\", "/").strip()
        if clean_path.startswith("/data/"):
            clean_path = clean_path[6:]
        elif clean_path.startswith("data/"):
            clean_path = clean_path[5:]

        candidates = [
            Path(Config.DATA_DIR) / clean_path,
            Path(Config.CROP_FOLDER) / Path(clean_path).name,
            Path(Config.UPLOAD_FOLDER) / Path(clean_path).name,
            Path(clean_path)
        ]
        for c in candidates:
            if c.exists() and c.is_file():
                try:
                    with PILImage.open(c) as im:
                        im = im.convert("RGB")
                        w, h = im.size
                        min_dim = min(w, h)
                        left = (w - min_dim) // 2
                        top = (h - min_dim) // 2
                        im = im.crop((left, top, left + min_dim, top + min_dim))
                        im = im.resize(target_size, PILImage.Resampling.LANCZOS)
                        draw = ImageDraw.Draw(im)
                        draw.rectangle([(0, 0), (target_size[0] - 1, target_size[1] - 1)], outline=ring_color, width=3)
                        im.save(img_buf, format="JPEG", quality=85)
                        crop_found = True
                        break
                except Exception:
                    pass

    if not crop_found:
        im = PILImage.new("RGB", target_size, bg_color)
        draw = ImageDraw.Draw(im)
        draw.ellipse([(2, 2), (target_size[0] - 3, target_size[1] - 3)], outline=ring_color, width=3)
        draw.ellipse([(20, 12), (44, 34)], fill=ring_color)
        draw.pieslice([(12, 34), (52, 68)], start=180, end=360, fill=ring_color)
        pid = person_id or "P"
        short_id = pid if len(pid) <= 3 else pid[-3:]
        try:
            draw.text((target_size[0] - 22, 4), short_id, fill=(30, 41, 59))
        except Exception:
            pass
        im.save(img_buf, format="PNG")

    img_buf.seek(0)
    return RLImage(img_buf, width=size[0], height=size[1])


def generate_date_wise_report_pdf(
    target_date=None,
    start_date=None,
    end_date=None,
    bus_id=None,
    journey_id=None,
    person_id=None,
    canonical_id=None,
    angle=None,
    filter_status=None
):
    """
    Generates an official landscape A4 PDF report with:
    - Transit system header banner
    - Filter & telemetry metrics summary
    - EMBEDDED PASSENGER FACE PHOTOS / CROPS in observation tables
    - Date-wise sections with Angle A, Angle B, Angle C detection tables
    - Canonical Master IDs and merge provenance notes
    - Biometric vector privacy safeguards disclaimer
    """
    report = get_date_wise_passenger_report(
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

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=24,
        rightMargin=24,
        topMargin=24,
        bottomMargin=24
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=15,
        leading=18,
        textColor=colors.HexColor("#1e3a8a")
    )
    subtitle_style = ParagraphStyle(
        "DocSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#64748b")
    )
    meta_style = ParagraphStyle(
        "DocMeta",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#334155")
    )
    date_header_style = ParagraphStyle(
        "DateSectionHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
        textColor=colors.HexColor("#0f172a")
    )
    head_style = ParagraphStyle(
        "THead",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=9.5,
        textColor=colors.white
    )
    cell_style = ParagraphStyle(
        "TableCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor("#1e293b")
    )
    cell_bold = ParagraphStyle(
        "TableCellBold",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor("#0f172a")
    )
    alias_style = ParagraphStyle(
        "TableAlias",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7,
        leading=8.5,
        textColor=colors.HexColor("#b45309")
    )
    master_style = ParagraphStyle(
        "TableMaster",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7,
        leading=8.5,
        textColor=colors.HexColor("#047857")
    )
    disclaimer_style = ParagraphStyle(
        "Disclaimer",
        parent=styles["Normal"],
        fontName="Helvetica-Oblique",
        fontSize=7,
        leading=9,
        textColor=colors.HexColor("#94a3b8")
    )

    story = []

    # 1. Header Banner
    header_data = [
        [
            Paragraph("<b>SMARTBUS AI — DATE-WISE MULTI-ANGLE PASSENGER REPORT</b>", title_style),
            Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}<br/>Confidential Transit Record", meta_style)
        ],
        [
            Paragraph("Multi-Angle Camera Attendance &bull; Verified Face Crops &bull; Canonical Identity Resolution", subtitle_style),
            Paragraph(f"Total Date Sections: {len(report.get('date_groups', []))}", meta_style)
        ]
    ]
    t_head = Table(header_data, colWidths=[560, 230])
    t_head.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
    ]))
    story.append(t_head)
    story.append(Spacer(1, 6))

    # 2. Disclaimer & Biometric Protection notice
    story.append(Paragraph(
        "<b>DATA PRIVACY NOTICE:</b> Biometric feature vectors are strictly safeguarded in encrypted memory and never exported. "
        "Passenger crop images displayed below correspond directly to verified camera observations. Observations from Angle A, B, and C are independently logged.",
        disclaimer_style
    ))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#cbd5e1"), spaceBefore=2, spaceAfter=8))

    date_groups = report.get("date_groups", [])
    if not date_groups:
        story.append(Paragraph("<b>No passenger detections found for the specified filter criteria.</b>", cell_style))
        doc.build(story)
        buf.seek(0)
        return buf.getvalue()

    # Column widths for landscape A4 (Total = 790pt)
    col_widths = [34, 52, 48, 64, 76, 70, 80, 96, 48, 48, 74, 100]

    for date_idx, grp in enumerate(date_groups):
        s = grp["summary"]
        d_str = grp["date_formatted"]
        iso_date = grp["date"]

        # Date Section Banner
        date_banner_text = (
            f"<b>DATE: {d_str.upper()} ({iso_date})</b> &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"Observations: {s['total_observations']} &nbsp;&bull;&nbsp; "
            f"Unique Master Passengers: {s['total_unique_passengers']} &nbsp;&bull;&nbsp; "
            f"Angle A: {s['angle_a_count']} &nbsp;&bull;&nbsp; "
            f"Angle B: {s['angle_b_count']} &nbsp;&bull;&nbsp; "
            f"Angle C: {s['angle_c_count']}" +
            (f" &nbsp;&bull;&nbsp; Merged: {s['merged_count']}" if s['merged_count'] > 0 else "")
        )

        banner_table = Table([[Paragraph(date_banner_text, date_header_style)]], colWidths=[790])
        banner_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#f1f5f9")),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor("#94a3b8")),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ]))
        story.append(banner_table)
        story.append(Spacer(1, 4))

        # 8 Canonical Transit Identity Statistics Table
        metrics_data = [
            [
                Paragraph(f"<b>Total Passenger Observations:</b> {s.get('total_passenger_observations', s.get('total_observations', 0))}", cell_style),
                Paragraph(f"<b>Total Unique Passengers:</b> {s.get('total_unique_passengers', 0)}", cell_style),
                Paragraph(f"<b>Total Repeated Observations:</b> {s.get('total_repeated_observations', 0)}", cell_style),
                Paragraph(f"<b>Total New Passengers:</b> {s.get('total_new_passengers', 0)}", cell_style),
            ],
            [
                Paragraph(f"<b>Total Matched Passengers:</b> {s.get('total_matched_passengers', 0)}", cell_style),
                Paragraph(f"<b>Total Uncertain Identities:</b> {s.get('total_uncertain_identities', s.get('needs_review_count', 0))}", cell_style),
                Paragraph(f"<b>Total Identity Conflicts:</b> {s.get('total_identity_conflicts', 0)}", cell_style),
                Paragraph(f"<b>Total Manual Reviews:</b> {s.get('total_manual_reviews', s.get('needs_review_count', 0))}", cell_style),
            ]
        ]
        metrics_table = Table(metrics_data, colWidths=[197, 197, 198, 198])
        metrics_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ]))
        story.append(metrics_table)
        story.append(Spacer(1, 6))

        unique_list = grp.get("unique_passengers", [])
        if not unique_list:
            story.append(Paragraph("<i>No unique passengers recorded on this date.</i>", cell_style))
            story.append(Spacer(1, 10))
            continue

        table_rows = [
            [
                Paragraph("<b>#</b>", head_style),
                Paragraph("<b>Photo</b>", head_style),
                Paragraph("<b>Person ID</b>", head_style),
                Paragraph("<b>Status</b>", head_style),
                Paragraph("<b>First In (Boarding)</b>", head_style),
                Paragraph("<b>Last Seen (Exit)</b>", head_style),
                Paragraph("<b>Presence Timeline (Occurrences Across Times)</b>", head_style),
                Paragraph("<b>Seat & Bus</b>", head_style),
                Paragraph("<b>Frames & Angles</b>", head_style),
            ]
        ]

        col_widths = [26, 34, 65, 65, 85, 85, 245, 95, 92]

        for u in unique_list:
            face_img = _get_obs_face_flowable(
                crop_path=u.get("best_crop_url"),
                person_id=u.get("person_id"),
                is_merged=u.get("is_merged", False),
                status=u.get("status", "PRESENT")
            )

            # Build Presence Timeline text showing all times & angles this person was present
            occ_items = []
            for occ in u.get("presence_occurrences", [])[:5]:
                orig_note = f" (orig: {occ['original_person_id']})" if occ.get("original_person_id") and occ["original_person_id"] != u["person_id"] else ""
                occ_items.append(f"&bull; <b>{occ['time']}</b> [{occ['angle']}] ({occ['det_conf_pct']}% Det){orig_note}")
            if len(u.get("presence_occurrences", [])) > 5:
                occ_items.append(f"<i>+{len(u['presence_occurrences']) - 5} more occurrences</i>")
            
            occ_text = "<br/>".join(occ_items)
            if u.get("total_detections", 1) > 1:
                occ_text += f"<br/><font color='#0284c7' size='6.5'><b>Confirmed Present {u['total_detections']} Times ({u['angles_str']})</b></font>"

            table_rows.append([
                Paragraph(f"<b>{u.get('sequence_no', '-')}</b>", cell_bold),
                face_img,
                Paragraph(f"<b>{u.get('person_id', 'N/A')}</b><br/><font color='#047857' size='6.5'>Unique Master</font>", cell_style),
                Paragraph(f"<b>{u.get('status', 'PRESENT')}</b>", cell_style),
                Paragraph(f"<b>{u.get('first_seen_time', 'N/A')}</b><br/><font color='#2563eb' size='6.5'>{u.get('first_angle', '')}</font>", cell_style),
                Paragraph(f"<b>{u.get('last_seen_time', 'N/A')}</b><br/><font color='#047857' size='6.5'>{u.get('last_angle', '')}</font>", cell_style),
                Paragraph(occ_text, cell_style),
                Paragraph(f"Seat: <b>{u.get('current_seat', 'N/A')}</b><br/>{u.get('bus_number', 'N/A')}<br/>{u.get('journey_code', 'N/A')}", cell_style),
                Paragraph(f"<b>{u.get('total_detections', 1)} frame(s)</b><br/>{u.get('angles_str', 'N/A')}", cell_style),
            ])

        obs_table = Table(table_rows, colWidths=col_widths, repeatRows=1)
        obs_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ]))

        story.append(obs_table)
        story.append(Spacer(1, 14))

    doc.build(story)
    buf.seek(0)
    return buf.getvalue()


def get_identity_provenance(person_id, journey_id=None):
    """
    Returns full provenance and transition history for a given Person ID:
    - Original identity details
    - If merged: previous ID, new master ID, merger timestamp, reviewer, rationale, and observation count
    - If master: list of alias IDs merged into it
    - Full transition flow (previous -> new)
    """
    clean_pid = str(person_id).strip()
    q = PassengerIdentity.query.filter_by(person_id=clean_pid)
    if journey_id:
        try:
            q = q.filter(PassengerIdentity.journey_id == int(journey_id))
        except (ValueError, TypeError):
            pass
    identity = q.order_by(PassengerIdentity.is_merged.desc(), PassengerIdentity.id.desc()).first()

    # Search merge records where this person is either master or merged alias
    m_alias_q = PassengerMergeRecord.query.filter_by(merged_person_id=clean_pid)
    m_master_q = PassengerMergeRecord.query.filter_by(master_person_id=clean_pid)
    if journey_id:
        try:
            m_alias_q = m_alias_q.filter(PassengerMergeRecord.journey_id == int(journey_id))
            m_master_q = m_master_q.filter(PassengerMergeRecord.journey_id == int(journey_id))
        except (ValueError, TypeError):
            pass

    merge_as_alias = m_alias_q.order_by(desc(PassengerMergeRecord.merge_timestamp)).all()
    merge_as_master = m_master_q.order_by(desc(PassengerMergeRecord.merge_timestamp)).all()

    # Check active aliases merged into this master
    active_aliases = []
    if identity:
        aliases_objs = PassengerIdentity.query.filter_by(canonical_identity_id=identity.id, is_merged=True).all()
        active_aliases = [a.person_id for a in aliases_objs]

    # Also include aliases from active merge records
    for rec in merge_as_master:
        if rec.status == "ACTIVE" and rec.merged_person_id not in active_aliases:
            active_aliases.append(rec.merged_person_id)

    # Determine canonical master
    canonical_person_id = clean_pid
    is_merged = False
    master_identity = None

    if identity and (identity.is_merged or identity.canonical_identity_id):
        canonical_person_id = identity.get_canonical_person_id()
        is_merged = True
        if identity.canonical_identity_id:
            master_identity = PassengerIdentity.query.get(identity.canonical_identity_id)
    elif merge_as_alias:
        rec = merge_as_alias[0]
        if rec.status == "ACTIVE":
            is_merged = True
            canonical_person_id = rec.master_person_id

    # Format merge transitions
    transitions = []
    for rec in merge_as_alias:
        transitions.append({
            "record_id": rec.id,
            "type": "MERGED_INTO_MASTER",
            "previous_person_id": rec.merged_person_id,
            "new_master_person_id": rec.master_person_id,
            "status": rec.status,
            "merge_timestamp": rec.merge_timestamp.strftime("%Y-%m-%d %H:%M:%S") if rec.merge_timestamp else None,
            "operator_name": rec.merged_by.full_name if rec.merged_by else "System Administrator",
            "reason": rec.merge_reason or "Manual multi-angle identity consolidation",
            "audit_reference": rec.audit_reference,
            "reversed_at": rec.reversed_at.strftime("%Y-%m-%d %H:%M:%S") if rec.reversed_at else None,
            "reversal_reason": rec.reversal_reason
        })

    for rec in merge_as_master:
        transitions.append({
            "record_id": rec.id,
            "type": "ACCEPTED_ALIAS",
            "previous_person_id": rec.merged_person_id,
            "new_master_person_id": rec.master_person_id,
            "status": rec.status,
            "merge_timestamp": rec.merge_timestamp.strftime("%Y-%m-%d %H:%M:%S") if rec.merge_timestamp else None,
            "operator_name": rec.merged_by.full_name if rec.merged_by else "System Administrator",
            "reason": rec.merge_reason or "Manual multi-angle identity consolidation",
            "audit_reference": rec.audit_reference,
            "reversed_at": rec.reversed_at.strftime("%Y-%m-%d %H:%M:%S") if rec.reversed_at else None,
            "reversal_reason": rec.reversal_reason
        })

    thumb_url = None
    if identity and identity.thumbnail_path:
        thumb_url = resolve_passenger_image_url(identity.thumbnail_path, clean_pid)
    elif master_identity and master_identity.thumbnail_path:
        thumb_url = resolve_passenger_image_url(master_identity.thumbnail_path, canonical_person_id)

    return {
        "person_id": clean_pid,
        "canonical_person_id": canonical_person_id,
        "is_merged": is_merged,
        "status": identity.status if identity else ("MERGED_ARCHIVED" if is_merged else "UNKNOWN"),
        "thumbnail_url": thumb_url,
        "active_aliases": list(set(active_aliases)),
        "transitions": transitions,
        "summary": {
            "previous_id": clean_pid if is_merged else None,
            "new_id": canonical_person_id if is_merged else clean_pid,
            "change_type": "MERGED_ALIAS" if is_merged else ("MASTER_WITH_ALIASES" if active_aliases else "STANDALONE_MASTER"),
            "display_text": f"Previous Person ID: {clean_pid} ➔ Merged into New Master ID: {canonical_person_id}" if is_merged else (
                f"Master Person ID: {clean_pid} (Consolidated from aliases: {', '.join(active_aliases)})" if active_aliases else f"Master Person ID: {clean_pid} (No merged aliases)"
            )
        }
    }

