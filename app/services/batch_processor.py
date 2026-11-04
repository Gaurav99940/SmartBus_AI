import os
import uuid
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

from app.config import Config
from app.database import db
from app.models import (
    Journey, SourceFolder, SourceMediaFile, TimetableImport,
    TimetableStop, StopMediaMatch, StopProcessingJob, Upload,
    PassengerObservation, PassengerIdentity, PresenceEvent
)
from app.services.media_service import (
    extract_frame_at_timestamp, extract_media_metadata,
    parse_stop_number_from_filename
)
from app.services.journey_service import process_multi_angle_batch
from app.services.audit_service import log_audit


def resolve_media_file_path(mf_or_path):
    """
    Resolves actual file path on disk across absolute, relative, or upload-directory structures.
    """
    if not mf_or_path:
        return None
    p = mf_or_path if isinstance(mf_or_path, str) else getattr(mf_or_path, "file_path", None)
    if not p:
        return None
    if os.path.exists(p):
        return p
    norm_p = os.path.normpath(p)
    if os.path.exists(norm_p):
        return norm_p
    base = os.path.basename(p)
    upload_dir = str(getattr(Config, "UPLOAD_FOLDER", "uploads"))
    in_upload = os.path.join(upload_dir, base)
    if os.path.exists(in_upload):
        return in_upload
    try:
        for root, _, files in os.walk(upload_dir):
            if base in files:
                return os.path.join(root, base)
    except Exception:
        pass
    cwd_path = os.path.join(os.getcwd(), p)
    if os.path.exists(cwd_path):
        return cwd_path
    return p


def project_media_time(mf, window_start, window_end=None):
    """
    Projects a media file's timestamp onto the target window date when dates differ,
    preserving duration and time-of-day.
    """
    if not mf or not mf.start_time:
        return None, None

    if window_end is None:
        window_end = window_start

    duration = (mf.end_time - mf.start_time) if mf.end_time else timedelta(0)

    if mf.start_time.date() == window_start.date():
        return mf.start_time, mf.start_time + duration

    t_start = mf.start_time.time()
    proj_start = datetime.combine(window_start.date(), t_start)
    if window_end.date() > window_start.date() and proj_start < window_start:
        proj_start = datetime.combine(window_end.date(), t_start)

    return proj_start, proj_start + duration


def find_matching_media_for_window(media_files, window_start, window_end, max_increment_minutes=None, step_minutes=None, stop_number=None, stop_name=None, return_details=False):
    """
    Finds matching media clip/image for [window_start, window_end].
    Stage 0: Explicit filename match (Stop # or Stop Name in filename).
    Stage 1: Direct window overlap (exact date or projected time-of-day).
    Stage 2: Incremental fallback search using 1-minute search steps up to max_increment_minutes.
    Stage 3: Nearest clip within max_increment_minutes.
    Stage 4: Broad route fallback (closest clip in angle queue).
    Returns (media_file, used_step) by default, or (media_file, used_step, match_type) if return_details=True.
    """
    if not media_files or not window_start or not window_end:
        return (None, 0, "NO_MEDIA") if return_details else (None, 0)

    if step_minutes is None:
        step_minutes = getattr(Config, "TIMETABLE_FALLBACK_STEP_MINUTES", 1)
    if max_increment_minutes is None:
        max_increment_minutes = getattr(Config, "TIMETABLE_MAX_FALLBACK_MINUTES", 30)

    mid_window = window_start + (window_end - window_start) / 2

    # Filter usable candidates & resolve paths
    valid_candidates = []
    for mf in media_files:
        if mf.status == "error" or not mf.start_time:
            continue
        resolved = resolve_media_file_path(mf)
        if resolved:
            mf.file_path = resolved
        valid_candidates.append(mf)

    if not valid_candidates:
        return (None, 0, "NO_MEDIA") if return_details else (None, 0)

    # -------------------------------------------------------------
    # Stage 0: Explicit Filename Match (Stop Number or Stop Name)
    # -------------------------------------------------------------
    if stop_number is not None:
        stop_num_matches = []
        for mf in valid_candidates:
            f_stop = parse_stop_number_from_filename(mf.filename)
            if f_stop is None and mf.file_path:
                f_stop = parse_stop_number_from_filename(mf.file_path)
            if f_stop == stop_number:
                proj_start, _ = project_media_time(mf, window_start, window_end)
                time_diff = abs((proj_start - mid_window).total_seconds()) if proj_start else 0.0
                stop_num_matches.append((time_diff, mf))

        if stop_num_matches:
            stop_num_matches.sort(key=lambda x: (1 if x[1].is_color is False else 0, x[0]))
            best_mf = stop_num_matches[0][1]
            return (best_mf, 0, "FILENAME_STOP_INDEX") if return_details else (best_mf, 0)

    if stop_name:
        import re
        norm_stop_name = re.sub(r'[^a-z0-9]', '', str(stop_name).lower())
        if len(norm_stop_name) >= 3:
            stop_name_matches = []
            for mf in valid_candidates:
                norm_fn = re.sub(r'[^a-z0-9]', '', str(mf.filename).lower())
                if norm_stop_name in norm_fn:
                    proj_start, _ = project_media_time(mf, window_start, window_end)
                    time_diff = abs((proj_start - mid_window).total_seconds()) if proj_start else 0.0
                    stop_name_matches.append((time_diff, mf))
            if stop_name_matches:
                stop_name_matches.sort(key=lambda x: (1 if x[1].is_color is False else 0, x[0]))
                best_mf = stop_name_matches[0][1]
                return (best_mf, 0, "FILENAME_STOP_NAME") if return_details else (best_mf, 0)

    # -------------------------------------------------------------
    # Stage 1: Direct Window Overlap (Exact Date or Projected Time-of-Day)
    # -------------------------------------------------------------
    overlap_candidates = []
    for mf in valid_candidates:
        proj_start, proj_end = project_media_time(mf, window_start, window_end)
        if not proj_start or not proj_end:
            continue
        if proj_start <= window_end and proj_end >= window_start:
            overlap_start = max(proj_start, window_start)
            overlap_end = min(proj_end, window_end)
            overlap_seconds = max(0.0, (overlap_end - overlap_start).total_seconds())
            mid_media = proj_start + (proj_end - proj_start) / 2
            time_diff = abs((mid_media - mid_window).total_seconds())
            is_same_date = (mf.start_time.date() == window_start.date())
            overlap_candidates.append({
                "media_file": mf,
                "overlap_seconds": overlap_seconds,
                "time_diff": time_diff,
                "is_same_date": is_same_date,
                "is_reliable": mf.is_timestamp_reliable
            })

    if overlap_candidates:
        overlap_candidates.sort(key=lambda c: (
            0 if c["is_same_date"] else 1,
            1 if c["media_file"].is_color is False else 0,
            -c["overlap_seconds"],
            c["time_diff"],
            0 if c["is_reliable"] else 1
        ))
        best = overlap_candidates[0]
        mtype = "WINDOW_SYNC" if best["is_same_date"] else "TIME_OF_DAY_SYNC"
        return (best["media_file"], 0, mtype) if return_details else (best["media_file"], 0)

    # -------------------------------------------------------------
    # Stage 2: Incremental Fallback Search (1-minute steps up to max_increment_minutes)
    # -------------------------------------------------------------
    max_steps = max(1, int(max_increment_minutes // step_minutes))
    for step_idx in range(1, max_steps + 1):
        delta_mins = min(step_idx * step_minutes, max_increment_minutes)
        expanded_start = window_start - timedelta(minutes=delta_mins)
        expanded_end = window_end + timedelta(minutes=delta_mins)

        step_candidates = []
        for mf in valid_candidates:
            proj_start, proj_end = project_media_time(mf, window_start, window_end)
            if not proj_start or not proj_end:
                continue
            if proj_start <= expanded_end and proj_end >= expanded_start:
                mid_media = proj_start + (proj_end - proj_start) / 2
                time_diff = abs((mid_media - mid_window).total_seconds())
                is_same_date = (mf.start_time.date() == window_start.date())
                step_candidates.append({
                    "media_file": mf,
                    "time_diff": time_diff,
                    "delta_mins": delta_mins,
                    "is_same_date": is_same_date,
                    "is_reliable": mf.is_timestamp_reliable
                })

        if step_candidates:
            step_candidates.sort(key=lambda c: (
                0 if c["is_same_date"] else 1,
                1 if c["media_file"].is_color is False else 0,
                c["time_diff"],
                0 if c["is_reliable"] else 1
            ))
            best = step_candidates[0]
            mtype = "WINDOW_INCREMENT" if best["is_same_date"] else "TIME_OF_DAY_INCREMENT"
            return (best["media_file"], delta_mins, mtype) if return_details else (best["media_file"], delta_mins)

    # -------------------------------------------------------------
    # Stage 3: Nearest clip within max_increment_minutes
    # -------------------------------------------------------------
    near_candidates = []
    for mf in valid_candidates:
        proj_start, proj_end = project_media_time(mf, window_start, window_end)
        if not proj_start:
            continue
        mid_media = proj_start + (proj_end - proj_start) / 2
        time_diff = abs((mid_media - mid_window).total_seconds())
        if time_diff <= (max_increment_minutes * 60):
            is_same_date = (mf.start_time.date() == window_start.date())
            near_candidates.append({
                "media_file": mf,
                "time_diff": time_diff,
                "is_same_date": is_same_date,
                "is_reliable": mf.is_timestamp_reliable
            })

    if near_candidates:
        near_candidates.sort(key=lambda c: (
            0 if c["is_same_date"] else 1,
            1 if c["media_file"].is_color is False else 0,
            c["time_diff"],
            0 if c["is_reliable"] else 1
        ))
        best = near_candidates[0]
        used_step = max(1, int(round(best["time_diff"] / 60.0)))
        return (best["media_file"], used_step, "NEAREST_CLIP") if return_details else (best["media_file"], used_step)

    # -------------------------------------------------------------
    # Stage 4: Broad Route Fallback (Closest clip in queue)
    # -------------------------------------------------------------
    broad_candidates = []
    for mf in valid_candidates:
        proj_start, proj_end = project_media_time(mf, window_start, window_end)
        if not proj_start:
            continue
        mid_media = proj_start + (proj_end - proj_start) / 2
        time_diff = abs((mid_media - mid_window).total_seconds())
        is_same_date = (mf.start_time.date() == window_start.date())
        broad_candidates.append({
            "media_file": mf,
            "time_diff": time_diff,
            "is_same_date": is_same_date,
            "is_reliable": mf.is_timestamp_reliable
        })

    if broad_candidates:
        broad_candidates.sort(key=lambda c: (
            0 if c["is_same_date"] else 1,
            1 if c["media_file"].is_color is False else 0,
            c["time_diff"]
        ))
        best = broad_candidates[0]
        used_step = max(1, int(round(best["time_diff"] / 60.0)))
        return (best["media_file"], used_step, "ROUTE_NEAREST_CLIP") if return_details else (best["media_file"], used_step)

    return (None, 0, "NO_MEDIA") if return_details else (None, 0)


def find_media_near_time_incrementally(media_files, anchor_time, max_increment_minutes=None, step_minutes=None):
    """
    Inter-angle synchronization helper:
    When one camera angle has a clip at anchor_time, but another camera angle
    has no clip at that exact time, searches incrementally in steps to pick the next closest available clip.
    Handles exact date matching and projected time-of-day matching.
    """
    if not media_files or not anchor_time:
        return None, 0

    if step_minutes is None:
        step_minutes = getattr(Config, "TIMETABLE_FALLBACK_STEP_MINUTES", 1)
    if max_increment_minutes is None:
        max_increment_minutes = getattr(Config, "TIMETABLE_MAX_FALLBACK_MINUTES", 30)

    candidates = []
    for mf in media_files:
        if mf.status == "error" or not mf.start_time:
            continue
        resolved = resolve_media_file_path(mf)
        if resolved:
            mf.file_path = resolved

        # Try exact datetime diff
        diff = abs((mf.start_time - anchor_time).total_seconds())
        # Also try projected time-of-day diff
        proj_start, _ = project_media_time(mf, anchor_time)
        diff_proj = abs((proj_start - anchor_time).total_seconds()) if proj_start else diff

        is_same_date = (mf.start_time.date() == anchor_time.date())
        best_diff = diff if is_same_date else diff_proj

        candidates.append((
            0 if is_same_date else 1,
            1 if mf.is_color is False else 0,
            best_diff,
            mf
        ))

    if candidates:
        candidates.sort(key=lambda x: (x[0], x[1], x[2]))
        best_mf = candidates[0][3]
        min_diff = candidates[0][2]
        inc = max(1, int(round(min_diff / 60.0)))
        return best_mf, inc

    return None, 0


def match_stops_to_media(timetable_id):
    """
    For every stop in the timetable, automatically searches discovered Angle A, B, C media
    and creates StopMediaMatch records.
    Supports robust folder linking, window matching, and cross-angle 1-minute incremental search.
    """
    timetable = TimetableImport.query.get(timetable_id)
    if not timetable:
        return False, "Timetable not found"

    stops = TimetableStop.query.filter_by(timetable_id=timetable_id).order_by(TimetableStop.stop_number).all()
    if not stops:
        return False, "Timetable has no stops"

    # Get media files grouped by angle with robust multi-folder pooling
    media_by_angle = {"A": [], "B": [], "C": []}
    for angle in ["A", "B", "C"]:
        query = SourceMediaFile.query.filter(
            SourceMediaFile.camera_angle == angle,
            SourceMediaFile.status != "error"
        )
        if timetable.journey_id:
            query = query.filter(
                (SourceMediaFile.journey_id == timetable.journey_id) |
                (SourceMediaFile.journey_id.is_(None))
            )
        all_candidates = query.order_by(SourceMediaFile.start_time).all()

        # If no candidates under current journey, pool across all journeys as fallback
        if not all_candidates:
            all_candidates = SourceMediaFile.query.filter(
                SourceMediaFile.camera_angle == angle,
                SourceMediaFile.status != "error"
            ).order_by(SourceMediaFile.start_time).all()

        # Resolve paths & prioritize existing files on disk
        valid_candidates = []
        for mf in all_candidates:
            resolved = resolve_media_file_path(mf)
            if resolved:
                mf.file_path = resolved
            valid_candidates.append(mf)

        existing_files = [f for f in valid_candidates if f.file_path and os.path.exists(f.file_path)]
        files = existing_files if existing_files else valid_candidates

        # If timetable has a journey_id, associate these files to that journey
        if timetable.journey_id and files:
            for f in files:
                if f.journey_id is None or f.journey_id != timetable.journey_id:
                    f.journey_id = timetable.journey_id

        media_by_angle[angle] = files

    total_matched = 0

    for stop in stops:
        # Delete existing matches for fresh match
        StopMediaMatch.query.filter_by(stop_id=stop.id).delete()

        w_start = stop.window_start
        w_end = stop.window_end
        if not w_start or not w_end:
            continue

        matched_results = {}  # angle -> (matched_mf, inc_used, match_type)

        # Pass 1: Try stop window matching for each angle with 1-minute increments & filename intelligence
        for angle in ["A", "B", "C"]:
            matched_mf, inc_used, m_type = find_matching_media_for_window(
                media_by_angle[angle],
                w_start,
                w_end,
                max_increment_minutes=getattr(Config, "TIMETABLE_MAX_FALLBACK_MINUTES", 30),
                stop_number=stop.stop_number,
                stop_name=stop.stop_name,
                return_details=True
            )
            if matched_mf:
                matched_results[angle] = (matched_mf, inc_used, m_type)

        # Pass 2: Inter-angle incremental synchronization
        # If some angles found a clip (e.g. Angle A & B found, but Angle C is missing in window):
        # Anchor to the found clip's timestamp and increment in 1-minute steps to find the next clip for the missing angle.
        if 0 < len(matched_results) < 3:
            # Pick anchor time from the best matched angle clip
            anchor_mf = next(iter(matched_results.values()))[0]
            anchor_time = anchor_mf.start_time

            for missing_angle in ["A", "B", "C"]:
                if missing_angle not in matched_results:
                    mf_found, inc_used = find_media_near_time_incrementally(
                        media_by_angle[missing_angle],
                        anchor_time,
                        max_increment_minutes=getattr(Config, "TIMETABLE_MAX_FALLBACK_MINUTES", 30)
                    )
                    if mf_found:
                        matched_results[missing_angle] = (mf_found, inc_used, "INTER_ANGLE_SYNC")

        # Pass 3: Chronological sequence assignment for any angle still missing when files exist
        for missing_angle in ["A", "B", "C"]:
            if missing_angle not in matched_results and media_by_angle[missing_angle]:
                angle_files = media_by_angle[missing_angle]
                stop_idx = (stop.stop_number - 1) if (stop.stop_number and stop.stop_number > 0) else 0
                chosen_mf = angle_files[stop_idx % len(angle_files)]
                matched_results[missing_angle] = (chosen_mf, 0, "CHRONOLOGICAL_SEQUENCE_SYNC")

        # Create DB match records
        angles_present = []
        for angle in ["A", "B", "C"]:
            if angle in matched_results:
                matched_mf, inc_used, m_type = matched_results[angle]
                mid_window = w_start + (w_end - w_start) / 2
                proj_start, _ = project_media_time(matched_mf, w_start, w_end)
                mid_mf = proj_start or matched_mf.start_time
                diff_sec = abs((mid_mf - mid_window).total_seconds()) if mid_mf else 0.0

                inc_desc = f" (+{inc_used}m increment)" if inc_used > 0 else ""
                reason = f"{m_type}{inc_desc} (diff: {round(diff_sec, 1)}s)"

                # Fast non-blocking color status check
                is_col = True if matched_mf.is_color is not False else False
                m_status = "matched" if is_col else "skipped_black_and_white"
                skip_msg = "Black-and-white clip skipped" if not is_col else None

                match = StopMediaMatch(
                    stop_id=stop.id,
                    camera_angle=angle,
                    source_media_file_id=matched_mf.id,
                    source_filename=matched_mf.filename,
                    source_folder=matched_mf.file_path,
                    media_type=matched_mf.media_type,
                    capture_timestamp=matched_mf.start_time,
                    selected_frame_timestamp=mid_window,
                    match_reason=reason,
                    time_diff_seconds=diff_sec,
                    status=m_status,
                    is_color=is_col,
                    color_score=matched_mf.color_score,
                    skip_reason=skip_msg,
                    reuse_count=matched_mf.reuse_count or 0
                )
                db.session.add(match)
                angles_present.append(angle)
                total_matched += 1
            else:
                match = StopMediaMatch(
                    stop_id=stop.id,
                    camera_angle=angle,
                    status="missing",
                    match_reason="NO_MEDIA_FOUND_IN_WINDOW"
                )
                db.session.add(match)

        # Update stop coverage status
        missing = [a for a in ["A", "B", "C"] if a not in angles_present]
        stop.missing_angles = ", ".join(missing) if missing else "None"
        if len(angles_present) == 3:
            stop.coverage_status = "FULLY_COVERED"
        elif len(angles_present) > 0:
            stop.coverage_status = "PARTIALLY_COVERED"
        else:
            stop.coverage_status = "NOT_COVERED"

    db.session.commit()
    return True, f"Successfully matched {total_matched} angle clips across {len(stops)} stops."


def process_single_stop(stop_id, user_id=None):
    """
    Executes the multi-angle passenger detection pipeline for a single stop:
    1. Extracts or references frame/image for Angles A, B, C.
    2. Invokes process_multi_angle_batch with the 3 angles.
    3. Records stop counts and coverage status.
    """
    stop = TimetableStop.query.get(stop_id)
    if not stop:
        return {"success": False, "error": f"Stop #{stop_id} not found"}

    timetable = stop.timetable
    journey = Journey.query.get(timetable.journey_id) if (timetable and timetable.journey_id) else None
    if not journey:
        journey = Journey.query.filter(Journey.status.in_(["active", "in_progress"])).order_by(Journey.id.desc()).first() or Journey.query.order_by(Journey.id.desc()).first()
        if not journey:
            from app.models import Bus
            bus = Bus.query.first()
            if not bus:
                bus = Bus(bus_number="BUS-01", registration_number="DL-01-A-1001", total_capacity=40)
                db.session.add(bus)
                db.session.flush()
            journey = Journey(bus_id=bus.id, status="in_progress", start_time=datetime.utcnow())
            db.session.add(journey)
            db.session.flush()
        if timetable:
            timetable.journey_id = journey.id
            db.session.commit()

    matches = {m.camera_angle: m for m in stop.matches}
    angle_uploads = {}
    batch_id = f"stop_{stop.stop_number}_{uuid.uuid4().hex[:6]}"

    stop.processing_status = "processing"
    db.session.commit()

    try:
        from app.ai.color_filter import evaluate_media_color_eligibility

        for angle in ["A", "B", "C"]:
            match = matches.get(angle)
            if not match or match.status == "missing" or not match.source_media_file:
                continue

            mf = match.source_media_file
            frame_abs_path = None
            cap_time = match.capture_timestamp or datetime.utcnow()

            # Pre-filter: Automatically detect and skip black-and-white / grayscale footage
            color_eval = evaluate_media_color_eligibility(mf.file_path)
            match.is_color = color_eval["is_color"]
            match.color_score = color_eval["color_score"]

            if not color_eval["is_color"]:
                match.status = "skipped_black_and_white"
                match.skip_reason = color_eval["skip_reason"]
                print(f"[BatchProcessor] Skipping Angle {angle} at Stop #{stop.stop_number}: {color_eval['skip_reason']}")
                continue

            if mf.media_type == "video":
                # Extract frame at selected timestamp
                target_ts = match.selected_frame_timestamp or stop.scheduled_arrival or mf.start_time
                extracted, err = extract_frame_at_timestamp(
                    video_path=mf.file_path,
                    target_timestamp=target_ts,
                    video_start_time=mf.start_time
                )
                if err or not extracted:
                    match.status = "failed"
                    match.error_message = f"Frame extraction error: {err}"
                    continue

                frame_abs_path = extracted["frame_path"]
                match.extracted_frame_path = frame_abs_path
                match.frame_number = extracted["frame_number"]
            else:
                # Image file: use directly
                frame_abs_path = mf.file_path

            mf.reuse_count = (mf.reuse_count or 0) + 1
            match.reuse_count = mf.reuse_count

            # Create an Upload record so it seamlessly plugs into the existing multi-angle AI pipeline
            up = Upload(
                journey_id=journey.id,
                bus_id=journey.bus_id,
                camera_angle=angle,
                batch_id=batch_id,
                file_path=str(frame_abs_path),
                original_filename=mf.filename,
                file_size=mf.file_size,
                file_hash=mf.file_hash,
                capture_timestamp=cap_time,
                upload_timestamp=datetime.utcnow(),
                route_stop_id=stop.id,
                status="processing"
            )
            db.session.add(up)
            db.session.flush()

            match.upload_id = up.id
            angle_uploads[angle] = up

        db.session.commit()

        if not angle_uploads:
            # Check if any angle was skipped due to black-and-white
            bw_skipped = [m for m in stop.matches if m.status == "skipped_black_and_white"]
            if bw_skipped:
                stop.processing_status = "SKIPPED_BLACK_AND_WHITE"
                stop.coverage_status = "SKIPPED_BW"
                stop.error_message = f"All {len(bw_skipped)} camera angles were black-and-white / grayscale and skipped before AI processing. Passenger identities and attendance were preserved."
                db.session.commit()
                return {
                    "success": True,
                    "skipped": True,
                    "status": "SKIPPED_BLACK_AND_WHITE",
                    "message": stop.error_message
                }
            else:
                stop.processing_status = "failed"
                stop.coverage_status = "FAILED"
                stop.error_message = "No usable media could be extracted for any camera angle."
                db.session.commit()
                return {"success": False, "error": stop.error_message}

        # Run existing multi-angle AI pipeline with actual timetable stop ID
        result = process_multi_angle_batch(
            journey_id=journey.id,
            batch_id=batch_id,
            angle_uploads=angle_uploads,
            user_id=user_id,
            stop_id=stop.id
        )

        counts = result.get("consolidated_counts", {})
        summaries = result.get("angle_summaries", {})

        # Update match records with annotated paths and counts
        for angle, up in angle_uploads.items():
            m = matches.get(angle)
            if m:
                if up.status == "SKIPPED_BLACK_AND_WHITE":
                    m.status = "skipped_black_and_white"
                    m.is_color = False
                    m.color_score = up.color_score
                    m.skip_reason = up.skip_reason
                else:
                    m.status = "processed"
                    m.is_color = True
                    m.detection_count = summaries.get(angle, {}).get("detected", 0)
                    m.annotated_frame_path = up.annotated_path

        # Update stop-level observations and counts
        stop.angle_a_count = counts.get("angle_a_count", 0)
        stop.angle_b_count = counts.get("angle_b_count", 0)
        stop.angle_c_count = counts.get("angle_c_count", 0)
        stop.total_raw_detections = counts.get("total_raw_detections", 0)
        stop.potential_duplicates = counts.get("potential_duplicates", 0)
        stop.confirmed_identities = counts.get("confirmed_unique", 0)
        stop.unidentified_passengers = counts.get("uncertain_or_unidentified", 0)
        stop.needs_review_count = counts.get("passengers_requiring_review", 0)
        stop.processed_at = datetime.utcnow()

        # Stop-wise passenger occupancy & movement tracking
        # Boarded: newly boarded passengers whose first stop is this stop
        # Continuing: passengers who boarded at earlier stops and remain present
        # Exited: passengers whose exit is confirmed at this stop
        boarded_pids = set(
            p.id for p in journey.passengers
            if p.boarding_stop_number == stop.stop_number or p.boarding_stop_id == stop.id
        )
        continuing_pids = set(
            p.id for p in journey.passengers
            if (p.boarding_stop_number or 0) < stop.stop_number and p.status in ["PRESENT", "POSSIBLY_ABSENT"]
        )
        exited_pids = set(
            p.id for p in journey.passengers
            if (p.exit_stop_number == stop.stop_number or p.exit_stop_id == stop.id) and p.status == "EXIT_CONFIRMED"
        )
        stop.boarded_count = len(boarded_pids)
        stop.continuing_count = len(continuing_pids)
        stop.exited_count = len(exited_pids)
        stop.occupancy = len(boarded_pids) + len(continuing_pids)
        stop.estimated_unique_passengers = stop.occupancy

        # Update coverage status
        # 1. Fully Covered: All 3 angles present and successfully processed
        # 2. Partially Covered: 1 or 2 angles processed
        # 3. Not Covered: No angles
        # 4. Needs Review: needs_review_count > 0
        missing_angles = [a for a in ["A", "B", "C"] if a not in angle_uploads]
        stop.missing_angles = ", ".join(missing_angles) if missing_angles else "None"

        if len(angle_uploads) == 3:
            if stop.needs_review_count > 0:
                stop.coverage_status = "NEEDS_REVIEW"
            else:
                stop.coverage_status = "FULLY_COVERED"
        elif len(angle_uploads) > 0:
            stop.coverage_status = "PARTIALLY_COVERED"
        else:
            stop.coverage_status = "NOT_COVERED"

        stop.processing_status = "completed"
        db.session.commit()

        log_audit(
            action="PROCESS_STOP_MEDIA",
            entity_type="TimetableStop",
            entity_id=stop.id,
            details={
                "stop_number": stop.stop_number,
                "stop_name": stop.stop_name,
                "angles_processed": list(angle_uploads.keys()),
                "coverage_status": stop.coverage_status,
                "unique_passengers": stop.estimated_unique_passengers
            },
            user_id=user_id
        )

        return {"success": True, "stop": stop.to_dict(), "counts": counts}
    except Exception as e:
        stop.processing_status = "failed"
        stop.error_message = str(e)
        db.session.commit()
        return {"success": False, "error": str(e)}


# Global job registry for threading / background execution
_active_jobs = {}
_job_locks = {}


class JobWorker(threading.Thread):
    def __init__(self, app, job_id, user_id=None):
        super().__init__(daemon=True)
        self.app = app
        self.job_id = job_id
        self.user_id = user_id
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()

    def pause(self):
        self._pause_event.set()

    def resume(self):
        self._pause_event.clear()

    def cancel(self):
        self._stop_event.set()
        self._pause_event.clear()

    def run(self):
        with self.app.app_context():
            job = StopProcessingJob.query.get(self.job_id)
            if not job:
                return

            job.status = "running"
            job.started_at = datetime.utcnow()
            db.session.commit()

            stops = TimetableStop.query.filter_by(
                timetable_id=job.timetable_id
            ).order_by(TimetableStop.stop_number).all()

            job.total_stops = len(stops)

            # Ensure media matches exist across stops before processing
            if stops and StopMediaMatch.query.filter_by(stop_id=stops[0].id).count() == 0:
                match_stops_to_media(job.timetable_id)
                # Re-query stops with matches
                stops = TimetableStop.query.filter_by(
                    timetable_id=job.timetable_id
                ).order_by(TimetableStop.stop_number).all()

            # Count total eligible media files registered across angles
            mf_query = SourceMediaFile.query.filter(SourceMediaFile.status != "error")
            if job.journey_id:
                mf_query = mf_query.filter(
                    (SourceMediaFile.journey_id == job.journey_id) |
                    (SourceMediaFile.journey_id.is_(None))
                )
            total_mfs = mf_query.count()
            job.total_media_files = total_mfs
            job.processed_media_files = 0
            job.remaining_media_files = total_mfs
            job.ai_detection_status = "Initialized. Starting multi-angle stop-wise analysis..."
            db.session.commit()

            for idx, stop in enumerate(stops):
                # Check cancellation
                if self._stop_event.is_set():
                    job.status = "cancelled"
                    db.session.commit()
                    break

                # Check pause
                while self._pause_event.is_set():
                    if self._stop_event.is_set():
                        job.status = "cancelled"
                        db.session.commit()
                        return
                    job.status = "paused"
                    db.session.commit()
                    threading.Event().wait(1.0)

                job.status = "running"
                job.current_stop_index = idx + 1
                job.current_stop_number = stop.stop_number
                job.current_stop_name = stop.stop_name
                job.current_angle = "Sync (A, B, C)"
                
                # Identify media filenames for current stop
                cur_names = [m.source_filename for m in stop.matches if m.source_filename and m.status != "missing"]
                job.current_media_filename = ", ".join(cur_names) if cur_names else "Scanning Window"
                job.ai_detection_status = f"Analyzing Stop #{stop.stop_number} ({stop.stop_name}) with AI Pipeline"
                db.session.commit()

                # Process single stop
                process_single_stop(stop.id, user_id=self.user_id)

                # Refresh job stats
                job.processed_stops = TimetableStop.query.filter(
                    TimetableStop.timetable_id == job.timetable_id,
                    TimetableStop.processing_status.in_(["completed", "SKIPPED_BLACK_AND_WHITE"])
                ).count()
                job.failed_stops = TimetableStop.query.filter_by(
                    timetable_id=job.timetable_id,
                    processing_status="failed"
                ).count()
                job.fully_covered_stops = TimetableStop.query.filter_by(
                    timetable_id=job.timetable_id,
                    coverage_status="FULLY_COVERED"
                ).count()
                job.partially_covered_stops = TimetableStop.query.filter_by(
                    timetable_id=job.timetable_id,
                    coverage_status="PARTIALLY_COVERED"
                ).count()
                job.not_covered_stops = TimetableStop.query.filter_by(
                    timetable_id=job.timetable_id,
                    coverage_status="NOT_COVERED"
                ).count()
                job.remaining_stops = max(0, job.total_stops - (job.processed_stops + job.failed_stops))
                job.progress_percent = (job.processed_stops + job.failed_stops) / job.total_stops * 100.0 if job.total_stops > 0 else 100.0

                # Calculate media file progress
                processed_mf_count = StopMediaMatch.query.join(TimetableStop).filter(
                    TimetableStop.timetable_id == job.timetable_id,
                    StopMediaMatch.status == "processed",
                    StopMediaMatch.source_media_file_id.isnot(None)
                ).distinct(StopMediaMatch.source_media_file_id).count()
                job.processed_media_files = min(processed_mf_count, job.total_media_files)
                job.remaining_media_files = max(0, job.total_media_files - job.processed_media_files)

                # Individual angle progress indicators
                job.angle_a_progress = ((idx + 1) / job.total_stops) * 100.0 if job.total_stops > 0 else 100.0
                job.angle_b_progress = ((idx + 1) / job.total_stops) * 100.0 if job.total_stops > 0 else 100.0
                job.angle_c_progress = ((idx + 1) / job.total_stops) * 100.0 if job.total_stops > 0 else 100.0

                # Sum unique passenger estimates across stops
                all_stops = TimetableStop.query.filter_by(timetable_id=job.timetable_id).all()
                job.total_unique_passengers_estimate = sum(s.estimated_unique_passengers for s in all_stops)
                job.total_confirmed_identities = sum(s.confirmed_identities for s in all_stops)

                db.session.commit()

            # Step 9: Continue processing all remaining eligible media according to the timetable and configured time window
            if not self._stop_event.is_set():
                processed_mf_ids = set(
                    m.source_media_file_id for m in StopMediaMatch.query.join(TimetableStop).filter(
                        TimetableStop.timetable_id == job.timetable_id,
                        StopMediaMatch.status == "processed",
                        StopMediaMatch.source_media_file_id.isnot(None)
                    ).all()
                )
                rem_query = SourceMediaFile.query.filter(SourceMediaFile.status != "error")
                if job.journey_id:
                    rem_query = rem_query.filter(
                        (SourceMediaFile.journey_id == job.journey_id) |
                        (SourceMediaFile.journey_id.is_(None))
                    )
                remaining_mfs = [
                    f for f in rem_query.order_by(SourceMediaFile.start_time).all()
                    if f.id not in processed_mf_ids and f.file_path and os.path.exists(f.file_path) and f.is_color is not False
                ]

                if remaining_mfs:
                    job.ai_detection_status = f"Processing {len(remaining_mfs)} remaining eligible media files..."
                    db.session.commit()

                    rem_by_angle = {"A": [], "B": [], "C": []}
                    for mf in remaining_mfs:
                        rem_by_angle[mf.camera_angle].append(mf)

                    max_steps = max(len(rem_by_angle["A"]), len(rem_by_angle["B"]), len(rem_by_angle["C"]))
                    for step_i in range(max_steps):
                        if self._stop_event.is_set():
                            break
                        step_uploads = {}
                        batch_id = f"rem_{step_i}_{uuid.uuid4().hex[:6]}"
                        for angle in ["A", "B", "C"]:
                            if step_i < len(rem_by_angle[angle]):
                                mf = rem_by_angle[angle][step_i]
                                frame_path = mf.file_path
                                if mf.media_type == "video":
                                    extracted, err = extract_frame_at_timestamp(mf.file_path, mf.start_time, mf.start_time)
                                    if extracted:
                                        frame_path = extracted["frame_path"]
                                up = Upload(
                                    journey_id=job.journey_id,
                                    camera_angle=angle,
                                    batch_id=batch_id,
                                    file_path=str(frame_path),
                                    original_filename=mf.filename,
                                    capture_timestamp=mf.start_time or datetime.utcnow(),
                                    upload_timestamp=datetime.utcnow(),
                                    status="processing"
                                )
                                db.session.add(up)
                                db.session.flush()
                                step_uploads[angle] = up
                                mf.reuse_count = (mf.reuse_count or 0) + 1
                        if step_uploads:
                            try:
                                process_multi_angle_batch(
                                    journey_id=job.journey_id,
                                    batch_id=batch_id,
                                    angle_uploads=step_uploads,
                                    user_id=self.user_id
                                )
                            except Exception as pe:
                                print(f"[JobWorker] Remaining media processing notice: {pe}")
                        db.session.commit()

            # Step 10: Complete job and generate final stats
            if not self._stop_event.is_set():
                job.status = "completed"
                job.completed_at = datetime.utcnow()
                job.progress_percent = 100.0
                job.processed_media_files = job.total_media_files
                job.remaining_media_files = 0
                job.angle_a_progress = 100.0
                job.angle_b_progress = 100.0
                job.angle_c_progress = 100.0
                job.ai_detection_status = "Completed. All stops and media successfully processed."
                db.session.commit()


def start_automated_job(app, job_id, user_id=None):
    """Starts or resumes a background processing job."""
    if job_id in _active_jobs and _active_jobs[job_id].is_alive():
        # Resume if paused
        _active_jobs[job_id].resume()
        return True, "Job resumed"

    worker = JobWorker(app, job_id, user_id)
    _active_jobs[job_id] = worker
    worker.start()
    return True, "Job started"


def pause_automated_job(job_id):
    if job_id in _active_jobs:
        _active_jobs[job_id].pause()
        return True, "Job paused"
    return False, "Job not running"


def resume_automated_job(job_id):
    if job_id in _active_jobs:
        _active_jobs[job_id].resume()
        return True, "Job resumed"
    return False, "Job not found"


def cancel_automated_job(job_id):
    if job_id in _active_jobs:
        _active_jobs[job_id].cancel()
        return True, "Job cancelled"
    return False, "Job not found"
