import os
import cv2
import numpy as np
from pathlib import Path
from app.config import Config


def evaluate_frame_colorfulness(img_bgr):
    """
    Evaluates whether an individual frame contains authentic color information
    or is effectively black-and-white / grayscale stored in 3 channels.

    Returns dict:
    {
        "is_color": bool,
        "color_score": float, # 0.0 to 100.0+ (Hasler & Susstrunk metric)
        "hasler_metric": float,
        "mean_saturation": float,
        "active_pixel_ratio": float,
        "mean_channel_diff": float,
        "classification": "COLOR" | "BLACK_AND_WHITE" | "NEEDS_REVIEW"
    }
    """
    if img_bgr is None or img_bgr.size == 0:
        return {
            "is_color": False,
            "color_score": 0.0,
            "hasler_metric": 0.0,
            "mean_saturation": 0.0,
            "active_pixel_ratio": 0.0,
            "mean_channel_diff": 0.0,
            "classification": "BLACK_AND_WHITE",
            "reason": "Empty or unreadable image frame."
        }

    # If single channel (2D array), it is unambiguously grayscale
    if len(img_bgr.shape) == 2 or img_bgr.shape[2] == 1:
        return {
            "is_color": False,
            "color_score": 0.0,
            "hasler_metric": 0.0,
            "mean_saturation": 0.0,
            "active_pixel_ratio": 0.0,
            "mean_channel_diff": 0.0,
            "classification": "BLACK_AND_WHITE",
            "reason": "Single-channel grayscale image."
        }

    b = img_bgr[:, :, 0].astype(np.float32)
    g = img_bgr[:, :, 1].astype(np.float32)
    r = img_bgr[:, :, 2].astype(np.float32)

    # 1. Mean channel differences |B-G|, |G-R|, |R-B|
    diff_bg = np.abs(b - g)
    diff_gr = np.abs(g - r)
    diff_rb = np.abs(r - b)
    mean_ch_diff = float((np.mean(diff_bg) + np.mean(diff_gr) + np.mean(diff_rb)) / 3.0)

    # 2. HSV Saturation channel analysis
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    s_channel = hsv[:, :, 1].astype(np.float32)
    mean_sat = float(np.mean(s_channel))
    # Fraction of pixels with saturation > 25 (guards against compression noise on grayscale)
    active_pixel_ratio = float(np.mean(s_channel > 25.0))

    # 3. Hasler & Susstrunk Colorfulness Metric
    rg = r - g
    yb = 0.5 * (r + g) - b
    std_rg = np.std(rg)
    mean_rg = np.mean(rg)
    std_yb = np.std(yb)
    mean_yb = np.mean(yb)
    std_rgyb = np.sqrt(std_rg**2 + std_yb**2)
    mean_rgyb = np.sqrt(mean_rg**2 + mean_yb**2)
    hasler_metric = float(std_rgyb + 0.3 * mean_rgyb)

    # Thresholds from Config
    hasler_thresh = getattr(Config, "COLORFULNESS_METRIC_THRESHOLD", 9.5)
    sat_thresh = getattr(Config, "COLOR_SATURATION_THRESHOLD", 8.0)
    ch_diff_thresh = getattr(Config, "COLOR_CHANNEL_DIFF_THRESHOLD", 3.5)
    act_ratio_thresh = getattr(Config, "COLOR_ACTIVE_PIXEL_RATIO", 0.03)

    # Decision logic:
    # A true color image has significant channel differences, notable saturation, and high colorfulness metric.
    # Grayscale image (even with JPEG compression noise) has mean_ch_diff < 2.0, hasler < 5.0, mean_sat < 7.0.
    is_bw = (hasler_metric < hasler_thresh) and (mean_ch_diff < ch_diff_thresh or mean_sat < sat_thresh)

    # Low light / dark interior footage handling:
    # If image is dark (mean brightness < 40), check if any chromaticity exists above noise
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    mean_brightness = float(np.mean(gray))

    if mean_brightness < 40.0:
        # For dark footage, scale threshold down slightly but require chromaticity ratio > 0.02
        if hasler_metric < (hasler_thresh * 0.70) and mean_ch_diff < (ch_diff_thresh * 0.65):
            is_bw = True

    # Uncertain margin check
    uncertain_low = getattr(Config, "COLOR_UNCERTAIN_LOW", 6.5)
    uncertain_high = getattr(Config, "COLOR_UNCERTAIN_HIGH", 11.5)
    is_uncertain = (uncertain_low <= hasler_metric <= uncertain_high) and not is_bw

    if is_bw:
        classification = "BLACK_AND_WHITE"
        is_color = False
    elif is_uncertain:
        classification = "NEEDS_REVIEW"
        is_color = False # Cautiously skip if review policy requires
    else:
        classification = "COLOR"
        is_color = True

    return {
        "is_color": is_color,
        "color_score": round(hasler_metric, 2),
        "hasler_metric": round(hasler_metric, 2),
        "mean_saturation": round(mean_sat, 2),
        "active_pixel_ratio": round(active_pixel_ratio, 3),
        "mean_channel_diff": round(mean_ch_diff, 2),
        "mean_brightness": round(mean_brightness, 1),
        "classification": classification
    }


def evaluate_media_color_eligibility(media_path, sample_count=None):
    """
    Evaluates whether a video clip or image file is genuine color (eligible for AI recognition)
    or black-and-white / grayscale (must be skipped).

    For video files:
    Samples multiple representative frames across duration to ensure reliable classification.
    Never lets a single misleading colored frame classify an otherwise grayscale clip as valid.

    Returns dict:
    {
        "is_color": bool,
        "status": "COLOR" | "SKIPPED_BLACK_AND_WHITE" | "NEEDS_REVIEW",
        "color_score": float,
        "skip_reason": str | None,
        "sampled_frames_count": int,
        "color_frames_count": int,
        "details": dict
    }
    """
    path = Path(media_path)
    if not path.exists():
        return {
            "is_color": False,
            "status": "ERROR_FILE_NOT_FOUND",
            "color_score": 0.0,
            "skip_reason": f"File does not exist: {media_path}",
            "sampled_frames_count": 0,
            "color_frames_count": 0,
            "details": {}
        }

    ext = path.suffix.lower()
    video_exts = {".mp4", ".avi", ".mov", ".mkv", ".wmv", ".webm", ".m4v"}

    if ext not in video_exts:
        # Single image evaluation
        img = cv2.imread(str(path))
        if img is None:
            return {
                "is_color": False,
                "status": "SKIPPED_BLACK_AND_WHITE",
                "color_score": 0.0,
                "skip_reason": f"Unable to read image file: {path.name}",
                "sampled_frames_count": 1,
                "color_frames_count": 0,
                "details": {}
            }
        eval_res = evaluate_frame_colorfulness(img)
        if not eval_res["is_color"]:
            skip_msg = f"Black-and-white / grayscale footage skipped before AI inference (Colorfulness: {eval_res['color_score']}, Saturation: {eval_res['mean_saturation']}, Channel Diff: {eval_res['mean_channel_diff']})"
            return {
                "is_color": False,
                "status": "SKIPPED_BLACK_AND_WHITE" if eval_res["classification"] == "BLACK_AND_WHITE" else "NEEDS_REVIEW",
                "color_score": eval_res["color_score"],
                "skip_reason": skip_msg,
                "sampled_frames_count": 1,
                "color_frames_count": 0,
                "details": eval_res
            }
        else:
            return {
                "is_color": True,
                "status": "COLOR",
                "color_score": eval_res["color_score"],
                "skip_reason": None,
                "sampled_frames_count": 1,
                "color_frames_count": 1,
                "details": eval_res
            }

    # Video file multi-frame sampling
    if sample_count is None:
        sample_count = getattr(Config, "COLOR_FRAME_SAMPLE_COUNT", 5)

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return {
            "is_color": False,
            "status": "SKIPPED_BLACK_AND_WHITE",
            "color_score": 0.0,
            "skip_reason": f"Unable to open video: {path.name}",
            "sampled_frames_count": 0,
            "color_frames_count": 0,
            "details": {}
        }

    try:
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total_frames <= 0:
            sample_fractions = [0.5]
        else:
            # Spread sampling points: 15%, 35%, 50%, 65%, 85%
            sample_fractions = [0.15, 0.35, 0.50, 0.65, 0.85][:sample_count]

        frame_results = []
        color_count = 0
        bw_count = 0
        review_count = 0

        for frac in sample_fractions:
            frame_num = int(total_frames * frac) if total_frames > 0 else 0
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
            ret, frame = cap.read()
            if not ret or frame is None:
                continue

            res = evaluate_frame_colorfulness(frame)
            res["frame_number"] = frame_num
            frame_results.append(res)
            if res["is_color"]:
                color_count += 1
            elif res["classification"] == "BLACK_AND_WHITE":
                bw_count += 1
            else:
                review_count += 1

        total_sampled = len(frame_results)
        if total_sampled == 0:
            return {
                "is_color": False,
                "status": "SKIPPED_BLACK_AND_WHITE",
                "color_score": 0.0,
                "skip_reason": "No readable video frames extracted for color analysis.",
                "sampled_frames_count": 0,
                "color_frames_count": 0,
                "details": {}
            }

        avg_score = float(np.mean([f["color_score"] for f in frame_results]))
        color_ratio = color_count / total_sampled
        min_color_ratio = getattr(Config, "COLOR_MIN_COLOR_FRAME_RATIO", 0.60)

        # Segment-level and majority policy:
        # A clip is eligible ONLY if at least min_color_ratio of sampled frames are authentic color.
        # Grayscale clips with a few color artifacts or glitches are rejected.
        if color_ratio >= min_color_ratio and avg_score >= getattr(Config, "COLORFULNESS_METRIC_THRESHOLD", 9.5):
            return {
                "is_color": True,
                "status": "COLOR",
                "color_score": round(avg_score, 2),
                "skip_reason": None,
                "sampled_frames_count": total_sampled,
                "color_frames_count": color_count,
                "details": {
                    "color_ratio": round(color_ratio, 2),
                    "frame_results": frame_results
                }
            }
        else:
            skip_msg = f"Black-and-white / grayscale footage skipped before AI inference ({bw_count}/{total_sampled} frames grayscale, Avg Colorfulness: {avg_score:.1f})"
            return {
                "is_color": False,
                "status": "SKIPPED_BLACK_AND_WHITE",
                "color_score": round(avg_score, 2),
                "skip_reason": skip_msg,
                "sampled_frames_count": total_sampled,
                "color_frames_count": color_count,
                "details": {
                    "color_ratio": round(color_ratio, 2),
                    "frame_results": frame_results
                }
            }
    finally:
        cap.release()
