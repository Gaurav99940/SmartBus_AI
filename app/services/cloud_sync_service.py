import os
import io
import re
import json
import shutil
import base64
import hashlib
import requests
import subprocess
from datetime import datetime
from pathlib import Path

from app.config import Config
from app.database import db
from app.models import (
    Journey, Bus, PassengerIdentity, PassengerObservation,
    RouteStop, TimetableImport, TimetableStop, CloudSyncLog, Upload
)
from app.services.audit_service import log_audit
from app.services.export_service import (
    generate_passenger_attendance_csv,
    generate_passenger_attendance_pdf,
    generate_journey_summary_pdf,
    generate_stop_processing_excel
)


# ============================================================================
# 1. REMOTE IMAGE SERVER CLIENT & CHECKSUM CACHE (http://143.244.140.108:8060)
# ============================================================================

def get_remote_server_base_url():
    """Returns the sanitized remote image server base URL."""
    url = getattr(Config, "REMOTE_IMAGE_SERVER_URL", "http://143.244.140.108:8060")
    return url.rstrip("/")


def get_file_sha256(file_path):
    """Calculates SHA-256 hash of a file for duplicate upload prevention."""
    if not file_path or not os.path.isfile(file_path):
        return None
    h = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def get_checksum_cache_path():
    """Returns the persistent image checksum cache path."""
    base_dir = getattr(Config, "SMARTBUS_DATA_DIR", Path(Config.BASE_DIR) / "smartbus-data")
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / "image_checksums.json"


def load_image_checksum_cache():
    """Loads previously uploaded image checksum mappings."""
    cache_path = get_checksum_cache_path()
    if cache_path.is_file():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_image_checksum_cache(cache):
    """Saves image checksum mappings to disk."""
    cache_path = get_checksum_cache_path()
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"[Checksum Cache Warning] Error saving cache: {e}")


def resolve_passenger_image_url(image_ref, person_id=None):
    """
    Returns a clean, universally valid absolute or relative URL for a passenger image.
    Ensures no double '/data/http' prefixes, provides automatic fallbacks across
    local disk, smartbus-data thumbnails, and remote image server mappings.
    """
    if not image_ref and not person_id:
        return None

    clean_ref = (str(image_ref).strip() if image_ref else "")
    # Strip accidental double prefix e.g. /data/http://143...
    if clean_ref.startswith("/data/http://") or clean_ref.startswith("/data/https://"):
        return clean_ref[6:]
    if clean_ref.startswith("/data/data/"):
        clean_ref = clean_ref[5:]

    # 1. Direct absolute URL
    if clean_ref.startswith("http://") or clean_ref.startswith("https://"):
        return clean_ref

    # 2. Check local file on disk
    if clean_ref:
        clean_p = clean_ref.lstrip("/").replace("data/", "", 1) if (clean_ref.startswith("/data/") or clean_ref.startswith("data/")) else clean_ref.lstrip("/")
        full_local = Path(Config.DATA_DIR) / clean_p
        if full_local.is_file():
            return f"/data/{clean_p.replace('\\', '/')}"

    # 3. Check smartbus-data/journeys/*/thumbnails/
    sb_journeys = Path(Config.BASE_DIR) / "smartbus-data" / "journeys"
    target_names = []
    if clean_ref:
        target_names.append(os.path.basename(clean_ref))
    if person_id:
        target_names.append(f"{person_id}_face.jpg")
        target_names.append(f"{person_id}.jpg")

    if sb_journeys.is_dir():
        for j_folder in sb_journeys.iterdir():
            thumbs_folder = j_folder / "thumbnails"
            if thumbs_folder.is_dir():
                for tname in target_names:
                    tfile = thumbs_folder / tname
                    if tfile.is_file():
                        return f"/data/thumbnails/{tname}"

    # 4. Check image checksum / remote URL cache
    cache = load_image_checksum_cache()
    for entry in cache.values():
        if entry.get("orig_basename") in target_names or (entry.get("filename") and entry.get("filename") in target_names):
            return entry.get("static_url") or entry.get("view_url")

    # 5. Fallback URL
    if person_id:
        return f"/data/thumbnails/{person_id}_face.jpg"
    if clean_ref:
        return f"/data/{clean_ref.lstrip('/')}"

    return None


def test_remote_server_connection():
    """
    Checks whether the remote image server is reachable.
    Returns: {"connected": bool, "status_code": int, "total_images": int, "error": str}
    """
    base_url = get_remote_server_base_url()
    try:
        resp = requests.get(f"{base_url}/images", params={"limit": 1}, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            return {
                "connected": True,
                "status_code": 200,
                "total_images": data.get("total", 0),
                "server_url": base_url,
                "error": None
            }
        return {
            "connected": False,
            "status_code": resp.status_code,
            "server_url": base_url,
            "error": f"Server returned HTTP {resp.status_code}"
        }
    except Exception as e:
        return {
            "connected": False,
            "status_code": 0,
            "server_url": base_url,
            "error": str(e)
        }


def upload_images_to_remote(file_paths, batch_size=15):
    """
    Bulk uploads multiple local image files to the remote server:
    POST /upload/bulk with multipart/form-data key 'files'.
    Uses SHA-256 checksums to avoid uploading duplicate images.
    Returns:
      {
        "success": bool,
        "total_requested": int,
        "total_uploaded": int,
        "total_failed": int,
        "already_cached": int,
        "uploaded_files": [...],
        "failed_files": [...],
        "url_mapping": { "orig_basename": "remote_view_url" }
      }
    """
    base_url = get_remote_server_base_url()
    valid_paths = [p for p in file_paths if p and os.path.isfile(p)]
    if not valid_paths:
        return {
            "success": True,
            "total_requested": 0,
            "total_uploaded": 0,
            "total_failed": 0,
            "already_cached": 0,
            "uploaded_files": [],
            "failed_files": [],
            "url_mapping": {}
        }

    cache = load_image_checksum_cache()
    url_mapping = {}
    paths_to_upload = []
    already_cached_count = 0

    for p in valid_paths:
        orig_base = os.path.basename(p)
        sha = get_file_sha256(p)
        if sha and sha in cache:
            entry = cache[sha]
            cached_url = entry.get("static_url") or entry.get("view_url")
            if cached_url:
                url_mapping[orig_base] = cached_url
                already_cached_count += 1
                continue
        paths_to_upload.append(p)

    total_uploaded = 0
    total_failed = 0
    all_uploaded = []
    all_failed = []

    for i in range(0, len(paths_to_upload), batch_size):
        chunk = paths_to_upload[i:i + batch_size]
        files_payload = []
        open_handles = []
        try:
            for p in chunk:
                fn = os.path.basename(p)
                ext = Path(p).suffix.lower()
                mime = "image/jpeg"
                if ext == ".png":
                    mime = "image/png"
                elif ext == ".webp":
                    mime = "image/webp"

                h = open(p, "rb")
                open_handles.append(h)
                files_payload.append(("files", (fn, h, mime)))

            resp = requests.post(
                f"{base_url}/upload/bulk",
                files=files_payload,
                timeout=35
            )

            if resp.status_code == 200:
                data = resp.json()
                uploaded = data.get("uploaded_files", [])
                failed = data.get("failed_files", [])

                total_uploaded += len(uploaded)
                total_failed += len(failed)
                all_uploaded.extend(uploaded)
                all_failed.extend(failed)

                # Map original local filename to returned remote view_url / static_url
                for orig_p, up_info in zip(chunk, uploaded):
                    orig_base = os.path.basename(orig_p)
                    remote_url = up_info.get("static_url") or up_info.get("view_url")
                    if remote_url:
                        url_mapping[orig_base] = remote_url
                        sha = get_file_sha256(orig_p)
                        if sha:
                            cache[sha] = {
                                "filename": up_info.get("filename"),
                                "orig_basename": orig_base,
                                "static_url": up_info.get("static_url"),
                                "view_url": up_info.get("view_url"),
                                "size_bytes": up_info.get("size_bytes"),
                                "uploaded_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                            }
            else:
                total_failed += len(chunk)
                all_failed.extend([{"filename": os.path.basename(p), "error": f"HTTP {resp.status_code}"} for p in chunk])
        except Exception as e:
            total_failed += len(chunk)
            all_failed.extend([{"filename": os.path.basename(p), "error": str(e)} for p in chunk])
        finally:
            for h in open_handles:
                try:
                    h.close()
                except Exception:
                    pass

    if all_uploaded:
        save_image_checksum_cache(cache)

    return {
        "success": total_failed == 0,
        "total_requested": len(valid_paths),
        "total_uploaded": total_uploaded,
        "total_failed": total_failed,
        "already_cached": already_cached_count,
        "uploaded_files": all_uploaded,
        "failed_files": all_failed,
        "url_mapping": url_mapping
    }


def fetch_remote_images(limit=100, offset=0):
    """
    Retrieves images list from GET /images and enriches with local report reference checks.
    """
    base_url = get_remote_server_base_url()
    try:
        resp = requests.get(
            f"{base_url}/images",
            params={"limit": limit, "offset": offset},
            timeout=10
        )
        if resp.status_code != 200:
            return {"success": False, "error": f"Remote server returned HTTP {resp.status_code}", "images": [], "total": 0}

        data = resp.json()
        raw_images = data.get("images", [])

        # Build in-memory set of image filenames referenced by active local DB passengers / crops
        referenced_filenames = {}
        try:
            passengers = PassengerIdentity.query.filter(PassengerIdentity.thumbnail_path.isnot(None)).all()
            for p in passengers:
                t = p.thumbnail_path or ""
                base = os.path.basename(t)
                referenced_filenames[base] = f"Passenger {p.person_id}"
                # Also index stem in case remote appended a random suffix
                stem = Path(base).stem.split("_")[0]
                if len(stem) >= 3:
                    referenced_filenames[stem] = f"Passenger {p.person_id}"

            obs_crops = PassengerObservation.query.filter(PassengerObservation.crop_path.isnot(None)).all()
            for o in obs_crops:
                c = o.crop_path or ""
                base = os.path.basename(c)
                referenced_filenames[base] = f"Observation #{o.id}"
        except Exception:
            pass

        enriched_images = []
        for img in raw_images:
            fn = img.get("filename", "")
            base = os.path.basename(fn)
            stem = Path(base).stem.split("_")[0]
            ref_label = referenced_filenames.get(base) or referenced_filenames.get(stem)

            enriched_images.append({
                "filename": fn,
                "size_bytes": img.get("size_bytes", 0),
                "size_kb": img.get("size_kb", 0.0),
                "size_mb": img.get("size_mb", 0.0),
                "created_at": img.get("created_at"),
                "modified_at": img.get("modified_at"),
                "view_url": img.get("view_url"),
                "static_url": img.get("static_url"),
                "is_referenced": bool(ref_label),
                "reference_label": ref_label
            })

        return {
            "success": True,
            "total": data.get("total", len(raw_images)),
            "limit": data.get("limit", limit),
            "offset": data.get("offset", offset),
            "images": enriched_images
        }
    except Exception as e:
        return {"success": False, "error": str(e), "images": [], "total": 0}


def delete_remote_image(filename, force=False, user_id=None):
    """
    Deletes a single image on remote server via DELETE /images/{filename}.
    Validates against directory traversal and checks active report references.
    """
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return {"success": False, "error": "Invalid filename."}

    clean_fn = os.path.basename(filename)

    # Check reference protection
    if not force:
        stem = Path(clean_fn).stem.split("_")[0]
        in_use_p = PassengerIdentity.query.filter(
            (PassengerIdentity.thumbnail_path.ilike(f"%{clean_fn}%")) |
            (PassengerIdentity.thumbnail_path.ilike(f"%{stem}%"))
        ).first()
        if in_use_p:
            return {
                "success": False,
                "requires_confirmation": True,
                "warning": f"Image '{clean_fn}' is referenced by active passenger {in_use_p.person_id}."
            }

    base_url = get_remote_server_base_url()
    try:
        resp = requests.delete(f"{base_url}/images/{clean_fn}", timeout=10)
        data = resp.json() if resp.status_code in [200, 404] else {}
        if resp.status_code == 200:
            log_audit("REMOTE_IMAGE_DELETE", f"Deleted remote image: {clean_fn}", user_id=user_id)
            return {"success": True, "message": data.get("message", f"Image {clean_fn} deleted.")}
        return {"success": False, "error": data.get("error", f"Remote error HTTP {resp.status_code}")}
    except Exception as e:
        return {"success": False, "error": str(e)}


def delete_remote_images_bulk(filenames, force=False, user_id=None):
    """
    Bulk deletes multiple images on remote server via POST /images/delete-bulk.
    """
    if not filenames or not isinstance(filenames, list):
        return {"success": False, "error": "No filenames provided."}

    cleaned = []
    for fn in filenames:
        if fn and "/" not in fn and "\\" not in fn and ".." not in fn:
            cleaned.append(os.path.basename(fn))

    if not cleaned:
        return {"success": False, "error": "No valid filenames to delete."}

    # Reference check if force is False
    if not force:
        in_use_list = []
        for cfn in cleaned:
            stem = Path(cfn).stem.split("_")[0]
            p = PassengerIdentity.query.filter(
                (PassengerIdentity.thumbnail_path.ilike(f"%{cfn}%")) |
                (PassengerIdentity.thumbnail_path.ilike(f"%{stem}%"))
            ).first()
            if p:
                in_use_list.append(f"{cfn} ({p.person_id})")

        if in_use_list:
            return {
                "success": False,
                "requires_confirmation": True,
                "warning": f"{len(in_use_list)} image(s) are referenced by active passenger records: {', '.join(in_use_list[:3])}"
            }

    base_url = get_remote_server_base_url()
    try:
        resp = requests.post(
            f"{base_url}/images/delete-bulk",
            json={"filenames": cleaned},
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            log_audit("REMOTE_IMAGE_BULK_DELETE", f"Deleted {data.get('deleted_count', len(cleaned))} remote images.", user_id=user_id)
            return {
                "success": True,
                "deleted_count": data.get("deleted_count", 0),
                "deleted": data.get("deleted", []),
                "not_found": data.get("not_found", [])
            }
        return {"success": False, "error": f"Remote error HTTP {resp.status_code}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ============================================================================
# 2. LOCAL DATA EXPORT & BUNDLE PREPARATION (smartbus-data/ Directory)
# ============================================================================

def export_journey_sync_bundle(journey_id, upload_remote_crops=True, user_id=None):
    """
    Exports complete journey data and official reports into a standardized smartbus-data/ structure:
      smartbus-data/
        manifest.json
        journeys/
          journey_{id:03d}/
            passengers.json
            attendance.json
            timeline.json
            stops.json
            reports/
              passenger_report.pdf
              summary_report.pdf
              attendance.csv
              attendance.xlsx
            thumbnails/
    Optionally uploads face crop thumbnails to the remote image server.
    """
    journey = Journey.query.get(journey_id)
    if not journey:
        return {"success": False, "error": f"Journey #{journey_id} not found."}

    base_export_dir = getattr(Config, "SMARTBUS_DATA_DIR", Path(Config.BASE_DIR) / "smartbus-data")
    jny_folder_name = f"journey_{journey.id:03d}"
    jny_dir = base_export_dir / "journeys" / jny_folder_name
    reports_dir = jny_dir / "reports"
    thumbs_dir = jny_dir / "thumbnails"

    reports_dir.mkdir(parents=True, exist_ok=True)
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    passengers = PassengerIdentity.query.filter_by(journey_id=journey.id).order_by(PassengerIdentity.id.asc()).all()
    bus = journey.bus

    # 1. Gather & Copy Local Thumbnails
    thumb_paths_to_upload = []
    p_thumb_mapping = {}

    for p in passengers:
        source_crop_path = None
        if p.thumbnail_path and os.path.isfile(p.thumbnail_path):
            source_crop_path = p.thumbnail_path
        elif p.thumbnail_path:
            full_p = Path(Config.DATA_DIR) / p.thumbnail_path
            if full_p.is_file():
                source_crop_path = str(full_p)

        if not source_crop_path:
            # Check best observation crop
            best_obs = next((o for o in p.observations if o.crop_path), None)
            if best_obs:
                full_c = Path(Config.DATA_DIR) / best_obs.crop_path
                if full_c.is_file():
                    source_crop_path = str(full_c)

        if not source_crop_path:
            # Check smartbus-data thumbnails folder
            sb_thumb = thumbs_dir / f"{p.person_id}_face.jpg"
            if sb_thumb.is_file():
                source_crop_path = str(sb_thumb)

        if not source_crop_path:
            # Check data/crops folder
            dc_thumb = Path(Config.DATA_DIR) / "crops" / f"{p.person_id}_face.jpg"
            if dc_thumb.is_file():
                source_crop_path = str(dc_thumb)

        if source_crop_path and os.path.isfile(source_crop_path):
            target_thumb_name = f"{p.person_id}_face.jpg"
            target_path = thumbs_dir / target_thumb_name
            if str(source_crop_path) != str(target_path):
                shutil.copy2(source_crop_path, target_path)

            # Ensure local data/crops directory also has this thumbnail
            crops_dir = Path(Config.DATA_DIR) / "crops"
            crops_dir.mkdir(parents=True, exist_ok=True)
            local_crop_copy = crops_dir / target_thumb_name
            if not local_crop_copy.is_file():
                shutil.copy2(source_crop_path, local_crop_copy)

            thumb_paths_to_upload.append(str(target_path))
            p_thumb_mapping[p.person_id] = target_thumb_name

    # 2. Upload Thumbnails to Remote Server (if enabled)
    remote_url_map = {}
    if upload_remote_crops and thumb_paths_to_upload:
        try:
            up_res = upload_images_to_remote(thumb_paths_to_upload)
            remote_url_map = up_res.get("url_mapping", {})
        except Exception as e:
            print(f"[Sync Warning] Remote image upload error: {e}")

    # 3. Build passengers.json (strictly without raw 512-d biometric vectors)
    passengers_data = []
    for p in passengers:
        local_thumb = p_thumb_mapping.get(p.person_id)
        remote_thumb = remote_url_map.get(local_thumb) if local_thumb else None
        if remote_thumb:
            p.thumbnail_path = remote_thumb
            for o in p.observations:
                if not o.crop_path or "http" in o.crop_path:
                    o.crop_path = remote_thumb

        traveled_stops = []
        if p.traveled_stops_json:
            try:
                traveled_stops = json.loads(p.traveled_stops_json)
            except Exception:
                pass

        obs_summary = []
        for o in p.observations:
            obs_summary.append({
                "observation_id": o.id,
                "time": o.observation_time.strftime("%Y-%m-%d %H:%M:%S") if o.observation_time else None,
                "camera_angle": o.camera_angle or "A",
                "detection_confidence": round(float(o.detection_confidence or 0.0), 3),
                "review_status": o.review_status or "CONFIRMED"
            })

        passengers_data.append({
            "id": p.id,
            "person_id": p.person_id,
            "canonical_person_id": p.get_canonical_person_id(),
            "status": p.status,
            "current_seat": p.get_seat_number(),
            "first_seen_time": p.first_seen_time.strftime("%Y-%m-%d %H:%M:%S") if p.first_seen_time else None,
            "last_seen_time": p.last_seen_time.strftime("%Y-%m-%d %H:%M:%S") if p.last_seen_time else None,
            "first_observed_angle": p.get_first_observed_angle(),
            "detected_angles": p.get_detected_angles(),
            "total_observations": p.total_observations or len(p.observations),
            "boarding_stop_number": p.boarding_stop_number or 1,
            "boarding_stop_name": p.boarding_stop_name or "Origin Stop",
            "exit_stop_number": p.exit_stop_number or 1,
            "exit_stop_name": p.exit_stop_name or "Destination Stop",
            "stops_traveled": p.get_distinct_stops_count(),
            "traveled_stops_detail": traveled_stops,
            "local_thumbnail_filename": local_thumb,
            "remote_thumbnail_url": remote_thumb,
            "is_merged": bool(p.is_merged or p.canonical_identity_id),
            "observations_summary": obs_summary
        })

    with open(jny_dir / "passengers.json", "w", encoding="utf-8") as f:
        json.dump(passengers_data, f, indent=2)

    db.session.commit()

    # 4. Build attendance.json
    total_pax = len(passengers)
    present_pax = sum(1 for p in passengers if p.status == "PRESENT")
    exited_pax = sum(1 for p in passengers if "EXIT" in (p.status or ""))
    cap = (bus.capacity if bus and bus.capacity else 40)
    occ_pct = round((total_pax / cap) * 100, 1) if cap > 0 else 0.0

    attendance_data = {
        "journey_id": journey.id,
        "journey_code": journey.journey_code,
        "bus_number": bus.bus_number if bus else "N/A",
        "route_name": (getattr(bus, "route_name", None) or f"{journey.origin} -> {journey.destination}"),
        "origin": journey.origin,
        "destination": journey.destination,
        "start_time": journey.start_time.strftime("%Y-%m-%d %H:%M:%S") if journey.start_time else None,
        "end_time": journey.end_time.strftime("%Y-%m-%d %H:%M:%S") if journey.end_time else None,
        "total_passengers_observed": total_pax,
        "present_passengers": present_pax,
        "exited_passengers": exited_pax,
        "bus_capacity": cap,
        "occupancy_rate_percentage": occ_pct,
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    }

    with open(jny_dir / "attendance.json", "w", encoding="utf-8") as f:
        json.dump(attendance_data, f, indent=2)

    # 5. Build timeline.json & stops.json
    # Fetch timetable stops if available
    tt = TimetableImport.query.filter_by(journey_id=journey.id).order_by(TimetableImport.id.desc()).first()
    stops_data = []
    if tt and tt.stops:
        for s in sorted(tt.stops, key=lambda x: x.stop_number):
            matches_summary = []
            for m in s.matches:
                matches_summary.append({
                    "camera_angle": m.camera_angle,
                    "status": m.status,
                    "media_filename": m.source_filename,
                    "match_reason": m.match_reason,
                    "is_color": m.is_color
                })
            stops_data.append({
                "stop_id": s.id,
                "stop_number": s.stop_number,
                "stop_name": s.stop_name,
                "scheduled_arrival": s.scheduled_arrival.strftime("%H:%M:%S") if s.scheduled_arrival else None,
                "scheduled_departure": s.scheduled_departure.strftime("%H:%M:%S") if s.scheduled_departure else None,
                "coverage_status": s.coverage_status,
                "matches": matches_summary
            })
    else:
        # Fallback to route stops
        rstops = RouteStop.query.filter_by(bus_id=bus.id if bus else None).order_by(RouteStop.stop_order).all()
        for rs in rstops:
            stops_data.append({
                "stop_id": rs.id,
                "stop_number": rs.stop_order,
                "stop_name": rs.stop_name,
                "scheduled_arrival": rs.time_window,
                "location_address": rs.location_address,
                "speed_kmh": rs.speed_kmh
            })

    with open(jny_dir / "stops.json", "w", encoding="utf-8") as f:
        json.dump(stops_data, f, indent=2)

    # Build timeline trajectories
    timeline_trajectories = []
    for p in passengers:
        b_num = p.boarding_stop_number or 1
        e_num = p.exit_stop_number or b_num
        st_count = p.get_distinct_stops_count()
        tot_stops = len(stops_data) or 1
        pct = round((st_count / tot_stops) * 100, 1)

        timeline_trajectories.append({
            "person_id": p.person_id,
            "status": p.status,
            "boarding_stop_number": b_num,
            "boarding_stop_name": p.boarding_stop_name or f"Stop #{b_num}",
            "exit_stop_number": e_num,
            "exit_stop_name": p.exit_stop_name or f"Stop #{e_num}",
            "traveled_stops_count": st_count,
            "travel_percentage": pct,
            "remote_thumbnail_url": remote_url_map.get(p_thumb_mapping.get(p.person_id))
        })

    with open(jny_dir / "timeline.json", "w", encoding="utf-8") as f:
        json.dump({
            "journey_id": journey.id,
            "journey_code": journey.journey_code,
            "total_route_stops": len(stops_data),
            "trajectories": timeline_trajectories
        }, f, indent=2)

    # 6. Generate Official PDF, CSV, Excel Reports
    try:
        csv_str = generate_passenger_attendance_csv(journey_id=journey.id)
        with open(reports_dir / "attendance.csv", "w", encoding="utf-8") as f:
            f.write(csv_str)
    except Exception as e:
        print(f"[Report Gen Warning] Attendance CSV error: {e}")

    try:
        pdf_bytes = generate_passenger_attendance_pdf(journey_id=journey.id)
        with open(reports_dir / "passenger_report.pdf", "wb") as f:
            f.write(pdf_bytes.getvalue() if hasattr(pdf_bytes, "getvalue") else pdf_bytes)
    except Exception as e:
        print(f"[Report Gen Warning] Attendance PDF error: {e}")

    try:
        sum_pdf_bytes = generate_journey_summary_pdf(journey_id=journey.id)
        with open(reports_dir / "summary_report.pdf", "wb") as f:
            f.write(sum_pdf_bytes.getvalue() if hasattr(sum_pdf_bytes, "getvalue") else sum_pdf_bytes)
    except Exception as e:
        print(f"[Report Gen Warning] Summary PDF error: {e}")

    if tt:
        try:
            excel_bytes = generate_stop_processing_excel(timetable_id=tt.id)
            with open(reports_dir / "attendance.xlsx", "wb") as f:
                f.write(excel_bytes.getvalue() if hasattr(excel_bytes, "getvalue") else excel_bytes)
        except Exception as e:
            print(f"[Report Gen Warning] Stop Excel error: {e}")

    # 7. Update root manifest.json
    manifest_path = base_export_dir / "manifest.json"
    manifest = {
        "version": "1.0",
        "system_name": "SmartBus AI Cloud Report Sync",
        "last_synced_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "published_journeys": []
    }
    if manifest_path.is_file():
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            pass

    # Update or append current journey entry
    existing_entry = next((e for e in manifest.get("published_journeys", []) if e.get("journey_id") == journey.id), None)
    entry_payload = {
        "journey_id": journey.id,
        "journey_code": journey.journey_code,
        "folder": jny_folder_name,
        "bus_number": bus.bus_number if bus else "N/A",
        "route": f"{journey.origin} -> {journey.destination}",
        "total_passengers": total_pax,
        "total_stops": len(stops_data),
        "exported_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "remote_images_uploaded": len(remote_url_map),
        "reports": {
            "passenger_report_pdf": f"journeys/{jny_folder_name}/reports/passenger_report.pdf",
            "summary_report_pdf": f"journeys/{jny_folder_name}/reports/summary_report.pdf",
            "attendance_csv": f"journeys/{jny_folder_name}/reports/attendance.csv",
            "attendance_xlsx": f"journeys/{jny_folder_name}/reports/attendance.xlsx" if tt else None
        }
    }

    if existing_entry:
        manifest["published_journeys"].remove(existing_entry)
    manifest.setdefault("published_journeys", []).append(entry_payload)
    manifest["last_synced_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    return {
        "success": True,
        "journey_id": journey.id,
        "journey_code": journey.journey_code,
        "export_directory": str(jny_dir),
        "total_passengers": total_pax,
        "total_stops": len(stops_data),
        "remote_images_uploaded": len(remote_url_map),
        "reports_generated": ["attendance.csv", "passenger_report.pdf", "summary_report.pdf"]
    }


# ============================================================================
# 3. GITHUB REPOSITORY SYNCHRONIZATION
# ============================================================================

def sync_data_to_github(journey_id=None, commit_message=None, user_id=None):
    """
    Synchronizes local smartbus-data/ records to the GitHub repository:
    1. If GITHUB_TOKEN is set, pushes files directly using GitHub REST API.
    2. Fallback to local git CLI if git is installed and configured.
    3. Saves CloudSyncLog record.
    """
    base_export_dir = getattr(Config, "SMARTBUS_DATA_DIR", Path(Config.BASE_DIR) / "smartbus-data")
    if not base_export_dir.exists():
        return {"success": False, "error": "No smartbus-data directory exists. Export journey first."}

    token = getattr(Config, "GITHUB_TOKEN", "") or os.getenv("GITHUB_TOKEN", "")
    repo = getattr(Config, "GITHUB_REPO", "Gaurav99940/SmartBus_AI") or os.getenv("GITHUB_REPO", "Gaurav99940/SmartBus_AI")
    branch = getattr(Config, "GITHUB_BRANCH", "main") or os.getenv("GITHUB_BRANCH", "main")

    default_msg = f"Publish SmartBus AI Reports - Journey #{journey_id}" if journey_id else "Publish SmartBus AI Reports & Attendance"
    msg = commit_message or default_msg

    sync_log = CloudSyncLog(
        journey_id=journey_id,
        sync_type="full",
        sync_status="in_progress",
        github_repo=repo,
        github_branch=branch,
        published_path=str(base_export_dir),
        created_at=datetime.utcnow()
    )
    db.session.add(sync_log)
    db.session.commit()

    # Collect all files inside smartbus-data/
    files_to_sync = []
    for root, _, filenames in os.walk(str(base_export_dir)):
        for fn in filenames:
            full_p = Path(root) / fn
            rel_p = full_p.relative_to(Config.BASE_DIR).as_posix()
            files_to_sync.append((full_p, rel_p))

    sync_log.total_records = len(files_to_sync)
    db.session.commit()

    # Strategy A: GitHub REST API (if token provided)
    if token:
        try:
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "SmartBus-AI-CloudSync/1.0"
            }
            uploaded_count = 0
            failed_count = 0

            for full_p, rel_p in files_to_sync:
                with open(full_p, "rb") as f:
                    content_b64 = base64.b64encode(f.read()).decode("utf-8")

                # Get existing file SHA if present
                get_url = f"https://api.github.com/repos/{repo}/contents/{rel_p}?ref={branch}"
                sha = None
                try:
                    r_get = requests.get(get_url, headers=headers, timeout=10)
                    if r_get.status_code == 200:
                        sha = r_get.json().get("sha")
                except Exception:
                    pass

                put_url = f"https://api.github.com/repos/{repo}/contents/{rel_p}"
                payload = {
                    "message": f"{msg} - {rel_p}",
                    "content": content_b64,
                    "branch": branch
                }
                if sha:
                    payload["sha"] = sha

                r_put = requests.put(put_url, headers=headers, json=payload, timeout=20)
                if r_put.status_code in [200, 201]:
                    uploaded_count += 1
                else:
                    failed_count += 1

            sync_log.sync_status = "success" if failed_count == 0 else "partial_success"
            sync_log.completed_at = datetime.utcnow()
            sync_log.details_json = json.dumps({"uploaded": uploaded_count, "failed": failed_count, "method": "github_api"})
            db.session.commit()

            log_audit("GITHUB_SYNC_SUCCESS", f"Published {uploaded_count} files to GitHub repo {repo}", user_id=user_id)
            return {
                "success": True,
                "method": "github_api",
                "repo": repo,
                "branch": branch,
                "files_synced": uploaded_count,
                "files_failed": failed_count,
                "sync_log_id": sync_log.id
            }
        except Exception as e:
            sync_log.sync_status = "failed"
            sync_log.error_message = str(e)
            sync_log.completed_at = datetime.utcnow()
            db.session.commit()
            return {"success": False, "error": f"GitHub API sync error: {str(e)}"}

    # Strategy B: Local Git CLI fallback
    try:
        subprocess.run(["git", "add", "smartbus-data/"], cwd=str(Config.BASE_DIR), check=True, capture_output=True)
        # Check if there are changes to commit
        diff_res = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(Config.BASE_DIR))
        if diff_res.returncode != 0:
            subprocess.run(["git", "commit", "-m", msg], cwd=str(Config.BASE_DIR), check=True, capture_output=True)

        push_res = subprocess.run(["git", "push", "origin", branch], cwd=str(Config.BASE_DIR), capture_output=True, text=True)
        if push_res.returncode == 0:
            sync_log.sync_status = "success"
            sync_log.completed_at = datetime.utcnow()
            sync_log.details_json = json.dumps({"files_count": len(files_to_sync), "method": "git_cli"})
            db.session.commit()
            log_audit("GITHUB_SYNC_SUCCESS", f"Pushed smartbus-data to GitHub repo via git CLI", user_id=user_id)
            return {
                "success": True,
                "method": "git_cli",
                "repo": repo,
                "branch": branch,
                "files_synced": len(files_to_sync),
                "sync_log_id": sync_log.id
            }
        else:
            # If git push failed (e.g. no credentials in non-interactive shell), still keep local export valid
            sync_log.sync_status = "local_exported"
            sync_log.error_message = f"Local export created in smartbus-data/. Git push note: {push_res.stderr.strip()[:200]}"
            sync_log.completed_at = datetime.utcnow()
            db.session.commit()
            return {
                "success": True,
                "method": "local_export_ready",
                "message": "Reports exported cleanly to smartbus-data/. Set GITHUB_TOKEN in .env for direct 1-click cloud sync.",
                "files_synced": len(files_to_sync),
                "sync_log_id": sync_log.id
            }
    except Exception as e:
        sync_log.sync_status = "local_exported"
        sync_log.error_message = f"Files exported locally. Git note: {str(e)}"
        sync_log.completed_at = datetime.utcnow()
        db.session.commit()
        return {
            "success": True,
            "method": "local_export_ready",
            "message": "Reports exported cleanly to smartbus-data/. Set GITHUB_TOKEN in .env for direct 1-click cloud sync.",
            "files_synced": len(files_to_sync),
            "sync_log_id": sync_log.id
        }


# ============================================================================
# 4. LIVE SERVER INGESTION (Lightweight Server Mode - No AI Inference)
# ============================================================================

def ingest_published_data(source_dir=None):
    """
    Called on the live server (or upon manual pull) to read smartbus-data/
    and ingest passengers, attendance, and journey timelines directly into the database.
    Does NOT invoke any AI models or OpenCV pipelines, preserving light server resources.
    """
    base_dir = Path(source_dir) if source_dir else getattr(Config, "SMARTBUS_DATA_DIR", Path(Config.BASE_DIR) / "smartbus-data")
    manifest_p = base_dir / "manifest.json"
    if not manifest_p.is_file():
        return {"success": False, "error": "No manifest.json found in smartbus-data/."}

    try:
        with open(manifest_p, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        return {"success": False, "error": f"Failed to parse manifest: {e}"}

    published = manifest.get("published_journeys", [])
    ingested_count = 0
    updated_passengers = 0

    for item in published:
        folder_name = item.get("folder")
        if not folder_name:
            continue
        jny_dir = base_dir / "journeys" / folder_name
        if not jny_dir.exists():
            continue

        j_id = item.get("journey_id")
        j_code = item.get("journey_code")

        # Find or create Journey
        journey = Journey.query.get(j_id) if j_id else None
        if not journey and j_code:
            journey = Journey.query.filter_by(journey_code=j_code).first()

        bus = None
        bus_num = item.get("bus_number")
        if bus_num and bus_num != "N/A":
            bus = Bus.query.filter_by(bus_number=bus_num).first()
            if not bus:
                bus = Bus(bus_number=bus_num, registration_number=bus_num, capacity=40)
                db.session.add(bus)
                db.session.flush()

        if not journey:
            journey = Journey(
                id=j_id if (j_id and not Journey.query.get(j_id)) else None,
                bus_id=bus.id if bus else 1,
                journey_code=j_code or f"JNY-SYNC-{int(datetime.utcnow().timestamp())}",
                origin=item.get("origin") or "Central Station",
                destination=item.get("destination") or "Terminal",
                status="completed",
                start_time=datetime.utcnow()
            )
            db.session.add(journey)
            db.session.flush()

        # Ingest passengers.json
        p_file = jny_dir / "passengers.json"
        crops_dir = Path(Config.DATA_DIR) / "crops"
        crops_dir.mkdir(parents=True, exist_ok=True)

        # Ensure an upload record exists to satisfy passenger_observations foreign key constraint
        sync_upload = Upload.query.filter_by(journey_id=journey.id).first()
        if not sync_upload:
            sync_upload = Upload(
                journey_id=journey.id,
                bus_id=bus.id if bus else 1,
                original_filename="sync_manifest.json",
                file_path=str(p_file),
                camera_angle="A",
                status="processed",
                capture_timestamp=datetime(2026, 10, 18, 18, 0, 0),
                upload_timestamp=datetime(2026, 10, 18, 18, 0, 0)
            )
            db.session.add(sync_upload)
            db.session.flush()

        if p_file.is_file():
            try:
                with open(p_file, "r", encoding="utf-8") as pf:
                    p_list = json.load(pf)

                for p_dict in p_list:
                    pid = p_dict.get("person_id")
                    if not pid:
                        continue
                    existing_p = PassengerIdentity.query.filter_by(journey_id=journey.id, person_id=pid).first()
                    local_fn = p_dict.get("local_thumbnail_filename") or f"{pid}_face.jpg"
                    remote_url = p_dict.get("remote_thumbnail_url")
                    thumb_url = remote_url or (f"crops/{local_fn}" if (crops_dir / local_fn).is_file() else local_fn)

                    # Copy thumbnail from smartbus-data to local data/crops
                    sb_thumb_src = jny_dir / "thumbnails" / local_fn
                    if sb_thumb_src.is_file():
                        local_crop_dest = crops_dir / local_fn
                        if not local_crop_dest.is_file():
                            shutil.copy2(sb_thumb_src, local_crop_dest)

                    target_p = existing_p
                    if not existing_p:
                        target_p = PassengerIdentity(
                            journey_id=journey.id,
                            person_id=pid,
                            canonical_person_id=p_dict.get("canonical_person_id") or pid,
                            status=p_dict.get("status") or "PRESENT",
                            current_seat=p_dict.get("current_seat"),
                            thumbnail_path=thumb_url,
                            boarding_stop_number=p_dict.get("boarding_stop_number"),
                            boarding_stop_name=p_dict.get("boarding_stop_name"),
                            exit_stop_number=p_dict.get("exit_stop_number"),
                            exit_stop_name=p_dict.get("exit_stop_name"),
                            stops_traveled=p_dict.get("stops_traveled") or 1,
                            traveled_stops_json=json.dumps(p_dict.get("traveled_stops_detail", [])),
                            total_observations=p_dict.get("total_observations", 1),
                            first_observed_angle=p_dict.get("first_observed_angle") or "A",
                            detected_angles_json=json.dumps(p_dict.get("detected_angles", ["A"])),
                            created_at=datetime.utcnow(),
                            updated_at=datetime.utcnow()
                        )
                        db.session.add(target_p)
                        db.session.flush()
                        updated_passengers += 1
                    else:
                        if remote_url and (not existing_p.thumbnail_path or "http" not in existing_p.thumbnail_path):
                            existing_p.thumbnail_path = remote_url
                        elif not existing_p.thumbnail_path:
                            existing_p.thumbnail_path = thumb_url
                        existing_p.stops_traveled = p_dict.get("stops_traveled") or existing_p.stops_traveled
                        if p_dict.get("traveled_stops_detail"):
                            existing_p.traveled_stops_json = json.dumps(p_dict.get("traveled_stops_detail"))
                        updated_passengers += 1

                    # Ingest passenger observations so Passenger Directory & reports display all crops and angles
                    obs_summary = p_dict.get("observations_summary") or []
                    effective_crop = target_p.thumbnail_path or f"crops/{local_fn}"

                    if obs_summary:
                        for obs_info in obs_summary:
                            obs_id = obs_info.get("observation_id")
                            obs_time_str = obs_info.get("time")
                            obs_dt = None
                            if obs_time_str:
                                try:
                                    obs_dt = datetime.strptime(obs_time_str, "%Y-%m-%d %H:%M:%S")
                                except Exception:
                                    pass
                            if not obs_dt:
                                obs_dt = target_p.first_seen_time or datetime.utcnow()

                            existing_obs = PassengerObservation.query.get(obs_id) if obs_id else None
                            if not existing_obs:
                                new_obs = PassengerObservation(
                                    id=obs_id if (obs_id and not PassengerObservation.query.get(obs_id)) else None,
                                    upload_id=sync_upload.id,
                                    journey_id=journey.id,
                                    passenger_identity_id=target_p.id,
                                    camera_angle=obs_info.get("camera_angle") or target_p.first_observed_angle or "A",
                                    observation_time=obs_dt,
                                    detection_confidence=float(obs_info.get("detection_confidence") or 0.85),
                                    bbox_x=0,
                                    bbox_y=0,
                                    bbox_w=100,
                                    bbox_h=100,
                                    crop_path=effective_crop,
                                    review_status=obs_info.get("review_status") or "CONFIRMED"
                                )
                                db.session.add(new_obs)
                            else:
                                if not existing_obs.crop_path or "crops/" not in existing_obs.crop_path:
                                    existing_obs.crop_path = effective_crop
                    else:
                        # Fallback: ensure at least one observation exists for this passenger
                        has_obs = PassengerObservation.query.filter_by(passenger_identity_id=target_p.id).first()
                        if not has_obs:
                            synth_obs = PassengerObservation(
                                upload_id=sync_upload.id,
                                journey_id=journey.id,
                                passenger_identity_id=target_p.id,
                                camera_angle=target_p.first_observed_angle or "A",
                                observation_time=target_p.first_seen_time or datetime.utcnow(),
                                detection_confidence=0.88,
                                bbox_x=0,
                                bbox_y=0,
                                bbox_w=100,
                                bbox_h=100,
                                crop_path=effective_crop,
                                review_status="CONFIRMED"
                            )
                            db.session.add(synth_obs)

            except Exception as e:
                print(f"[Ingest Warning] Error loading passengers.json: {e}")

        # Ingest stops.json
        s_file = jny_dir / "stops.json"
        if s_file.is_file():
            try:
                with open(s_file, "r", encoding="utf-8") as sf:
                    s_list = json.load(sf)
                for s_dict in s_list:
                    s_num = s_dict.get("stop_number") or s_dict.get("stop_id")
                    s_name = s_dict.get("stop_name") or f"Stop #{s_num}"
                    if s_num and bus:
                        existing_rs = RouteStop.query.filter_by(bus_id=bus.id, stop_order=s_num).first()
                        if not existing_rs:
                            rs = RouteStop(
                                bus_id=bus.id,
                                stop_order=s_num,
                                stop_name=s_name,
                                time_window=s_dict.get("scheduled_arrival") or "10:00 AM",
                                location_address=s_dict.get("location_address") or s_name,
                                latitude=s_dict.get("latitude") or 26.8467,
                                longitude=s_dict.get("longitude") or 80.9462
                            )
                            db.session.add(rs)
            except Exception as e:
                print(f"[Ingest Warning] Error loading stops.json: {e}")

        ingested_count += 1

    db.session.commit()
    return {
        "success": True,
        "published_journeys_found": len(published),
        "journeys_ingested": ingested_count,
        "passengers_synced": updated_passengers,
        "last_synced_at": manifest.get("last_synced_at")
    }


def get_sync_history(limit=25):
    """
    Returns history of cloud sync operations for the dashboard.
    """
    logs = CloudSyncLog.query.order_by(CloudSyncLog.id.desc()).limit(limit).all()
    return [l.to_dict() for l in logs]


def retry_failed_sync(journey_id=None):
    """
    Finds passengers whose thumbnails failed to upload or are missing remote URLs,
    uploads them to the remote image server, and updates all JSON manifests and DB records.
    """
    target_jny = Journey.query.get(journey_id) if journey_id else Journey.query.order_by(Journey.id.desc()).first()
    if not target_jny:
        return {"success": False, "error": "No journey found to retry sync."}

    passengers = PassengerIdentity.query.filter_by(journey_id=target_jny.id).all()
    base_dir = getattr(Config, "SMARTBUS_DATA_DIR", Path(Config.BASE_DIR) / "smartbus-data")
    thumbs_dir = base_dir / "journeys" / f"journey_{target_jny.id:03d}" / "thumbnails"

    paths_to_upload = []
    for p in passengers:
        # Check if missing remote URL
        if not p.thumbnail_path or not p.thumbnail_path.startswith("http"):
            t_file = thumbs_dir / f"{p.person_id}_face.jpg"
            if t_file.is_file():
                paths_to_upload.append(str(t_file))

    if not paths_to_upload:
        return {
            "success": True,
            "message": "All passenger thumbnails are already synchronized with valid remote URLs.",
            "total_retried": 0,
            "total_uploaded": 0
        }

    up_res = upload_images_to_remote(paths_to_upload)
    url_map = up_res.get("url_mapping", {})

    updated_count = 0
    for p in passengers:
        tname = f"{p.person_id}_face.jpg"
        if tname in url_map:
            p.thumbnail_path = url_map[tname]
            updated_count += 1
            for o in p.observations:
                o.crop_path = url_map[tname]

    db.session.commit()

    # Re-export journey bundle to refresh passengers.json and timeline.json
    export_journey_sync_bundle(target_jny.id, upload_remote_crops=False)

    return {
        "success": True,
        "message": f"Successfully retried and synchronized {updated_count} passenger thumbnails.",
        "total_retried": len(paths_to_upload),
        "total_uploaded": up_res.get("total_uploaded", 0),
        "already_cached": up_res.get("already_cached", 0),
        "updated_records": updated_count
    }


def get_latest_sync_status():
    """
    Returns latest synchronization metrics, connection status, and publishing history.
    """
    latest_log = CloudSyncLog.query.order_by(CloudSyncLog.id.desc()).first()
    conn_info = test_remote_server_connection()
    repo = getattr(Config, "GITHUB_REPO", "Gaurav99940/SmartBus_AI")
    branch = getattr(Config, "GITHUB_BRANCH", "main")
    token = getattr(Config, "GITHUB_TOKEN", "")
    role = getattr(Config, "SMARTBUS_SYSTEM_ROLE", "local")

    base_export_dir = getattr(Config, "SMARTBUS_DATA_DIR", Path(Config.BASE_DIR) / "smartbus-data")
    manifest_p = base_export_dir / "manifest.json"
    published_count = 0
    manifest_date = None
    if manifest_p.is_file():
        try:
            with open(manifest_p, "r", encoding="utf-8") as f:
                mf = json.load(f)
                published_count = len(mf.get("published_journeys", []))
                manifest_date = mf.get("last_synced_at")
        except Exception:
            pass

    return {
        "success": True,
        "system_role": role,
        "remote_image_server": conn_info,
        "github": {
            "repo": repo,
            "branch": branch,
            "has_token": bool(token)
        },
        "published_journeys_count": published_count,
        "last_published_manifest": manifest_date,
        "latest_sync_log": latest_log.to_dict() if latest_log else None
    }
