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

# Lazy-loaded singleton models
_mtcnn = None
_resnet = None
_cv_cascade = None

def get_face_models():
    """
    Returns singleton instances of MTCNN and InceptionResnetV1.
    """
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


# Safe cascade or detector helper
def get_fallback_detector():
    global _cv_cascade
    if _cv_cascade is None:
        try:
            if hasattr(cv2, 'CascadeClassifier') and hasattr(cv2, 'data') and hasattr(cv2.data, 'haarcascades'):
                cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
                _cv_cascade = cv2.CascadeClassifier(cascade_path)
            else:
                _cv_cascade = False
        except Exception:
            _cv_cascade = False
    return _cv_cascade


def detect_and_extract_faces(image_path):
    """
    Runs face detection on an interior bus image, crops faces, evaluates quality,
    and calculates 512-dimensional L2-normalized embeddings.

    Returns:
    List of dicts:
    [
        {
            "bbox": [x, y, w, h],
            "detector_prob": float,
            "quality": dict,
            "crop_path": str, # relative or absolute path to saved crop
            "embedding": list of 512 floats,
            "face_img_bgr": np.ndarray (in memory)
        },
        ...
    ]
    """
    mtcnn, resnet = get_face_models()
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    # Read original image via OpenCV
    img_bgr = cv2.imread(str(image_path))
    if img_bgr is None:
        raise ValueError(f"Could not load image at {image_path}")

    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    img_pil = Image.fromarray(img_rgb)
    img_h, img_w = img_bgr.shape[:2]

    detected_faces = []

    # 1. Detect using MTCNN
    boxes, probs = None, None
    try:
        boxes, probs = mtcnn.detect(img_pil)
    except Exception as e:
        print(f"[FacePipeline] MTCNN detection warning: {e}")

    # Fallback if MTCNN detected nothing and fallback is available
    if (boxes is None or len(boxes) == 0):
        detector = get_fallback_detector()
        if detector:
            try:
                gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
                cv_boxes = detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(Config.MIN_FACE_SIZE, Config.MIN_FACE_SIZE))
                if len(cv_boxes) > 0:
                    boxes = []
                    probs = []
                    for (cx, cy, cw, ch) in cv_boxes:
                        boxes.append([cx, cy, cx + cw, cy + ch])
                        probs.append(0.80)
                    boxes = np.array(boxes)
                    probs = np.array(probs)
            except Exception:
                pass

    if boxes is None or len(boxes) == 0:
        return detected_faces, img_bgr

    # Ensure crops directory exists
    crops_dir = Path(Config.CROP_FOLDER)
    crops_dir.mkdir(parents=True, exist_ok=True)

    for i, box in enumerate(boxes):
        prob = float(probs[i]) if probs is not None and probs[i] is not None else 0.5
        x1, y1, x2, y2 = [int(v) for v in box]

        # Apply boundary checks
        x1 = max(0, min(x1, img_w - 1))
        y1 = max(0, min(y1, img_h - 1))
        x2 = max(0, min(x2, img_w))
        y2 = max(0, min(y2, img_h))

        w = x2 - x1
        h = y2 - y1

        if w < 10 or h < 10:
            continue

        # Extract face crop
        face_crop_bgr = img_bgr[y1:y2, x1:x2]
        if face_crop_bgr.size == 0:
            continue

        # Assess quality
        quality = assess_face_quality(face_crop_bgr, min_face_size=Config.MIN_FACE_SIZE, blur_threshold=Config.BLUR_THRESHOLD)

        # Generate unique filename for face crop
        crop_filename = f"crop_{uuid.uuid4().hex[:12]}.jpg"
        crop_abs_path = crops_dir / crop_filename
        cv2.imwrite(str(crop_abs_path), face_crop_bgr)
        crop_rel_path = f"crops/{crop_filename}"

        # Compute face embedding
        embedding = None
        try:
            # Prepare tensor for InceptionResnetV1
            # Resize to 160x160 RGB
            face_crop_rgb = cv2.cvtColor(face_crop_bgr, cv2.COLOR_BGR2RGB)
            face_resized = cv2.resize(face_crop_rgb, (160, 160))
            # Standard facenet normalization: (x - 127.5) / 128.0
            face_tensor = torch.tensor(face_resized, dtype=torch.float32).permute(2, 0, 1) # [3, 160, 160]
            face_tensor = (face_tensor - 127.5) / 128.0
            face_tensor = face_tensor.unsqueeze(0).to(device)

            with torch.no_grad():
                emb = resnet(face_tensor)
                emb = emb.squeeze().cpu().numpy()
                # L2 normalize
                norm = np.linalg.norm(emb)
                if norm > 0:
                    emb = emb / norm
                embedding = emb.tolist()
        except Exception as e:
            print(f"[FacePipeline] Error computing embedding for face {i}: {e}")

        # Extract clothing signature from upper body region directly below face
        clothing_sig = None
        try:
            body_y2 = min(img_h, y1 + int(h * 3.5))
            body_x1 = max(0, x1 - int(w * 0.75))
            body_x2 = min(img_w, x1 + int(w * 1.75))
            torso_crop = img_bgr[y1:body_y2, body_x1:body_x2]
            if torso_crop.size > 0:
                from app.ai.person_pipeline import extract_clothing_signature
                cs = extract_clothing_signature(torso_crop)
                if cs is not None:
                    clothing_sig = cs.tolist()
        except Exception as e:
            print(f"[FacePipeline] Clothing extraction error: {e}")

        detected_faces.append({
            "bbox": [x1, y1, w, h],
            "detector_prob": round(prob, 3),
            "quality": quality,
            "crop_path": crop_rel_path,
            "crop_abs_path": str(crop_abs_path),
            "embedding": embedding,
            "clothing_signature": clothing_sig,
            "face_img_bgr": face_crop_bgr
        })

    return detected_faces, img_bgr


def render_annotated_image(original_image_path, assignments, output_path):
    """
    Renders clean, modern bounding boxes and labels for each passenger detected.
    assignments: List of dicts with keys:
      - 'bbox': [x, y, w, h]
      - 'person_id': 'P001'
      - 'similarity_score': float
      - 'confidence_label': 'High' | 'Medium' | 'Low' | 'Needs Review'
      - 'is_blur': bool
    """
    img = cv2.imread(str(original_image_path))
    if img is None:
        return None

    # Color scheme (BGR)
    COLORS = {
        "High": (46, 204, 113),       # Bright Emerald Green
        "Medium": (41, 128, 185),     # Blue
        "Low": (26, 188, 156),        # Turquoise / Cyan
        "Needs Review": (39, 39, 235) # Vibrant Coral Red
    }

    for item in assignments:
        bbox = item["bbox"]
        x, y, w, h = bbox
        pid = item.get("person_id", "Unknown")
        conf_label = item.get("confidence_label", "Medium")
        sim_score = item.get("similarity_score", 0.0)
        is_blur = item.get("is_blur", False)

        color = COLORS.get(conf_label, (255, 165, 0))

        # Main bounding box
        thickness = 2
        cv2.rectangle(img, (x, y), (x + w, y + h), color, thickness)

        # Subtle corner accents for a sleek modern HUD look
        corner_len = min(16, w // 4, h // 4)
        if corner_len > 4:
            # Top-left
            cv2.line(img, (x, y), (x + corner_len, y), color, thickness + 2)
            cv2.line(img, (x, y), (x, y + corner_len), color, thickness + 2)
            # Top-right
            cv2.line(img, (x + w, y), (x + w - corner_len, y), color, thickness + 2)
            cv2.line(img, (x + w, y), (x + w, y + corner_len), color, thickness + 2)
            # Bottom-left
            cv2.line(img, (x, y + h), (x + corner_len, y + h), color, thickness + 2)
            cv2.line(img, (x, y + h), (x, y + h - corner_len), color, thickness + 2)
            # Bottom-right
            cv2.line(img, (x + w, y + h), (x + w - corner_len, y + h), color, thickness + 2)
            cv2.line(img, (x + w, y + h), (x + w, y + h - corner_len), color, thickness + 2)

        # Label tag
        if sim_score > 0:
            tag_text = f"{pid} ({int(sim_score * 100)}%)"
        else:
            tag_text = f"{pid} [New]"

        if is_blur:
            tag_text += " [Blur]"

        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.55
        font_thickness = 1
        (text_w, text_h), baseline = cv2.getTextSize(tag_text, font, font_scale, font_thickness)

        tag_y1 = max(0, y - text_h - 10)
        tag_y2 = y
        tag_x1 = x
        tag_x2 = x + text_w + 12

        # Draw filled tag background
        cv2.rectangle(img, (tag_x1, tag_y1), (tag_x2, tag_y2), color, cv2.FILLED)
        # White text with crisp rendering
        cv2.putText(img, tag_text, (tag_x1 + 6, tag_y2 - 5), font, font_scale, (255, 255, 255), font_thickness, cv2.LINE_AA)

    # Save annotated output
    out_dir = Path(output_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path), img)
    return str(output_path)
