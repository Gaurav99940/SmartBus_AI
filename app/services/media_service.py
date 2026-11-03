import os
import re
import cv2
import hashlib
from datetime import datetime
from pathlib import Path
from PIL import Image, ExifTags
from app.config import Config

SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".wmv", ".webm", ".m4v"}
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALL_SUPPORTED_EXTENSIONS = SUPPORTED_VIDEO_EXTENSIONS | SUPPORTED_IMAGE_EXTENSIONS


def compute_file_hash(file_path):
    """Computes SHA-256 hash of a file for duplicate detection."""
    sha = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                sha.update(chunk)
        return sha.hexdigest()
    except Exception:
        return None


def parse_timestamp_from_filename(filename_or_path, base_date=None):
    """
    Attempts to extract timestamp from filename or path string:
    Supports patterns like:
      - 15-10-2026_06-01-20, 15-10-2026 06:01:20
      - 2026-10-15_18-01-20, 20261015_180120
      - 15-10-2026 06-01-20 PM / AM
      - clip_A_15-10-2026_18-01-20
      - 2026-10-23_10-15-00.mp4
      - 101500.mp4
      - Stop01_A_10-15-00.mp4
      - 1792136931479_0_5862D3WN1000127670_20261016062343_20261016062414.mp4
      - 5862D3WN1000127670_20261018_193301.jpg
    """
    s = Path(filename_or_path).stem

    # Check 12-hour AM/PM with date
    m_ampm = re.search(r'(?<!\d)(\d{2})[-_/](\d{2})[-_/](\d{4})[-_\s]+(\d{1,2})[-_:](\d{2})(?:[-_:](\d{2}))?[-_\s]*(AM|PM)', s, re.IGNORECASE)
    if m_ampm:
        try:
            sec = m_ampm.group(6) or "00"
            dt_str = f"{m_ampm.group(1)}-{m_ampm.group(2)}-{m_ampm.group(3)} {m_ampm.group(4)}:{m_ampm.group(5)}:{sec} {m_ampm.group(7).upper()}"
            return datetime.strptime(dt_str, "%d-%m-%Y %I:%M:%S %p")
        except Exception:
            pass

    # Day-Month-Year 24h: 15-10-2026_18-01-20 or 15-10-2026 18:01:20
    for m_dmy in re.finditer(r'(?<!\d)(\d{2})[-_/](\d{2})[-_/](\d{4})[-_\s]+(\d{2})[-_:](\d{2})(?:[-_:](\d{2}))?(?!\d)', s):
        try:
            sec = m_dmy.group(6) or "00"
            dt_str = f"{m_dmy.group(1)}-{m_dmy.group(2)}-{m_dmy.group(3)} {m_dmy.group(4)}:{m_dmy.group(5)}:{sec}"
            return datetime.strptime(dt_str, "%d-%m-%Y %H:%M:%S")
        except Exception:
            continue

    # Year-Month-Day 24h: 2026-10-23_10-15-00 or 2026-10-23 10:15:00
    for m_ymd in re.finditer(r'(?<!\d)(\d{4})[-_/](\d{2})[-_/](\d{2})[-_\s]+(\d{2})[-_:](\d{2})(?:[-_:](\d{2}))?(?!\d)', s):
        try:
            sec = m_ymd.group(6) or "00"
            dt_str = f"{m_ymd.group(1)}-{m_ymd.group(2)}-{m_ymd.group(3)} {m_ymd.group(4)}:{m_ymd.group(5)}:{sec}"
            return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue

    # Compact yyyymmdd_hhmmss or yyyymmddhhmmss with boundary protection
    patterns = [
        (r'(?<!\d)(\d{4})(\d{2})(\d{2})[-_](\d{2})(\d{2})(\d{2})(?!\d)', "%Y%m%d %H%M%S"),
        (r'(?<!\d)(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?!\d)', "%Y%m%d%H%M%S"),
    ]
    for pat, fmt in patterns:
        for m in re.finditer(pat, s):
            try:
                if fmt == "%Y%m%d %H%M%S":
                    dt_str = f"{m.group(1)}{m.group(2)}{m.group(3)} {m.group(4)}{m.group(5)}{m.group(6)}"
                    return datetime.strptime(dt_str, fmt)
                elif fmt == "%Y%m%d%H%M%S":
                    dt_str = f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}{m.group(5)}{m.group(6)}"
                    return datetime.strptime(dt_str, fmt)
            except Exception:
                continue

    # Time-only with delimiters: Stop01_A_10-15-00 or 10_15_00 or 10-15
    m_time_delim = re.search(r'(?:^|[_\-\s])(\d{2})[-_:](\d{2})(?:[-_:](\d{2}))?(?:[_\-\s]|$)', s)
    if m_time_delim:
        try:
            hr = int(m_time_delim.group(1))
            mn = int(m_time_delim.group(2))
            sc = int(m_time_delim.group(3) or "00")
            if 0 <= hr < 24 and 0 <= mn < 60 and 0 <= sc < 60:
                from datetime import date
                b_date = base_date or date.today()
                from datetime import time as dt_time
                return datetime.combine(b_date, dt_time(hr, mn, sc))
        except Exception:
            pass

    # Compact 6-digit HHMMSS (e.g. 101500.mp4)
    m_compact_time = re.search(r'(?:^|[_\-\s])(\d{2})(\d{2})(\d{2})(?:[_\-\s]|$)', s)
    if m_compact_time:
        try:
            hr = int(m_compact_time.group(1))
            mn = int(m_compact_time.group(2))
            sc = int(m_compact_time.group(3))
            if 0 <= hr < 24 and 0 <= mn < 60 and 0 <= sc < 60:
                from datetime import date, time as dt_time
                b_date = base_date or date.today()
                return datetime.combine(b_date, dt_time(hr, mn, sc))
        except Exception:
            pass

    return None


def parse_dual_timestamps_from_filename(filename_or_path, base_date=None):
    """
    Extracts both start_time and end_time if present in filename
    (e.g. _20261016062343_20261016062414.mp4 or _20261018_193301_20261018_193306.jpg).
    Returns (start_dt, end_dt).
    """
    s = Path(filename_or_path).stem

    # 1. Dual YYYYMMDD_HHMMSS sequences
    all_sep = re.findall(r'(?<!\d)(\d{4})(\d{2})(\d{2})[-_](\d{2})(\d{2})(\d{2})(?!\d)', s)
    if len(all_sep) >= 2:
        try:
            m1 = all_sep[0]
            m2 = all_sep[1]
            t1 = datetime.strptime(f"{m1[0]}{m1[1]}{m1[2]} {m1[3]}{m1[4]}{m1[5]}", "%Y%m%d %H%M%S")
            t2 = datetime.strptime(f"{m2[0]}{m2[1]}{m2[2]} {m2[3]}{m2[4]}{m2[5]}", "%Y%m%d %H%M%S")
            if t2 >= t1:
                return t1, t2
            return t1, None
        except Exception:
            pass
    elif len(all_sep) == 1:
        try:
            m1 = all_sep[0]
            t1 = datetime.strptime(f"{m1[0]}{m1[1]}{m1[2]} {m1[3]}{m1[4]}{m1[5]}", "%Y%m%d %H%M%S")
            return t1, None
        except Exception:
            pass

    # 2. Dual 14-digit sequences YYYYMMDDHHMMSS
    all_14 = re.findall(r'(?<!\d)(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?!\d)', s)
    if len(all_14) >= 2:
        try:
            m1 = all_14[0]
            m2 = all_14[1]
            t1 = datetime.strptime(f"{m1[0]}{m1[1]}{m1[2]} {m1[3]}{m1[4]}{m1[5]}", "%Y%m%d %H%M%S")
            t2 = datetime.strptime(f"{m2[0]}{m2[1]}{m2[2]} {m2[3]}{m2[4]}{m2[5]}", "%Y%m%d %H%M%S")
            if t2 >= t1:
                return t1, t2
            return t1, None
        except Exception:
            pass
    elif len(all_14) == 1:
        try:
            m1 = all_14[0]
            t1 = datetime.strptime(f"{m1[0]}{m1[1]}{m1[2]} {m1[3]}{m1[4]}{m1[5]}", "%Y%m%d %H%M%S")
            return t1, None
        except Exception:
            pass

    start_dt = parse_timestamp_from_filename(filename_or_path, base_date=base_date)
    return start_dt, None


def extract_media_metadata(file_path, base_date=None):
    """
    Extracts metadata, durations, start/end timestamps from video or image file.
    Does NOT modify the file. File is treated strictly read-only.
    """
    path = Path(file_path)
    if not path.exists():
        return {"error": f"File not found: {file_path}", "is_valid": False}

    ext = path.suffix.lower()
    stat = path.stat()
    file_size = stat.st_size
    mtime = datetime.fromtimestamp(stat.st_mtime)

    meta = {
        "file_path": str(path.resolve()),
        "filename": path.name,
        "file_ext": ext,
        "file_size": file_size,
        "media_type": "video" if ext in SUPPORTED_VIDEO_EXTENSIONS else ("image" if ext in SUPPORTED_IMAGE_EXTENSIONS else "unknown"),
        "is_valid": True,
        "duration_seconds": 0.0,
        "fps": 0.0,
        "total_frames": 0,
        "width": 0,
        "height": 0,
        "start_time": None,
        "end_time": None,
        "timestamp_source": "FILE_MTIME",
        "is_timestamp_reliable": False,
        "file_hash": None,
        "error": None
    }

    if meta["media_type"] not in ("video", "image"):
        meta["is_valid"] = False
        meta["error"] = f"Unsupported media format '{ext}'"
        return meta

    # Try filename timestamps (single or dual start/end)
    fn_start, fn_end = parse_dual_timestamps_from_filename(path.name, base_date=base_date)
    fn_time = fn_start

    if meta["media_type"] == "image":
        try:
            with Image.open(path) as img:
                meta["width"], meta["height"] = img.size
                exif = img.getexif()
                exif_time = None
                if exif:
                    for tag_id, val in exif.items():
                        tag_name = ExifTags.TAGS.get(tag_id, tag_id)
                        if tag_name in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
                            try:
                                exif_time = datetime.strptime(str(val).strip(), "%Y:%m:%d %H:%M:%S")
                                break
                            except Exception:
                                pass
                if exif_time:
                    meta["start_time"] = exif_time
                    meta["end_time"] = exif_time
                    meta["timestamp_source"] = "EXIF"
                    meta["is_timestamp_reliable"] = True
                elif fn_time:
                    meta["start_time"] = fn_time
                    meta["end_time"] = fn_end or fn_time
                    meta["timestamp_source"] = "FILENAME"
                    meta["is_timestamp_reliable"] = True
                else:
                    meta["start_time"] = mtime
                    meta["end_time"] = mtime
                    meta["timestamp_source"] = "FILE_MTIME"
                    meta["is_timestamp_reliable"] = False
        except Exception as e:
            meta["is_valid"] = False
            meta["error"] = f"Image read error: {str(e)}"
            return meta

    elif meta["media_type"] == "video":
        cap = None
        try:
            cap = cv2.VideoCapture(str(path))
            if not cap.isOpened():
                meta["is_valid"] = False
                meta["error"] = f"Unable to open video: {path.name}"
                return meta

            fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

            duration = (total_frames / fps) if (fps > 0 and total_frames > 0) else 0.0
            if duration <= 0.0 and fn_start and fn_end:
                duration = max(0.0, (fn_end - fn_start).total_seconds())

            meta["fps"] = fps
            meta["total_frames"] = total_frames
            meta["width"] = width
            meta["height"] = height
            meta["duration_seconds"] = duration

            # Determine video start and end timestamps
            if fn_start:
                meta["start_time"] = fn_start
                meta["end_time"] = fn_end if fn_end else (fn_start + timedelta(seconds=duration))
                meta["timestamp_source"] = "FILENAME"
                meta["is_timestamp_reliable"] = True
            else:
                from datetime import timedelta
                meta["start_time"] = mtime - timedelta(seconds=duration)
                meta["end_time"] = mtime
                meta["timestamp_source"] = "FILE_MTIME"
                meta["is_timestamp_reliable"] = False
        except Exception as e:
            meta["is_valid"] = False
            meta["error"] = f"Video inspection error: {str(e)}"
            return meta
        finally:
            if cap:
                cap.release()

    meta["file_hash"] = compute_file_hash(str(path))
    return meta


def scan_directory(dir_path, base_date=None):
    """
    Scans a local directory safely for supported clips and images.
    Returns: { "success": bool, "folder_path": str, "folder_name": str, "clips_count": int, "images_count": int, "files": list, "error": str }
    """
    folder = Path(dir_path)
    if not folder.exists() or not folder.is_dir():
        return {
            "success": False,
            "error": f"Directory does not exist or is not a folder: {dir_path}",
            "clips_count": 0,
            "images_count": 0,
            "files": []
        }

    try:
        discovered_files = []
        clips_count = 0
        images_count = 0

        # Scan folder items (non-recursive to avoid huge subtrees unless needed)
        for item in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
            if item.is_file():
                ext = item.suffix.lower()
                if ext in ALL_SUPPORTED_EXTENSIONS:
                    file_info = extract_media_metadata(str(item), base_date=base_date)
                    if file_info["is_valid"]:
                        if file_info["media_type"] == "video":
                            clips_count += 1
                        elif file_info["media_type"] == "image":
                            images_count += 1
                        discovered_files.append(file_info)

        return {
            "success": True,
            "folder_path": str(folder.resolve()),
            "folder_name": folder.name,
            "clips_count": clips_count,
            "images_count": images_count,
            "files": discovered_files
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to scan directory: {str(e)}",
            "clips_count": 0,
            "images_count": 0,
            "files": []
        }


def extract_frame_at_timestamp(video_path, target_timestamp, video_start_time, output_dir=None):
    """
    Extracts a single frame from video at the target timestamp.
    Avoids reading entire video into RAM by seeking directly using OpenCV.
    """
    path = Path(video_path)
    if not path.exists():
        return None, "Video file not found"

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None, "Failed to open video"

    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

        # Calculate offset in seconds from video_start_time
        offset_seconds = (target_timestamp - video_start_time).total_seconds() if video_start_time else 0.0
        if offset_seconds < 0:
            offset_seconds = 0.0

        target_frame_num = int(offset_seconds * fps)
        if total_frames > 0 and target_frame_num >= total_frames:
            target_frame_num = max(0, total_frames - 1)

        # Evaluate candidate frames for optimal visual clarity, contrast & passenger visibility
        candidates = []
        if total_frames > 0:
            primary = max(0, min(total_frames - 1, target_frame_num))
            candidates.append(primary)
            # Add secondary candidates at 35%, 50%, 65% of the video duration to find the clearest cabin view
            for frac in [0.35, 0.50, 0.65]:
                f_cand = int(total_frames * frac)
                if f_cand not in candidates and 0 <= f_cand < total_frames:
                    candidates.append(f_cand)
        else:
            candidates.append(0)

        import numpy as np
        best_frame = None
        best_score = -1.0
        best_frame_num = target_frame_num

        for cand_num in candidates:
            cap.set(cv2.CAP_PROP_POS_FRAMES, cand_num)
            ret, cand_frame = cap.read()
            if not ret or cand_frame is None:
                continue

            gray = cv2.cvtColor(cand_frame, cv2.COLOR_BGR2GRAY)
            mean_val = float(np.mean(gray))
            if mean_val < 15 or mean_val > 240:
                score = 0.0
            else:
                lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
                # Closeness factor to target offset
                dist_penalty = 1.0 - (min(abs(cand_num - target_frame_num), total_frames) / max(total_frames, 1)) * 0.25
                score = lap_var * dist_penalty

            if score > best_score:
                best_score = score
                best_frame = cand_frame
                best_frame_num = cand_num

        if best_frame is None:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, best_frame = cap.read()
            if not ret or best_frame is None:
                return None, "Failed to read frame from video"
            best_frame_num = 0

        frame = best_frame
        target_frame_num = best_frame_num

        # Save extracted frame to output_dir
        if output_dir is None:
            output_dir = Config.UPLOAD_FOLDER / "frames"
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        frame_fname = f"frame_{path.stem}_{target_frame_num}_{int(datetime.utcnow().timestamp())}.jpg"
        out_path = output_dir / frame_fname
        cv2.imwrite(str(out_path), frame)

        return {
            "frame_path": str(out_path),
            "frame_filename": frame_fname,
            "frame_number": target_frame_num,
            "frame_timestamp": target_timestamp,
            "width": frame.shape[1],
            "height": frame.shape[0]
        }, None
    except Exception as e:
        return None, str(e)
    finally:
        cap.release()
