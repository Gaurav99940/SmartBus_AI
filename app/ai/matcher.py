import json
import re
import numpy as np
import cv2
from pathlib import Path
from app.config import Config
from app.models import PassengerIdentity, PassengerObservation
from app.database import db

MODEL_VERSION = "facenet_vggface2_512"

# Calibrated Robust Biometric Cosine Similarity Thresholds (InceptionResnetV1 / FaceNet)
# Strict thresholds to prevent multi-passenger face embedding drift:
THRESH_STRONG_FACE = 0.75       # Unambiguous confident face match
THRESH_SUPPORTED_FACE = 0.60    # Solid face match with lighting/angle variation (supported by clothes/gallery)
THRESH_AMBIGUOUS_FACE = 0.52    # Borderline similarity: requires high clothing match or flagged as NEEDS_REVIEW
THRESH_CONFLICT_CUTOFF = 0.58   # Hard Biometric Cutoff: Faces with similarity < 0.58 to centroid are strictly DIFFERENT people
THRESH_DISTINCT_MARGIN = 0.05   # Margin needed from competing second-best match


def check_biometric_identity_conflict(candidate_emb, passenger_obj, conflict_threshold=THRESH_CONFLICT_CUTOFF):
    """
    Validates that assigning candidate_emb to passenger_obj will NOT create
    an identity collision or feature drift. ONE PERSON ID = ONE REAL PASSENGER.
    Returns: (has_conflict: bool, max_sim: float, conflict_reason: str)
    """
    if candidate_emb is None or passenger_obj is None:
        return False, 1.0, ""

    c_emb = np.array(candidate_emb, dtype=np.float32)
    c_norm = np.linalg.norm(c_emb)
    if c_norm == 0:
        return False, 1.0, ""
    c_emb = c_emb / c_norm

    refs = get_passenger_reference_embeddings(passenger_obj)
    if not refs:
        return False, 1.0, ""

    sims = [cosine_similarity(c_emb, ref) for ref in refs]
    max_sim = max(sims)
    min_sim = min(sims)

    # Multi-Stage Validation Step 1: Centroid of Top-K Verified Faces
    top_k_refs = refs[:min(5, len(refs))]
    centroid = np.mean(top_k_refs, axis=0)
    cent_norm = np.linalg.norm(centroid)
    if cent_norm > 0:
        centroid /= cent_norm
    centroid_sim = cosine_similarity(c_emb, centroid)

    # Centroid Hard Veto (sim < conflict_threshold)
    if centroid_sim < conflict_threshold:
        return True, centroid_sim, f"Biometric conflict: candidate face diverges from passenger centroid ({centroid_sim:.3f} < {conflict_threshold:.2f})"

    # Multi-Stage Validation Step 2: Primary Immutable Anchor Face Consistency
    anchor_sim = cosine_similarity(c_emb, refs[0])
    if anchor_sim < 0.56:
        return True, anchor_sim, f"Biometric conflict: candidate face diverges from anchor face ({anchor_sim:.3f} < 0.56)"

    # Multi-Stage Validation Step 3: Gallery Contradiction Check
    if len(refs) > 1 and min_sim < 0.40:
        return True, min_sim, f"Biometric conflict: candidate contradicts gallery pose ({min_sim:.3f} < 0.40)"

    return False, max_sim, ""


def compute_box_iou(b1, b2, img_shape=(720, 1280)):
    """
    Computes Intersection-over-Union between two boxes [x, y, w, h].
    Works with both normalized coordinates and pixel coordinates.
    """
    if not b1 or not b2 or len(b1) < 4 or len(b2) < 4:
        return 0.0
    is_norm1 = max(b1[:4]) <= 1.5
    is_norm2 = max(b2[:4]) <= 1.5
    h, w = img_shape
    box1 = [b1[0]*w, b1[1]*h, b1[2]*w, b1[3]*h] if (is_norm1 and not is_norm2) else list(b1[:4])
    box2 = [b2[0]*w, b2[1]*h, b2[2]*w, b2[3]*h] if (is_norm2 and not is_norm1) else list(b2[:4])
    x1, y1, w1, h1 = box1
    x2, y2, w2, h2 = box2
    ix1 = max(x1, x2)
    iy1 = max(y1, y2)
    ix2 = min(x1 + w1, x2 + w2)
    iy2 = min(y1 + h1, y2 + h2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    union = (w1 * h1) + (w2 * h2) - inter
    return float(inter / (union + 1e-6))


def compute_center_dist(b1, b2, img_shape=(720, 1280)):
    """
    Computes normalized center proximity score in range [0.0, 1.0].
    Works seamlessly with both normalized coordinates [0.0..1.0] and pixel coordinates.
    Distance <= 0.10 is same seat (score 1.0), decaying smoothly to 0.0 at distance >= 0.35.
    """
    if not b1 or not b2 or len(b1) < 4 or len(b2) < 4:
        return 0.0
    is_norm1 = max(b1[:4]) <= 1.5
    is_norm2 = max(b2[:4]) <= 1.5
    h, w = img_shape
    if is_norm1:
        c1 = (float(b1[0]) + float(b1[2]) / 2.0, float(b1[1]) + float(b1[3]) / 2.0)
    else:
        c1 = ((float(b1[0]) + float(b1[2]) / 2.0) / w, (float(b1[1]) + float(b1[3]) / 2.0) / h)

    if is_norm2:
        c2 = (float(b2[0]) + float(b2[2]) / 2.0, float(b2[1]) + float(b2[3]) / 2.0)
    else:
        c2 = ((float(b2[0]) + float(b2[2]) / 2.0) / w, (float(b2[1]) + float(b2[3]) / 2.0) / h)

    dist = np.sqrt((c1[0] - c2[0])**2 + (c1[1] - c2[1])**2)
    return float(max(0.0, 1.0 - (dist / 0.35)))


def get_crop_histogram(crop_rel_or_abs):
    """
    Loads crop from disk and returns normalized clothing feature vector.
    """
    if not crop_rel_or_abs:
        return None
    candidates = [
        Path(Config.DATA_DIR) / str(crop_rel_or_abs).replace("\\", "/").strip(),
        Path(Config.CROP_FOLDER) / Path(crop_rel_or_abs).name,
        Path(crop_rel_or_abs)
    ]
    for c in candidates:
        if c.exists() and c.is_file():
            try:
                im = cv2.imread(str(c))
                if im is not None and im.size > 0:
                    from app.ai.person_pipeline import extract_clothing_signature
                    return extract_clothing_signature(im)
            except Exception:
                pass
    return None


def cosine_similarity(vec_a, vec_b):
    """
    Computes cosine similarity between two 1D float vectors.
    Gracefully handles dimension differences (e.g. truncated clothing features).
    Returns float in range [-1.0, 1.0].
    """
    if vec_a is None or vec_b is None:
        return 0.0
    a = np.array(vec_a, dtype=np.float32)
    b = np.array(vec_b, dtype=np.float32)
    if a.size == 0 or b.size == 0:
        return 0.0
    if a.shape != b.shape:
        m = min(len(a), len(b))
        a = a[:m]
        b = b[:m]
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def generate_next_person_id(journey_id, allocated_ids=None):
    """
    Generates the next journey-scoped person ID: P001, P002, etc.
    Takes into account already allocated IDs in the current batch.
    """
    existing_pids = db.session.query(PassengerIdentity.person_id).filter_by(journey_id=journey_id).all()
    all_pids = set(pid for (pid,) in existing_pids if pid)
    if allocated_ids:
        all_pids.update(allocated_ids)

    max_num = 0
    for pid in all_pids:
        if pid:
            m = re.search(r'\d+', str(pid))
            if m:
                try:
                    num = int(m.group())
                    if num > max_num:
                        max_num = num
                except ValueError:
                    pass
    next_num = max_num + 1
    new_pid = f"P{next_num:03d}"
    if allocated_ids is not None:
        allocated_ids.add(new_pid)
    return new_pid


def get_passenger_reference_embeddings(p):
    """
    Retrieves all valid reference face embeddings for a passenger identity:
    1. Primary representative embedding
    2. Multi-reference gallery embeddings (diverse poses, lighting, angles)
    3. Stored observation embeddings from recent observations
    Returns a list of 1D L2-normalized float32 numpy arrays.
    """
    refs = []
    seen_reprs = set()

    # 1. Representative embedding
    if p.representative_embedding_json:
        try:
            arr = np.array(json.loads(p.representative_embedding_json), dtype=np.float32)
            norm = np.linalg.norm(arr)
            if norm > 0:
                arr = arr / norm
                refs.append(arr)
                seen_reprs.add(tuple(np.round(arr[:8], 3)))
        except Exception:
            pass

    # 2. Multi-reference gallery embeddings
    if hasattr(p, 'gallery_embeddings_json') and p.gallery_embeddings_json:
        try:
            gallery = json.loads(p.gallery_embeddings_json)
            if isinstance(gallery, list):
                for item in gallery:
                    arr = np.array(item, dtype=np.float32)
                    norm = np.linalg.norm(arr)
                    if norm > 0:
                        arr = arr / norm
                        sig = tuple(np.round(arr[:8], 3))
                        if sig not in seen_reprs:
                            refs.append(arr)
                            seen_reprs.add(sig)
        except Exception:
            pass

    # 3. Recent observation embeddings if gallery has few entries
    if len(refs) < 4 and hasattr(p, 'observations'):
        for obs in reversed(p.observations[-8:]):
            if hasattr(obs, 'face_embedding_json') and obs.face_embedding_json:
                try:
                    arr = np.array(json.loads(obs.face_embedding_json), dtype=np.float32)
                    norm = np.linalg.norm(arr)
                    if norm > 0:
                        arr = arr / norm
                        sig = tuple(np.round(arr[:8], 3))
                        if sig not in seen_reprs:
                            refs.append(arr)
                            seen_reprs.add(sig)
                            if len(refs) >= 6:
                                break
                except Exception:
                    pass

    return refs


def compute_best_face_similarity(cand_emb, ref_embeddings):
    """
    Compares candidate face embedding against all reference embeddings for a passenger.
    Returns (best_similarity, top_matches_avg).
    """
    if cand_emb is None or not ref_embeddings:
        return 0.0, 0.0

    cand = np.array(cand_emb, dtype=np.float32)
    norm = np.linalg.norm(cand)
    if norm == 0:
        return 0.0, 0.0
    cand = cand / norm

    sims = [cosine_similarity(cand, ref) for ref in ref_embeddings]
    if not sims:
        return 0.0, 0.0
    
    sims.sort(reverse=True)
    best_sim = sims[0]
    top_avg = float(np.mean(sims[:min(3, len(sims))]))
    return float(best_sim), top_avg


def update_passenger_embedding(passenger_obj, new_embedding, alpha=0.85, min_similarity=None):
    """
    Updates the representative embedding using an exponential moving average
    so the biometric profile adapts smoothly to minor changes in lighting or angles.
    """
    if new_embedding is None:
        return
    curr_emb = np.array(new_embedding, dtype=np.float32)
    norm = np.linalg.norm(curr_emb)
    if norm > 0:
        curr_emb = curr_emb / norm

    if passenger_obj.representative_embedding_json:
        try:
            old_emb = np.array(json.loads(passenger_obj.representative_embedding_json), dtype=np.float32)
            old_norm = np.linalg.norm(old_emb)
            if old_norm > 0:
                old_emb = old_emb / old_norm
            # Anti-poisoning guard: if min_similarity specified and distance too great, reject blending
            if min_similarity is not None and cosine_similarity(old_emb, curr_emb) < min_similarity:
                return
            updated = alpha * old_emb + (1.0 - alpha) * curr_emb
            norm = np.linalg.norm(updated)
            if norm > 0:
                updated = updated / norm
            passenger_obj.representative_embedding_json = json.dumps(updated.tolist())
            passenger_obj.embedding_sample_count = (passenger_obj.embedding_sample_count or 1) + 1
        except Exception:
            passenger_obj.representative_embedding_json = json.dumps(curr_emb.tolist())
    else:
        passenger_obj.representative_embedding_json = json.dumps(curr_emb.tolist())


def update_passenger_gallery_and_embedding(passenger_obj, new_embedding, quality=None, alpha=0.90):
    """
    Updates the representative centroid embedding and maintains a diverse,
    high-quality reference gallery (up to 8 embeddings) across the journey.
    Anchor embedding (index 0) is IMMUTABLE and preserved forever.
    """
    if new_embedding is None or passenger_obj is None:
        return

    curr_emb = np.array(new_embedding, dtype=np.float32)
    curr_norm = np.linalg.norm(curr_emb)
    if curr_norm == 0:
        return
    curr_emb = curr_emb / curr_norm

    gallery = []
    if hasattr(passenger_obj, 'gallery_embeddings_json') and passenger_obj.gallery_embeddings_json:
        try:
            parsed = json.loads(passenger_obj.gallery_embeddings_json)
            if isinstance(parsed, list):
                gallery = [g for g in parsed if len(g) == len(curr_emb)]
        except Exception:
            gallery = []

    # If gallery is empty, seed index 0 as the immutable anchor
    if not gallery:
        gallery.append(curr_emb.tolist())
        passenger_obj.representative_embedding_json = json.dumps(curr_emb.tolist())
    else:
        anchor_emb = np.array(gallery[0], dtype=np.float32)
        sim_to_anchor = cosine_similarity(curr_emb, anchor_emb)
        sims_to_gallery = [cosine_similarity(curr_emb, np.array(g, dtype=np.float32)) for g in gallery]
        max_sim = max(sims_to_gallery)
        min_sim = min(sims_to_gallery)

        # Anti-poisoning guard:
        # Incorporate ONLY if confirmed same person:
        # 1. sim_to_anchor >= 0.45
        # 2. min_sim >= 0.40 (no conflict with any member in gallery)
        # 3. max_sim < 0.999 (not exact duplicate)
        if sim_to_anchor >= 0.45 and min_sim >= 0.40 and max_sim < 0.999:
            if len(gallery) < 8:
                gallery.append(curr_emb.tolist())
            else:
                # Replace the member that is most redundant (preserve index 0 anchor!)
                redundancies = [cosine_similarity(curr_emb, np.array(g, dtype=np.float32)) for g in gallery[1:]]
                replace_idx = int(np.argmax(redundancies)) + 1
                gallery[replace_idx] = curr_emb.tolist()

            # Update centroid smoothly with strong anchor weighting (alpha=0.90)
            update_passenger_embedding(passenger_obj, curr_emb.tolist(), alpha=0.90, min_similarity=0.52)

    if hasattr(passenger_obj, 'gallery_embeddings_json'):
        passenger_obj.gallery_embeddings_json = json.dumps(gallery)


def update_passenger_clothes(passenger_obj, new_clothes, alpha=0.85):
    """
    Updates representative clothing signature with anti-poisoning guard.
    """
    if new_clothes is None:
        return
    curr_sig = np.array(new_clothes, dtype=np.float32)
    norm = np.linalg.norm(curr_sig)
    if norm > 0:
        curr_sig = curr_sig / norm

    if hasattr(passenger_obj, 'representative_clothes_json') and passenger_obj.representative_clothes_json:
        try:
            old_sig = np.array(json.loads(passenger_obj.representative_clothes_json), dtype=np.float32)
            sim = cosine_similarity(old_sig, curr_sig)
            if sim < 0.60:
                return
            updated = alpha * old_sig + (1.0 - alpha) * curr_sig
            norm = np.linalg.norm(updated)
            if norm > 0:
                updated = updated / norm
            passenger_obj.representative_clothes_json = json.dumps(updated.tolist())
        except Exception:
            passenger_obj.representative_clothes_json = json.dumps(curr_sig.tolist())
    else:
        try:
            passenger_obj.representative_clothes_json = json.dumps(curr_sig.tolist())
        except Exception:
            pass


def match_faces_to_journey_identities(detected_faces, journey_id, high_thresh=None, med_thresh=None, low_thresh=None):
    """
    Matches detected faces in an upload to existing PassengerIdentity records
    for the journey using multi-reference FaceNet gallery + Clothing signature.

    Enforces 1-to-1 matching constraint (no person ID can be assigned twice in the same photo).
    Faces that confidently match existing passengers reuse their persistent ID.
    Only genuinely new passengers receive new person IDs.
    """
    if high_thresh is None:
        high_thresh = THRESH_STRONG_FACE
    if med_thresh is None:
        med_thresh = THRESH_SUPPORTED_FACE
    if low_thresh is None:
        low_thresh = THRESH_AMBIGUOUS_FACE

    existing_passengers = PassengerIdentity.query.filter_by(journey_id=journey_id).all()
    
    passenger_profiles = []
    seen_canonical = set()
    for p in existing_passengers:
        canon = p.get_canonical_identity() if hasattr(p, 'get_canonical_identity') else p
        if canon.id in seen_canonical:
            continue
        seen_canonical.add(canon.id)
        refs = get_passenger_reference_embeddings(canon)

        clothes_sigs = []
        if hasattr(canon, 'representative_clothes_json') and canon.representative_clothes_json:
            try:
                cs = np.array(json.loads(canon.representative_clothes_json), dtype=np.float32)
                clothes_sigs.append(cs)
            except Exception:
                pass

        if not clothes_sigs and canon.thumbnail_path:
            cs = get_crop_histogram(canon.thumbnail_path)
            if cs is not None:
                clothes_sigs.append(cs)

        passenger_profiles.append({
            "passenger": canon,
            "person_id": canon.person_id,
            "refs": refs,
            "clothes_signatures": clothes_sigs
        })

    assignments = []
    num_detections = len(detected_faces)
    num_known = len(passenger_profiles)
    allocated_batch_ids = set()

    if num_detections == 0:
        return assignments

    if num_known == 0:
        for face in detected_faces:
            new_pid = generate_next_person_id(journey_id, allocated_ids=allocated_batch_ids)
            assignments.append({
                "detection": face,
                "passenger_identity": None,
                "is_new": True,
                "similarity_score": 0.0,
                "confidence_label": "High" if face.get("quality", {}).get("is_acceptable", True) else "Needs Review",
                "person_id": new_pid,
                "status_update": "PRESENT"
            })
        return assignments

    # Pairwise candidate scoring: (score, det_idx, pass_idx, face_sim, clothes_sim)
    candidate_matrix = []
    for i, face in enumerate(detected_faces):
        det_emb = np.array(face["embedding"], dtype=np.float32) if face.get("embedding") is not None else None
        det_cs = np.array(face["clothing_signature"], dtype=np.float32) if face.get("clothing_signature") is not None else None
        if det_cs is None and face.get("crop_path"):
            det_cs = get_crop_histogram(face["crop_path"])

        for j, prof in enumerate(passenger_profiles):
            face_sim = 0.0
            has_face_emb = False
            if det_emb is not None and prof["refs"]:
                best_sim, _ = compute_best_face_similarity(det_emb, prof["refs"])
                face_sim = max(0.0, best_sim)
                has_face_emb = True

            clothes_sim = 0.0
            if det_cs is not None and prof["clothes_signatures"]:
                for cs in prof["clothes_signatures"]:
                    c_sim = max(0.0, cosine_similarity(det_cs, cs))
                    if c_sim > clothes_sim:
                        clothes_sim = c_sim

            score = 0.0
            has_clothes = bool(prof["clothes_signatures"] and det_cs is not None)
            if has_face_emb:
                # Hard biometric conflict check:
                has_conflict, max_sim, _ = check_biometric_identity_conflict(det_emb, prof["passenger"])
                if has_conflict:
                    score = 0.0
                elif not has_clothes:
                    # When clothing signature is not yet registered, rely directly on face biometrics
                    if face_sim >= THRESH_STRONG_FACE:
                        score = face_sim
                    elif face_sim >= 0.65:
                        score = face_sim
                    else:
                        score = 0.0
                elif face_sim >= THRESH_STRONG_FACE:
                    # Clear face match: face is primary evidence, clothing is secondary support
                    score = 0.90 * face_sim + 0.10 * max(0.0, clothes_sim)
                elif face_sim >= THRESH_SUPPORTED_FACE:
                    # Face similarity >= 0.60. Requires good clothing agreement
                    if clothes_sim >= 0.50:
                        score = 0.80 * face_sim + 0.20 * clothes_sim
                    else:
                        score = 0.0
                else:
                    # Face similarity < 0.60: STRICT HARD VETO!
                    # Clothing/body appearance is strictly FORBIDDEN from overriding low face similarity!
                    score = 0.0
            else:
                # No face available on detection:
                # CRITICAL RULE: If the passenger identity has a confirmed face, DO NOT match faceless detection to it!
                if prof["refs"]:
                    score = 0.0
                else:
                    p_seat = getattr(prof["passenger"], "current_seat", None)
                    det_seat = face.get("seat_zone")
                    seat_compatible = True
                    if p_seat and det_seat and p_seat != det_seat:
                        seat_compatible = False

                    if seat_compatible and clothes_sim >= 0.88:
                        score = clothes_sim * 0.85
                    else:
                        score = 0.0

            if score >= 0.55:
                candidate_matrix.append((score, i, j, face_sim, clothes_sim))

    # Sort descending by match score
    candidate_matrix.sort(key=lambda x: x[0], reverse=True)

    assigned_faces = set()
    assigned_passengers = set()

    # Distinctiveness check: inspect competing candidates per detection
    det_scores = {}
    for score, i, j, f_sim, c_sim in candidate_matrix:
        if i not in det_scores:
            det_scores[i] = []
        det_scores[i].append((score, j, f_sim))

    for score, i, j, face_sim, clothes_sim in candidate_matrix:
        if i in assigned_faces or j in assigned_passengers:
            continue

        # Check distinctiveness from runner up
        competing = det_scores.get(i, [])
        is_distinct = True
        if len(competing) > 1 and competing[0][1] == j:
            runner_up_score = competing[1][0]
            if (score - runner_up_score) < THRESH_DISTINCT_MARGIN and face_sim < THRESH_STRONG_FACE:
                is_distinct = False

        assigned_faces.add(i)
        assigned_passengers.add(j)
        p_obj = passenger_profiles[j]["passenger"]
        face = detected_faces[i]

        if not is_distinct or score < THRESH_SUPPORTED_FACE or face_sim < THRESH_SUPPORTED_FACE:
            label = "Needs Review"
        elif score >= THRESH_STRONG_FACE or face_sim >= THRESH_STRONG_FACE:
            label = "High"
        else:
            label = "Medium"

        face_q = face.get("quality", {})
        if face_q.get("is_blur") or not face_q.get("is_acceptable", True):
            label = "Needs Review"

        assignments.append({
            "detection": face,
            "passenger_identity": p_obj,
            "is_new": False,
            "similarity_score": float(score),
            "confidence_label": label,
            "person_id": p_obj.person_id,
            "status_update": "PRESENT"
        })

    # Unassigned detections: truly new passengers
    for i, face in enumerate(detected_faces):
        if i not in assigned_faces:
            face_q = face.get("quality", {})
            is_acceptable = face_q.get("is_acceptable", True) and not face_q.get("is_blur", False)
            new_pid = generate_next_person_id(journey_id, allocated_ids=allocated_batch_ids)
            assignments.append({
                "detection": face,
                "passenger_identity": None,
                "is_new": True,
                "similarity_score": 0.0,
                "confidence_label": "High" if is_acceptable else "Needs Review",
                "person_id": new_pid,
                "status_update": "PRESENT"
            })

    return assignments


def match_angle_detections_to_passengers(
    detections,
    camera_angle,
    journey_id,
    allocated_pids=None,
    already_matched_pids=None,
    in_progress_profiles=None
):
    """
    Robust multi-modal matcher for bus passenger re-identification across stops and angles.
    Differentiates and identifies passengers using:
    1. Multi-reference FaceNet gallery cosine similarity
    2. Upper-body clothing / appearance signature (LAB moments + 3D color histograms)
    3. Weak seat & spatial prior (only when face is completely absent; seat moves DO NOT break identity)

    Enforces 1-to-1 matching within each camera angle image.
    Preserves existing Passenger IDs across consecutive analyses (Stop 1 -> Stop 2 -> Stop 3...)
    so that present passengers have their presence confirmed and their last confirmed presence updated.
    Only truly new unique boarding passengers get newly generated Person IDs.
    """
    if allocated_pids is None:
        allocated_pids = set()

    existing_passengers = PassengerIdentity.query.filter_by(journey_id=journey_id).all()
    if (not existing_passengers and not in_progress_profiles) or not detections:
        # First batch in journey or no detections: assign new IDs
        for det in detections:
            if not det.get("person_id"):
                new_pid = generate_next_person_id(journey_id, allocated_ids=allocated_pids)
                allocated_pids.add(new_pid)
                det["person_id"] = new_pid
                det_q = det.get("quality", {})
                is_acceptable = det_q.get("is_acceptable", True) and not det_q.get("is_blur", False)
                det["confidence_label"] = "High" if (is_acceptable and det.get("detection_confidence", 0) >= Config.DETECTION_CONF_MEDIUM) else "Needs Review"
                det["review_status"] = "CONFIRMED" if det["confidence_label"] != "Needs Review" else "NEEDS_REVIEW"
                det["similarity_score"] = 0.0
        return detections

    # Build rich multi-modal feature profiles for all journey passengers
    passenger_profiles = []
    seen_canonical = set()
    for p in existing_passengers:
        canon = p.get_canonical_identity() if hasattr(p, 'get_canonical_identity') else p
        if canon.id in seen_canonical:
            continue
        seen_canonical.add(canon.id)
        refs = get_passenger_reference_embeddings(canon)

        clothes_sigs = []
        if hasattr(canon, 'representative_clothes_json') and canon.representative_clothes_json:
            try:
                cs = np.array(json.loads(canon.representative_clothes_json), dtype=np.float32)
                clothes_sigs.append(cs)
            except Exception:
                pass

        past_norm_boxes = []
        recent_obs = sorted(canon.observations, key=lambda o: o.id, reverse=True)[:6]
        for obs in recent_obs:
            if hasattr(obs, 'clothing_signature_json') and obs.clothing_signature_json:
                try:
                    cs = np.array(json.loads(obs.clothing_signature_json), dtype=np.float32)
                    clothes_sigs.append(cs)
                except Exception:
                    pass
            elif obs.crop_path:
                cs = get_crop_histogram(obs.crop_path)
                if cs is not None:
                    clothes_sigs.append(cs)

            if obs.camera_angle == camera_angle:
                img_w = 1280
                img_h = 720
                u = obs.upload
                if u and u.file_path and Path(u.file_path).exists():
                    try:
                        im = cv2.imread(u.file_path)
                        if im is not None:
                            img_h, img_w = im.shape[:2]
                    except Exception:
                        pass
                norm_b = [float(obs.bbox_x)/img_w, float(obs.bbox_y)/img_h, float(obs.bbox_w)/img_w, float(obs.bbox_h)/img_h]
                past_norm_boxes.append(norm_b)

        if not clothes_sigs and canon.thumbnail_path:
            cs = get_crop_histogram(canon.thumbnail_path)
            if cs is not None:
                clothes_sigs.append(cs)

        passenger_profiles.append({
            "passenger": canon,
            "person_id": canon.person_id,
            "refs": refs,
            "clothes_signatures": clothes_sigs,
            "past_norm_boxes": past_norm_boxes,
            "status": canon.status,
            "seat": canon.current_seat
        })

    # Also incorporate in-progress profiles from earlier angles in the current batch
    if in_progress_profiles:
        for ip in in_progress_profiles:
            # Check if this PID is already in passenger_profiles
            existing_prof = next((pr for pr in passenger_profiles if pr["person_id"] == ip["person_id"]), None)
            if existing_prof:
                if ip.get("embedding"):
                    e_arr = np.array(ip["embedding"], dtype=np.float32)
                    e_arr /= np.linalg.norm(e_arr)
                    existing_prof["refs"].append(e_arr)
            else:
                refs = []
                if ip.get("embedding"):
                    e_arr = np.array(ip["embedding"], dtype=np.float32)
                    e_arr /= np.linalg.norm(e_arr)
                    refs.append(e_arr)
                clothes_sigs = []
                if ip.get("clothing_signature"):
                    c_arr = np.array(ip["clothing_signature"], dtype=np.float32)
                    clothes_sigs.append(c_arr)
                elif ip.get("crop_path"):
                    cs = get_crop_histogram(ip["crop_path"])
                    if cs is not None:
                        clothes_sigs.append(cs)
                passenger_profiles.append({
                    "passenger": ip.get("passenger_identity"),
                    "person_id": ip["person_id"],
                    "refs": refs,
                    "clothes_signatures": clothes_sigs,
                    "past_norm_boxes": [],
                    "status": "PRESENT",
                    "seat": None
                })

    # Compute pairwise match scores between all detections and all passengers
    candidate_pairs = []

    for d_idx, det in enumerate(detections):
        det_norm_box = det.get("norm_bbox")
        if not det_norm_box:
            det_box = det.get("bbox") or [0, 0, 50, 50]
            det_norm_box = [float(det_box[0])/1280, float(det_box[1])/720, float(det_box[2])/1280, float(det_box[3])/720]

        det_emb = np.array(det["embedding"], dtype=np.float32) if det.get("embedding") is not None else None
        
        det_clothes = None
        if det.get("clothing_signature"):
            det_clothes = np.array(det["clothing_signature"], dtype=np.float32)
        elif det.get("appearance_feature"):
            det_clothes = np.array(det["appearance_feature"], dtype=np.float32)
        elif det.get("crop_path"):
            det_clothes = get_crop_histogram(det["crop_path"])

        for p_idx, prof in enumerate(passenger_profiles):
            face_sim = 0.0
            has_face_match = False
            if det_emb is not None and prof["refs"]:
                best_sim, _ = compute_best_face_similarity(det_emb, prof["refs"])
                face_sim = max(0.0, best_sim)
                has_face_match = True

            clothes_sim = 0.0
            if det_clothes is not None and prof["clothes_signatures"]:
                for cs in prof["clothes_signatures"]:
                    c_sim = max(0.0, cosine_similarity(det_clothes, cs))
                    if c_sim > clothes_sim:
                        clothes_sim = c_sim

            spat_sim = 0.0
            if prof["past_norm_boxes"]:
                for prev_b in prof["past_norm_boxes"]:
                    s = compute_center_dist(det_norm_box, prev_b)
                    iou = compute_box_iou(det_norm_box, prev_b)
                    comb_spat = max(s, iou)
                    if comb_spat > spat_sim:
                        spat_sim = comb_spat

            score = 0.0
            is_candidate = False

            if has_face_match:
                # Hard biometric conflict check:
                has_conflict = False
                if prof.get("passenger"):
                    has_conflict, max_sim, _ = check_biometric_identity_conflict(det_emb, prof["passenger"])
                elif prof["refs"]:
                    sims = [cosine_similarity(det_emb, ref) for ref in prof["refs"]]
                    has_conflict = (max(sims) < THRESH_CONFLICT_CUTOFF)

                has_clothes = bool(prof["clothes_signatures"] and det_clothes is not None)
                if has_conflict:
                    score = 0.0
                    is_candidate = False
                elif not has_clothes:
                    if face_sim >= THRESH_STRONG_FACE:
                        score = face_sim
                        is_candidate = True
                    elif face_sim >= 0.65:
                        score = face_sim
                        is_candidate = True
                    else:
                        score = 0.0
                        is_candidate = False
                elif face_sim >= THRESH_STRONG_FACE:
                    # Clear face match: face is primary biometric identity evidence
                    score = 0.90 * face_sim + 0.10 * max(0.0, clothes_sim)
                    is_candidate = True
                elif face_sim >= THRESH_SUPPORTED_FACE:
                    # Face similarity >= 0.60. Requires good clothing agreement
                    if clothes_sim >= 0.50:
                        score = 0.80 * face_sim + 0.20 * clothes_sim
                        is_candidate = True
                    else:
                        score = 0.0
                        is_candidate = False
                else:
                    # Face similarity < 0.60: STRICT HARD VETO!
                    # Clothing/body appearance is strictly FORBIDDEN from overriding low face similarity!
                    score = 0.0
                    is_candidate = False
            else:
                # Face not available on detection:
                # CRITICAL RULE: If the passenger identity has a confirmed face, DO NOT match faceless detection to it!
                if prof["refs"]:
                    score = 0.0
                    is_candidate = False
                else:
                    p_obj = prof.get("passenger")
                    p_status = p_obj.status if p_obj else prof.get("status", "PRESENT")
                    obs_angles = [o.camera_angle for o in p_obj.observations[-4:]] if (p_obj and p_obj.observations) else []
                    is_same_angle = (camera_angle in obs_angles) if obs_angles else True

                    if p_status in ["PRESENT", "POSSIBLY_ABSENT"] and is_same_angle and spat_sim >= 0.75 and clothes_sim >= 0.88:
                        score = 0.70 * clothes_sim + 0.30 * spat_sim
                        is_candidate = (score >= 0.85)
                    else:
                        score = 0.0
                        is_candidate = False

            if is_candidate and score >= 0.55:
                candidate_pairs.append((score, d_idx, p_idx, face_sim, spat_sim, clothes_sim))

    # Sort descending by match score
    candidate_pairs.sort(key=lambda x: x[0], reverse=True)

    # Distinctiveness map per detection index
    det_candidates = {}
    for score, d_idx, p_idx, f_sim, s_sim, c_sim in candidate_pairs:
        if d_idx not in det_candidates:
            det_candidates[d_idx] = []
        det_candidates[d_idx].append((score, p_idx, f_sim))

    matched_det_indices = set()
    matched_pass_in_this_angle = set()

    for score, d_idx, p_idx, face_sim, spat_sim, clothes_sim in candidate_pairs:
        if d_idx in matched_det_indices or p_idx in matched_pass_in_this_angle:
            continue

        prof = passenger_profiles[p_idx]
        p_obj = prof.get("passenger")

        # Distinctiveness check
        is_distinct = True
        competing = det_candidates.get(d_idx, [])
        if len(competing) > 1 and competing[0][1] == p_idx:
            runner_up_score = competing[1][0]
            if (score - runner_up_score) < THRESH_DISTINCT_MARGIN and face_sim < THRESH_STRONG_FACE:
                is_distinct = False

        matched_det_indices.add(d_idx)
        matched_pass_in_this_angle.add(p_idx)

        det = detections[d_idx]
        det["passenger_identity"] = p_obj
        det["person_id"] = prof["person_id"]
        det["similarity_score"] = float(score)

        if not is_distinct or score < THRESH_SUPPORTED_FACE or face_sim < THRESH_SUPPORTED_FACE:
            conf_label = "Needs Review"
        elif score >= THRESH_STRONG_FACE or face_sim >= THRESH_STRONG_FACE:
            conf_label = "High"
        else:
            conf_label = "Medium"

        det_q = det.get("quality", {})
        if det_q.get("is_blur") or not det_q.get("is_acceptable", True):
            conf_label = "Needs Review"

        det["confidence_label"] = conf_label
        det["review_status"] = "CONFIRMED" if conf_label != "Needs Review" else "NEEDS_REVIEW"
        if already_matched_pids is not None:
            already_matched_pids.add(prof["person_id"])

    # Remaining unmatched detections: genuinely new passengers who boarded
    for d_idx, det in enumerate(detections):
        if d_idx not in matched_det_indices:
            det_q = det.get("quality", {})
            is_acceptable = det_q.get("is_acceptable", True) and not det_q.get("is_blur", False)

            new_pid = generate_next_person_id(journey_id, allocated_ids=allocated_pids)
            if allocated_pids is not None:
                allocated_pids.add(new_pid)
            if already_matched_pids is not None:
                already_matched_pids.add(new_pid)

            det["passenger_identity"] = None
            det["person_id"] = new_pid
            det["similarity_score"] = 0.0
            det["confidence_label"] = "High" if (is_acceptable and det.get("detection_confidence", 0) >= Config.DETECTION_CONF_MEDIUM) else "Needs Review"
            det["review_status"] = "CONFIRMED" if det["confidence_label"] != "Needs Review" else "NEEDS_REVIEW"

    return detections
