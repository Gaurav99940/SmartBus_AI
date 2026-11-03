import cv2
import numpy as np

def assess_face_quality(face_bgr, min_face_size=28, blur_threshold=45.0):
    """
    Assesses face image quality based on:
    - Resolution / pixel dimensions
    - Blur (Laplacian variance)
    - Illumination (mean brightness and dynamic range)
    - Aspect ratio (human face proportion)

    Returns:
    {
        "is_acceptable": bool,
        "is_blur": bool,
        "is_too_small": bool,
        "is_poor_lighting": bool,
        "blur_score": float,
        "brightness": float,
        "quality_score": float, # 0.0 to 1.0
        "reasons": list of str
    }
    """
    h, w = face_bgr.shape[:2]
    reasons = []

    # 1. Size check
    is_too_small = (w < min_face_size or h < min_face_size)
    if is_too_small:
        reasons.append(f"Face is too small ({w}x{h}px < {min_face_size}px). May yield unreliable embedding.")

    # Convert to grayscale for blur and brightness
    gray = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2GRAY) if len(face_bgr.shape) == 3 else face_bgr

    # 2. Blur assessment using Laplacian variance
    laplacian_var = float(cv2.Laplacian(gray, cv2.CV_64F).var()) if (h > 4 and w > 4) else 0.0
    is_blur = laplacian_var < blur_threshold
    if is_blur:
        reasons.append(f"Blur detected (variance={laplacian_var:.1f} < threshold={blur_threshold}).")

    # 3. Illumination check
    mean_val = float(np.mean(gray))
    std_val = float(np.std(gray))
    is_poor_lighting = False
    if mean_val < 35:
        is_poor_lighting = True
        reasons.append("Face is severely underexposed/dark.")
    elif mean_val > 230:
        is_poor_lighting = True
        reasons.append("Face is severely overexposed/blown out.")
    elif std_val < 15:
        is_poor_lighting = True
        reasons.append("Face has very low contrast/flat lighting.")

    # 4. Aspect ratio check (human face is roughly 0.65 to 1.45 w/h)
    aspect_ratio = float(w) / float(h) if h > 0 else 1.0
    if aspect_ratio < 0.45 or aspect_ratio > 1.8:
        reasons.append(f"Unusual face bounding box aspect ratio ({aspect_ratio:.2f}).")

    # Compute overall quality score [0.0 - 1.0]
    # Penalize based on blur, size, lighting
    size_penalty = 0.4 if is_too_small else min(1.0, (w * h) / (100 * 100)) * 0.3
    blur_score_normalized = min(1.0, laplacian_var / (blur_threshold * 2.0)) * 0.4
    lighting_score = (1.0 - abs(mean_val - 128.0) / 128.0) * 0.3
    quality_score = max(0.05, min(1.0, size_penalty + blur_score_normalized + lighting_score))

    is_acceptable = not (is_too_small and is_blur)

    return {
        "is_acceptable": is_acceptable,
        "is_blur": is_blur,
        "is_too_small": is_too_small,
        "is_poor_lighting": is_poor_lighting,
        "blur_score": round(laplacian_var, 2),
        "brightness": round(mean_val, 1),
        "quality_score": round(quality_score, 2),
        "reasons": reasons
    }
