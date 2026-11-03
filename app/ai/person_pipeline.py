import os
import uuid
from pathlib import Path
import cv2
import numpy as np
import torch
from PIL import Image
from facenet_pytorch import MTCNN, InceptionResnetV1

from app.config import Config
from app.ai.quality import assess_face_quality

# Lazy-loaded singletons
_yolo_model = None
_mtcnn = None
_resnet = None
_hog_detector = None


def get_yolo_model():
    """Returns singleton YOLO person detector with safe fallback."""
    global _yolo_model
    if _yolo_model is None:
        try:
            # Shield against Windows Python 3.13 permission errors during Git inspection in user directory
            orig_exists = Path.exists
            def _safe_exists(self, *args, **kwargs):
                try:
                    return orig_exists(self, *args, **kwargs)
                except PermissionError:
                    return False
            Path.exists = _safe_exists

            from ultralytics import YOLO
            model_path = Config.YOLO_MODEL_PATH
            if not Path(model_path).is_absolute():
                local_path = Config.BASE_DIR / model_path
                if local_path.exists():
                    model_path = str(local_path)
            _yolo_model = YOLO(model_path)
        except Exception as e:
            print(f"[PersonDetector] YOLO load warning: {e}. Falling back to OpenCV HOG.")
            _yolo_model = False
    return _yolo_model


def get_face_models():
    """Returns singleton instances of MTCNN and InceptionResnetV1."""
    global _mtcnn, _resnet
    if _mtcnn is None:
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        _mtcnn = MTCNN(
            image_size=160,
            margin=14,
            min_face_size=Config.MIN_FACE_SIZE,
            thresholds=[0.6, 0.7, 0.7],
            factor=0.709,
            post_process=True,
            keep_all=True,
            device=device
        )
    if _resnet is None:
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        _resnet = InceptionResnetV1(pretrained="vggface2").eval().to(device)
    return _mtcnn, _resnet


def get_hog_detector():
    """Fallback HOG person detector if YOLO unavailable."""
    global _hog_detector
    if _hog_detector is None:
        try:
            hog = cv2.HOGDescriptor()
            hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
            _hog_detector = hog
        except Exception:
            _hog_detector = False
    return _hog_detector


def assess_image_overall_quality(img_bgr):
    """
    Checks if an interior bus photo is too blurry, dark, or overexposed.
    Returns: (is_usable: bool, warnings: list)
    """
    warnings = []
    if img_bgr is None or img_bgr.size == 0:
        return False, ["Unable to load image file."]

    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    mean_brightness = float(np.mean(gray))
    std_brightness = float(np.std(gray))

    # Dark check
    if mean_brightness < 25:
        warnings.append("Image is extremely dark; person detection confidence may be degraded.")
    elif mean_brightness < 45:
        warnings.append("Low interior lighting detected in bus cabin.")

    # High exposure check
    if mean_brightness > 235:
        warnings.append("Image is heavily overexposed; visible facial details may be washed out.")

    # Blur check (Laplacian variance)
    lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()
    if lap_var < 20.0:
        warnings.append("Severe motion blur or lens defocus detected.")
    elif lap_var < 40.0:
        warnings.append("Noticeable camera blur in bus interior capture.")

    # Low contrast check
    if std_brightness < 18:
        warnings.append("Low image contrast detected.")

    return True, warnings


def extract_clothing_signature(crop_bgr, face_bbox_in_crop=None):
    """
    Computes a multi-space, normalized clothing and appearance signature
    specifically isolating the passenger's upper body / clothing region:
    - Isolates torso region (below face or middle 18% to 85% height, 15% to 85% width)
    - Color moments (mean and standard deviation of BGR and LAB)
    - 3D LAB Color Histogram (4x4x4 bins = 64) normalized L1 (perceptually uniform)
    - 3D HSV Color Histogram (8x4x4 bins = 128) normalized L1
    - L2-normalized so dot product equals cosine similarity.
    """
    if crop_bgr is None or crop_bgr.size == 0:
        return np.zeros(206, dtype=np.float32)

    h, w = crop_bgr.shape[:2]
    if face_bbox_in_crop and len(face_bbox_in_crop) == 4:
        fx, fy, fw, fh = face_bbox_in_crop
        y_start = min(int(h * 0.70), max(0, fy + fh))
        y_end = int(h * 0.90)
        x_start = max(0, int(w * 0.10))
        x_end = min(w, int(w * 0.90))
    else:
        y_start = int(h * 0.18)
        y_end = int(h * 0.85)
        x_start = int(w * 0.15)
        x_end = int(w * 0.85)

    torso = crop_bgr[y_start:y_end, x_start:x_end]
    if torso.size == 0:
        torso = crop_bgr

    try:
        bgr_mean = torso.mean(axis=(0, 1)) / 255.0
        bgr_std = torso.std(axis=(0, 1)) / 255.0

        lab = cv2.cvtColor(torso, cv2.COLOR_BGR2LAB)
        lab_mean = lab.mean(axis=(0, 1)) / 255.0
        lab_std = lab.std(axis=(0, 1)) / 255.0

        hist_lab = cv2.calcHist([lab], [0, 1, 2], None, [4, 4, 4], [0, 256, 0, 256, 0, 256])
        hist_lab = cv2.normalize(hist_lab, None, 1.0, 0, cv2.NORM_L1).flatten()

        hsv = cv2.cvtColor(torso, cv2.COLOR_BGR2HSV)
        hist_hsv = cv2.calcHist([hsv], [0, 1, 2], None, [8, 4, 4], [0, 180, 0, 256, 0, 256])
        hist_hsv = cv2.normalize(hist_hsv, None, 1.0, 0, cv2.NORM_L1).flatten()

        feat = np.concatenate([bgr_mean, bgr_std, lab_mean, lab_std, hist_lab, hist_hsv]).astype(np.float32)
        norm = np.linalg.norm(feat)
        if norm > 0:
            feat /= norm
        return feat
    except Exception:
        return np.zeros(206, dtype=np.float32)


def extract_appearance_feature(crop_bgr):
    """
    Computes a robust visual appearance signature for person re-identification.
    Returns 206-element list of normalized floats.
    """
    sig = extract_clothing_signature(crop_bgr)
    return sig.tolist() if sig is not None else None


def detect_persons_and_faces(image_path, camera_angle="A", roi_config=None):
    """
    Analyzes an interior bus photo:
    1. Evaluates overall lighting and blur quality.
    2. Uses YOLO to detect visible human bodies (face & body, back turned, occluded, seated, aisle).
    3. Runs MTCNN face detection on detected persons to extract faces & biometric embeddings.
    4. Categorizes detection confidence: High, Medium, Low, Needs Review.
    5. Saves crops for inspection (face crop if available, or upper body crop).

    Returns:
    {
        "detections": list of dicts,
        "warnings": list of warning strings,
        "image_shape": (h, w),
        "raw_count": int,
        "high_confidence_count": int,
        "needs_review_count": int,
        "uncertain_count": int
    }
    """
    img_bgr = cv2.imread(str(image_path))
    if img_bgr is None:
        raise ValueError(f"Could not load image at {image_path}")

    img_h, img_w = img_bgr.shape[:2]
    is_usable, warnings = assess_image_overall_quality(img_bgr)

    crops_dir = Path(Config.CROP_FOLDER)
    crops_dir.mkdir(parents=True, exist_ok=True)

    yolo = get_yolo_model()
    raw_boxes = [] # [x1, y1, x2, y2, conf]

    if yolo:
        try:
            # Optimized imgsz=640 and half=False for fast CPU/GPU inference
            results = yolo(str(image_path), classes=[0], conf=Config.DETECTION_CONF_LOW, imgsz=640, verbose=False)
            if results and len(results) > 0 and results[0].boxes is not None:
                for b in results[0].boxes:
                    conf = float(b.conf[0])
                    coords = [int(v) for v in b.xyxy[0]]
                    raw_boxes.append((coords[0], coords[1], coords[2], coords[3], conf))
        except Exception as e:
            warnings.append(f"YOLO detection warning: {str(e)}")

    # Fallback to OpenCV HOG if YOLO yielded nothing or failed
    if len(raw_boxes) == 0:
        hog = get_hog_detector()
        if hog:
            try:
                rects, weights = hog.detectMultiScale(img_bgr, winStride=(4, 4), padding=(8, 8), scale=1.05)
                for i, (hx, hy, hw, hh) in enumerate(rects):
                    h_conf = float(weights[i]) if i < len(weights) else 0.50
                    # Normalize hog weight roughly into [0.35, 0.85]
                    norm_conf = min(0.85, max(0.35, 0.4 + h_conf * 0.3))
                    raw_boxes.append((hx, hy, hx + hw, hy + hh, norm_conf))
            except Exception as e:
                warnings.append(f"HOG fallback detection warning: {str(e)}")

    # Also check MTCNN face detector as complementary fallback
    # to catch any passengers seated closely whose faces are visible even if YOLO missed torso
    mtcnn, resnet = get_face_models()
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    # Sort boxes by y2 descending or conf descending
    raw_boxes.sort(key=lambda b: (b[4], b[3] - b[1]), reverse=True)

    # Non-maximum suppression / deduplication of overlapping boxes
    deduped_boxes = []
    for b in raw_boxes:
        bx1, by1, bx2, by2, bconf = b
        # Boundary clipping
        bx1 = max(0, min(bx1, img_w - 1))
        by1 = max(0, min(by1, img_h - 1))
        bx2 = max(0, min(bx2, img_w))
        by2 = max(0, min(by2, img_h))
        bw = bx2 - bx1
        bh = by2 - by1
        if bw < 15 or bh < 20:
            continue

        overlap = False
        for db in deduped_boxes:
            dx1, dy1, dx2, dy2, _ = db
            inter_x1 = max(bx1, dx1)
            inter_y1 = max(by1, dy1)
            inter_x2 = min(bx2, dx2)
            inter_y2 = min(by2, dy2)
            if inter_x2 > inter_x1 and inter_y2 > inter_y1:
                inter_area = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
                db_area = (dx2 - dx1) * (dy2 - dy1)
                b_area = bw * bh
                min_area = min(db_area, b_area)
                union_area = db_area + b_area - inter_area
                iou = inter_area / (union_area + 1e-6)
                iom = inter_area / (min_area + 1e-6)
                if iou > 0.40 or iom > 0.52:
                    overlap = True
                    break
        if not overlap:
            deduped_boxes.append((bx1, by1, bx2, by2, bconf))

    detections = []
    high_conf_count = 0
    needs_review_count = 0
    uncertain_count = 0

    for idx, (x1, y1, x2, y2, det_conf) in enumerate(deduped_boxes):
        w = x2 - x1
        h = y2 - y1
        temp_id = f"T-{camera_angle}-{idx + 1:02d}"

        # Classify confidence category
        if det_conf >= Config.DETECTION_CONF_HIGH:
            conf_label = "High"
            high_conf_count += 1
        elif det_conf >= Config.DETECTION_CONF_MEDIUM:
            conf_label = "Medium"
        else:
            conf_label = "Low"
            needs_review_count += 1

        # Check ROI overlap if configured
        in_assigned_roi = True
        if roi_config and isinstance(roi_config, dict):
            rx_min = int(roi_config.get("x_min", 0.0) * img_w)
            ry_min = int(roi_config.get("y_min", 0.0) * img_h)
            rx_max = int(roi_config.get("x_max", 1.0) * img_w)
            ry_max = int(roi_config.get("y_max", 1.0) * img_h)
            center_x = x1 + w // 2
            center_y = y1 + h // 2
            in_assigned_roi = (rx_min <= center_x <= rx_max and ry_min <= center_y <= ry_max)

        # Extract person crop
        person_crop_bgr = img_bgr[y1:y2, x1:x2]
        crop_filename = f"crop_{camera_angle}_{uuid.uuid4().hex[:10]}.jpg"
        crop_abs_path = crops_dir / crop_filename
        crop_rel_path = f"crops/{crop_filename}"

        # Detect face inside person box
        has_face = False
        face_confidence = 0.0
        face_embedding = None
        face_bbox_in_img = None
        face_quality = {
            "is_acceptable": True,
            "quality_score": 1.0,
            "is_blur": False,
            "brightness": 128.0
        }

        # Try MTCNN on person upper region with progressive fallback
        upper_h = max(int(h * 0.65), min(h, 160))
        upper_crop_bgr = person_crop_bgr[:upper_h, :]
        fboxes, fprobs = None, None

        if upper_crop_bgr.size > 0:
            try:
                upper_rgb = cv2.cvtColor(upper_crop_bgr, cv2.COLOR_BGR2RGB)
                fboxes, fprobs = mtcnn.detect(Image.fromarray(upper_rgb))
            except Exception as e:
                print(f"[PersonDetector] Face detect error: {e}")

        # Fallback to wider upper body (85% height) if no face detected in top 65%
        if (fboxes is None or len(fboxes) == 0 or (fprobs is not None and max(fprobs) < 0.50)) and h > 50:
            extended_h = max(int(h * 0.85), upper_h)
            extended_crop_bgr = person_crop_bgr[:extended_h, :]
            if extended_crop_bgr.size > 0:
                try:
                    ext_rgb = cv2.cvtColor(extended_crop_bgr, cv2.COLOR_BGR2RGB)
                    fb_ext, fp_ext = mtcnn.detect(Image.fromarray(ext_rgb))
                    if fb_ext is not None and len(fb_ext) > 0 and fp_ext is not None and max(fp_ext) >= 0.50:
                        fboxes, fprobs = fb_ext, fp_ext
                except Exception:
                    pass

        if fboxes is not None and len(fboxes) > 0 and fprobs is not None:
            # Strict Spatial Face-to-Body Intersection Over Union (IoU > 0.85) & Containment:
            # 1. Face bounding box must be strictly inside the target person body box (overlap >= 85%)
            # 2. Face center must lie strictly in the top 50% of the person crop (head region)
            # 3. Face dimensions must be realistic (width >= 14% of person width, height >= 10%)
            valid_candidates = []
            for f_i, (fb, fp) in enumerate(zip(fboxes, fprobs)):
                if fp < 0.50:
                    continue
                fx1_c, fy1_c, fx2_c, fy2_c = fb
                fw_c = fx2_c - fx1_c
                fh_c = fy2_c - fy1_c
                if fw_c <= 0 or fh_c <= 0:
                    continue

                fy_center = (fy1_c + fy2_c) / 2.0
                fx_center = (fx1_c + fx2_c) / 2.0

                # Must be located strictly in upper 50% of person crop
                if fy_center > (h * 0.50):
                    continue

                # Must be reasonably sized relative to person crop
                if fw_c < (w * 0.14) or fh_c < (h * 0.10):
                    continue

                # Intersection over Face Area (Containment IoU > 0.85)
                inter_w = max(0.0, min(fx2_c, float(w)) - max(fx1_c, 0.0))
                inter_h = max(0.0, min(fy2_c, float(h)) - max(fy1_c, 0.0))
                face_containment_iou = (inter_w * inter_h) / (fw_c * fh_c)
                if face_containment_iou < 0.85:
                    continue

                x_offset = abs(fx_center - (w / 2.0)) / (w / 2.0 + 1e-6)
                centrality = 1.0 - min(1.0, x_offset)
                area = fw_c * fh_c
                c_score = fp * (area ** 0.5) * (0.6 + 0.4 * centrality)
                valid_candidates.append((c_score, f_i, fb, fp))

            if valid_candidates:
                valid_candidates.sort(key=lambda x: x[0], reverse=True)
                _, best_fidx, best_fb, fprob = valid_candidates[0]
                fx1, fy1, fx2, fy2 = [int(v) for v in best_fb]
                fprob = float(fprob)
                # Convert to image coordinates
                fx1_img = max(0, min(x1 + fx1, img_w - 1))
                fy1_img = max(0, min(y1 + fy1, img_h - 1))
                fx2_img = max(0, min(x1 + fx2, img_w))
                fy2_img = max(0, min(y1 + fy2, img_h))
                fw = fx2_img - fx1_img
                fh = fy2_img - fy1_img

                if fw >= 12 and fh >= 12:
                    # Apply 15% margin padding around face box for optimal InceptionResnetV1 alignment
                    pad_x = int(0.15 * fw)
                    pad_y = int(0.15 * fh)
                    fx1_pad = max(0, fx1_img - pad_x)
                    fy1_pad = max(0, fy1_img - pad_y)
                    fx2_pad = min(img_w, fx2_img + pad_x)
                    fy2_pad = min(img_h, fy2_img + pad_y)

                    face_crop_bgr = img_bgr[fy1_pad:fy2_pad, fx1_pad:fx2_pad]
                    if face_crop_bgr.size > 0:
                        face_quality = assess_face_quality(
                            face_crop_bgr,
                            min_face_size=Config.MIN_FACE_SIZE,
                            blur_threshold=Config.BLUR_THRESHOLD
                        )
                        has_face = True
                        face_confidence = round(fprob, 3)
                        face_bbox_in_img = [fx1_img, fy1_img, fw, fh]

                        face_crop_filename = f"face_{camera_angle}_{uuid.uuid4().hex[:10]}.jpg"
                        face_crop_abs = crops_dir / face_crop_filename
                        cv2.imwrite(str(face_crop_abs), face_crop_bgr)
                        face_crop_rel = f"crops/{face_crop_filename}"

                        # Compute standardized, L2-normalized 512-dim embedding
                        try:
                            face_rgb = cv2.cvtColor(face_crop_bgr, cv2.COLOR_BGR2RGB)
                            face_resized = cv2.resize(face_rgb, (160, 160))
                            tensor = torch.tensor(face_resized, dtype=torch.float32).permute(2, 0, 1)
                            tensor = (tensor - 127.5) / 128.0
                            tensor = tensor.unsqueeze(0).to(device)
                            with torch.no_grad():
                                emb = resnet(tensor).squeeze().cpu().numpy()
                                norm = np.linalg.norm(emb)
                                if norm > 0:
                                    emb = emb / norm
                                face_embedding = emb.tolist()
                        except Exception as e:
                            print(f"[PersonDetector] Embedding warning: {e}")

        # Always save person upper body crop to crop_abs_path so clothes & appearance features are preserved
        if person_crop_bgr.size > 0:
            cv2.imwrite(str(crop_abs_path), person_crop_bgr)
        else:
            crop_rel_path = None

        if not has_face:
            uncertain_count += 1
            if conf_label == "Low":
                needs_review_count += 0 # already counted
            else:
                needs_review_count += 1
                conf_label = "Needs Review"

        face_local_box = [fx1, fy1, fw, fh] if has_face else None
        clothes_sig = extract_clothing_signature(person_crop_bgr, face_bbox_in_crop=face_local_box)
        norm_bbox = [float(x1) / img_w, float(y1) / img_h, float(w) / img_w, float(h) / img_h]

        # Use face crop as primary thumbnail if available and good quality, else person crop
        primary_thumb = (face_crop_rel if (has_face and face_quality.get("is_acceptable", True)) else crop_rel_path) or crop_rel_path

        detections.append({
            "temp_id": temp_id,
            "camera_angle": camera_angle,
            "bbox": [x1, y1, w, h],
            "norm_bbox": norm_bbox,
            "person_bbox": [x1, y1, w, h],
            "face_bbox": face_bbox_in_img,
            "detection_confidence": round(det_conf, 3),
            "face_confidence": face_confidence,
            "confidence_label": conf_label,
            "has_face": has_face,
            "quality": face_quality,
            "embedding": face_embedding,
            "embedding_model_version": "facenet_vggface2_512",
            "clothing_signature": clothes_sig.tolist(),
            "appearance_feature": clothes_sig.tolist(),
            "crop_path": primary_thumb,
            "crop_abs_path": str(crop_abs_path) if crop_rel_path else None,
            "face_crop_path": face_crop_rel if has_face else None,
            "body_crop_path": crop_rel_path,
            "in_assigned_roi": in_assigned_roi,
            "review_status": "NEEDS_REVIEW" if (conf_label == "Needs Review" or not has_face) else "CONFIRMED"
        })

    # Intra-frame duplicate face deduplication
    final_detections = []
    for d in detections:
        is_dup = False
        if d.get("embedding") is not None:
            d_emb = np.array(d["embedding"], dtype=np.float32)
            d_box = d.get("face_bbox") or d.get("bbox")
            for fd in final_detections:
                if fd.get("embedding") is not None:
                    fd_emb = np.array(fd["embedding"], dtype=np.float32)
                    sim = float(np.dot(d_emb, fd_emb))
                    fd_box = fd.get("face_bbox") or fd.get("bbox")
                    b_iom = 0.0
                    if d_box and fd_box:
                        ix1 = max(d_box[0], fd_box[0])
                        iy1 = max(d_box[1], fd_box[1])
                        ix2 = min(d_box[0] + d_box[2], fd_box[0] + fd_box[2])
                        iy2 = min(d_box[1] + d_box[3], fd_box[1] + fd_box[3])
                        if ix2 > ix1 and iy2 > iy1:
                            ia = (ix2 - ix1) * (iy2 - iy1)
                            b_iom = ia / (min(d_box[2]*d_box[3], fd_box[2]*fd_box[3]) + 1e-6)
                    if sim >= 0.90 or (sim >= 0.82 and b_iom > 0.40):
                        is_dup = True
                        break
        if not is_dup:
            final_detections.append(d)

    detections = final_detections

    return {
        "detections": detections,
        "warnings": warnings,
        "image_shape": (img_h, img_w),
        "raw_count": len(detections),
        "high_confidence_count": sum(1 for d in detections if d.get("confidence_label") == "High"),
        "needs_review_count": sum(1 for d in detections if d.get("confidence_label") == "Needs Review"),
        "uncertain_count": sum(1 for d in detections if not d.get("has_face"))
    }


def render_multi_angle_annotated_hud(original_image_path, detections, output_path, camera_angle="A", seating_label=""):
    """
    Renders sleek, modern HUD annotation for interior bus photo:
    - Bounding box colored by status & confidence:
      * Emerald Green: Confidently identified passenger (P001, etc.)
      * Cyan/Blue: Detected passenger (unidentified body)
      * Amber/Orange: Low confidence detection
      * Coral/Red: Needs Review / Uncertain match
    - HUD corner brackets
    - Crisp label with Person ID / Temp ID, confidence %, and status
    - Header banner displaying Camera Angle and Seating Section coverage
    """
    img = cv2.imread(str(original_image_path))
    if img is None:
        return None

    img_h, img_w = img.shape[:2]

    # Color definitions (BGR)
    COLOR_CONFIRMED = (113, 204, 46)     # Emerald Green
    COLOR_UNIDENTIFIED = (185, 128, 41)  # Deep Sky Blue
    COLOR_LOW_CONF = (18, 156, 243)      # Amber/Orange
    COLOR_REVIEW = (60, 76, 231)         # Vibrant Coral Red

    for det in detections:
        bbox = det.get("person_bbox") or det.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        x, y, w, h = bbox
        pid = det.get("person_id")
        temp_id = det.get("temp_id", "T-01")
        conf_label = det.get("confidence_label", "Medium")
        det_conf = det.get("detection_confidence", 0.0)
        sim_score = det.get("similarity_score", 0.0)
        has_id = pid and not pid.startswith("T-") and pid != "Unidentified" and pid != "Unknown"
        is_review = det.get("review_status") == "NEEDS_REVIEW" or conf_label == "Needs Review" or det.get("is_potential_duplicate")

        # Pick color
        if is_review:
            box_color = COLOR_REVIEW
        elif has_id:
            box_color = COLOR_CONFIRMED
        elif conf_label == "Low":
            box_color = COLOR_LOW_CONF
        else:
            box_color = COLOR_UNIDENTIFIED

        # Main bounding box
        thickness = 2
        cv2.rectangle(img, (x, y), (x + w, y + h), box_color, thickness)

        # Sleek corner accents (HUD look)
        corner_len = min(16, max(6, w // 4), max(6, h // 4))
        if corner_len > 4:
            cv2.line(img, (x, y), (x + corner_len, y), box_color, thickness + 2)
            cv2.line(img, (x, y), (x, y + corner_len), box_color, thickness + 2)
            cv2.line(img, (x + w, y), (x + w - corner_len, y), box_color, thickness + 2)
            cv2.line(img, (x + w, y), (x + w, y + corner_len), box_color, thickness + 2)
            cv2.line(img, (x, y + h), (x + corner_len, y + h), box_color, thickness + 2)
            cv2.line(img, (x, y + h), (x, y + h - corner_len), box_color, thickness + 2)
            cv2.line(img, (x + w, y + h), (x + w - corner_len, y + h), box_color, thickness + 2)
            cv2.line(img, (x + w, y + h), (x + w, y + h - corner_len), box_color, thickness + 2)

        # If a face was separately located, draw subtle dotted/inner box around face
        if det.get("face_bbox"):
            fx, fy, fw, fh = det["face_bbox"]
            cv2.rectangle(img, (fx, fy), (fx + fw, fy + fh), (255, 255, 255), 1)

        # Label text
        if has_id:
            if sim_score > 0:
                tag_text = f"{pid} ({int(sim_score * 100)}%)"
            else:
                tag_text = f"{pid} [Confirmed]"
        else:
            pct = int(det_conf * 100)
            tag_text = f"{temp_id} ({pct}%)"
            if is_review:
                tag_text += " [Review]"

        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.52
        font_thickness = 1
        (tw, th), _ = cv2.getTextSize(tag_text, font, font_scale, font_thickness)

        tag_y1 = max(0, y - th - 8)
        tag_y2 = y
        tag_x1 = x
        tag_x2 = min(img_w, x + tw + 10)

        # Draw tag background
        cv2.rectangle(img, (tag_x1, tag_y1), (tag_x2, tag_y2), box_color, cv2.FILLED)
        cv2.putText(img, tag_text, (tag_x1 + 5, tag_y2 - 4), font, font_scale, (255, 255, 255), font_thickness, cv2.LINE_AA)

    # Top angle banner
    header_text = f"ANGLE {camera_angle} - {seating_label or 'Assigned Seating Section'} | Visible Count: {len(detections)}"
    cv2.rectangle(img, (10, 10), (min(img_w - 10, 520), 40), (20, 20, 20), cv2.FILLED)
    cv2.putText(img, header_text, (18, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)

    # Save
    out_dir = Path(output_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path), img)
    return str(output_path)
