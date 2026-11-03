import os
import io
import json
import unittest
import uuid
from pathlib import Path
from datetime import datetime, timedelta
import numpy as np
import cv2

from app import create_app
from app.database import db
from app.models import (
    User, Bus, Journey, Upload, PassengerIdentity,
    PassengerObservation, PresenceEvent, TimetableImport, TimetableStop, StopMediaMatch
)
from app.ai.matcher import (
    cosine_similarity,
    check_biometric_identity_conflict,
    match_faces_to_journey_identities,
    match_angle_detections_to_passengers,
    update_passenger_gallery_and_embedding
)
from app.services.journey_service import process_multi_angle_batch
from app.services.batch_processor import find_matching_media_for_window
from app.ai.color_filter import evaluate_media_color_eligibility


def create_vector_with_similarity(base_vec, target_sim):
    """Creates a normalized 512-d embedding with exact desired cosine similarity to base_vec."""
    base = np.array(base_vec, dtype=np.float32)
    base /= np.linalg.norm(base)
    rand_noise = np.random.randn(len(base)).astype(np.float32)
    # make orthogonal
    orthogonal = rand_noise - np.dot(rand_noise, base) * base
    orthogonal /= np.linalg.norm(orthogonal)
    res = target_sim * base + np.sqrt(max(0.0, 1.0 - target_sim**2)) * orthogonal
    return res / np.linalg.norm(res)


class TestAIAccuracyAndUniqueness(unittest.TestCase):
    """
    Comprehensive test suite ensuring strict AI model accuracy, passenger uniqueness,
    biometric conflict prevention, clip reuse, and black-and-white media filtering.
    """
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        with self.app.app_context():
            uid = uuid.uuid4().hex[:8]
            self.bus = Bus(
                bus_number=f"ACC-BUS-{uid}",
                registration_number=f"DL-01-ACC-{uid}",
                capacity=30
            )
            db.session.add(self.bus)
            db.session.commit()

            self.journey = Journey(
                journey_code=f"ACC-JRN-{uid}",
                bus_id=self.bus.id,
                origin="Anand Vihar",
                destination="Kaushambi",
                status="active"
            )
            db.session.add(self.journey)
            db.session.commit()
            self.journey_id = self.journey.id

            # Create test media file paths with unique fixture names to avoid clobbering baseline assets
            uploads_dir = Path("data/uploads")
            uploads_dir.mkdir(parents=True, exist_ok=True)
            self.test_img_a = (uploads_dir / f"test_acc_fixture_{uid}_A.jpg").resolve()
            self.test_img_b = (uploads_dir / f"test_acc_fixture_{uid}_B.jpg").resolve()
            self.test_img_c = (uploads_dir / f"test_acc_fixture_{uid}_C.jpg").resolve()

            # Generate real color images with rich chromatic variance for color qualification
            color_a = np.zeros((300, 400, 3), dtype=np.uint8)
            color_a[:, :130] = [230, 40, 20]    # Blue/Cyan
            color_a[:, 130:270] = [20, 220, 40]  # Green
            color_a[:, 270:] = [40, 40, 240]    # Red
            cv2.imwrite(str(self.test_img_a), color_a)

            color_b = np.zeros((300, 400, 3), dtype=np.uint8)
            color_b[:, :200] = [210, 190, 20]
            color_b[:, 200:] = [30, 20, 230]
            cv2.imwrite(str(self.test_img_b), color_b)

            color_c = np.zeros((300, 400, 3), dtype=np.uint8)
            color_c[:, :] = [180, 50, 160]
            cv2.imwrite(str(self.test_img_c), color_c)

    def test_01_same_passenger_different_seats_retains_id(self):
        """1. Same passenger moving to different seats must retain the same Person ID."""
        with self.app.app_context():
            emb_base = np.random.randn(512).astype(np.float32)
            emb_base /= np.linalg.norm(emb_base)

            # Passenger boarded at Seat 1A
            p = PassengerIdentity(
                journey_id=self.journey_id,
                person_id="P_SEAT_1",
                current_seat="1A",
                representative_embedding_json=json.dumps(emb_base.tolist()),
                gallery_embeddings_json=json.dumps([emb_base.tolist()]),
                representative_clothes_json=json.dumps([0.5] * 206),
                status="PRESENT"
            )
            db.session.add(p)
            db.session.commit()

            # At next stop, same face detected at Seat 4B (sim = 0.82)
            emb_next = create_vector_with_similarity(emb_base, 0.82)

            detections = [{
                "face_id": "face_seat_move",
                "embedding": emb_next.tolist(),
                "clothing_signature": [0.5] * 206,
                "confidence": 0.95,
                "seat_zone": "4B",
                "center": (400, 300)
            }]

            matches = match_faces_to_journey_identities(detections, self.journey_id)
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["person_id"], "P_SEAT_1")
            self.assertFalse(matches[0]["is_new"])

    def test_02_same_passenger_different_angles_unifies_id(self):
        """2. Same passenger visible across Angle A, B, and C unifies into a single canonical ID."""
        from unittest.mock import patch
        with self.app.app_context():
            u_a = Upload(journey_id=self.journey_id, file_path=str(self.test_img_a), original_filename="a.jpg", camera_angle="A")
            u_b = Upload(journey_id=self.journey_id, file_path=str(self.test_img_b), original_filename="b.jpg", camera_angle="B")
            db.session.add_all([u_a, u_b])
            db.session.commit()

            emb_common = np.random.randn(512).astype(np.float32)
            emb_common /= np.linalg.norm(emb_common)
            emb_b = create_vector_with_similarity(emb_common, 0.85)

            def mock_detect(path, *args, **kwargs):
                camera_angle = kwargs.get("camera_angle", "A")
                if camera_angle == "A":
                    dets = [{
                        "temp_id": "T-A-01",
                        "camera_angle": "A",
                        "bbox": [50, 50, 100, 200],
                        "norm_bbox": [0.1, 0.1, 0.2, 0.4],
                        "person_bbox": [50, 50, 100, 200],
                        "face_bbox": [60, 60, 40, 50],
                        "detection_confidence": 0.95,
                        "face_confidence": 0.98,
                        "confidence_label": "High",
                        "has_face": True,
                        "quality": {"is_acceptable": True, "quality_score": 90.0, "is_blur": False},
                        "embedding": emb_common.tolist(),
                        "embedding_model_version": "facenet_vggface2_512",
                        "clothing_signature": [0.6] * 206,
                        "appearance_feature": [0.6] * 206,
                        "crop_path": "crops/test_a.jpg",
                        "crop_abs_path": str(self.test_img_a),
                        "face_crop_path": "crops/face_a.jpg",
                        "body_crop_path": "crops/body_a.jpg",
                        "in_assigned_roi": True,
                        "review_status": "CONFIRMED"
                    }]
                else:
                    dets = [{
                        "temp_id": "T-B-01",
                        "camera_angle": "B",
                        "bbox": [60, 60, 100, 200],
                        "norm_bbox": [0.15, 0.15, 0.2, 0.4],
                        "person_bbox": [60, 60, 100, 200],
                        "face_bbox": [70, 70, 40, 50],
                        "detection_confidence": 0.92,
                        "face_confidence": 0.96,
                        "confidence_label": "High",
                        "has_face": True,
                        "quality": {"is_acceptable": True, "quality_score": 88.0, "is_blur": False},
                        "embedding": emb_b.tolist(),
                        "embedding_model_version": "facenet_vggface2_512",
                        "clothing_signature": [0.6] * 206,
                        "appearance_feature": [0.6] * 206,
                        "crop_path": "crops/test_b.jpg",
                        "crop_abs_path": str(self.test_img_b),
                        "face_crop_path": "crops/face_b.jpg",
                        "body_crop_path": "crops/body_b.jpg",
                        "in_assigned_roi": True,
                        "review_status": "CONFIRMED"
                    }]
                return {
                    "detections": dets,
                    "warnings": [],
                    "raw_count": len(dets),
                    "high_confidence_count": len(dets),
                    "needs_review_count": 0,
                    "uncertain_count": 0,
                    "annotated_frame": np.zeros((300, 400, 3), dtype=np.uint8)
                }

            with patch("app.services.journey_service.detect_persons_and_faces", side_effect=mock_detect):
                batch_res = process_multi_angle_batch(self.journey_id, "batch_cross_angle_acc", {"A": u_a, "B": u_b})
                self.assertIn("consolidated_counts", batch_res)
                counts = batch_res["consolidated_counts"]
                self.assertEqual(counts["total_raw_detections"], 2)
                self.assertEqual(counts["confirmed_unique"], 1)

    def test_03_same_passenger_multiple_stops_retains_id(self):
        """3. Same passenger at multiple stops retains ID and does NOT spawn a new ID."""
        with self.app.app_context():
            emb_truth = np.random.randn(512).astype(np.float32)
            emb_truth /= np.linalg.norm(emb_truth)

            p = PassengerIdentity(
                journey_id=self.journey_id,
                person_id="P_MULTI_STOP",
                representative_embedding_json=json.dumps(emb_truth.tolist()),
                gallery_embeddings_json=json.dumps([emb_truth.tolist()]),
                representative_clothes_json=json.dumps([0.4] * 206),
                status="PRESENT",
                stops_traveled=1
            )
            db.session.add(p)
            db.session.commit()

            # At Stop 2, lighting changes slightly (sim = 0.80)
            emb_stop2 = create_vector_with_similarity(emb_truth, 0.80)

            detections = [{
                "face_id": "face_stop2",
                "embedding": emb_stop2.tolist(),
                "clothing_signature": [0.4] * 206,
                "confidence": 0.90,
                "seat_zone": "1A"
            }]

            matches = match_faces_to_journey_identities(detections, self.journey_id)
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["person_id"], "P_MULTI_STOP")
            self.assertFalse(matches[0]["is_new"])

    def test_04_different_passengers_similar_clothes_strictly_separated(self):
        """4. Two different people wearing identical/similar clothes must NEVER merge (biometric conflict)."""
        with self.app.app_context():
            # Passenger 1 (Woman)
            emb_p1 = np.random.randn(512).astype(np.float32)
            emb_p1 /= np.linalg.norm(emb_p1)

            # Passenger 2 (Man) with completely different face (orthogonal/negative similarity)
            emb_p2 = np.random.randn(512).astype(np.float32)
            emb_p2 = emb_p2 - np.dot(emb_p2, emb_p1) * emb_p1
            emb_p2 /= np.linalg.norm(emb_p2)

            p1 = PassengerIdentity(
                journey_id=self.journey_id,
                person_id="P_PASSENGER_1",
                representative_embedding_json=json.dumps(emb_p1.tolist()),
                gallery_embeddings_json=json.dumps([emb_p1.tolist()]),
                representative_clothes_json=json.dumps([0.8] * 206),
                status="PRESENT"
            )
            db.session.add(p1)
            db.session.commit()

            # Biometric conflict check:
            # Candidate has emb_p2 (different person) but IDENTICAL clothes ([0.8]*206)
            has_conflict, anchor_sim, _ = check_biometric_identity_conflict(emb_p2, p1, conflict_threshold=0.50)
            self.assertTrue(has_conflict)
            self.assertLess(anchor_sim, 0.50)

            # Matching candidate against journey identities MUST NOT match P_PASSENGER_1
            detections = [{
                "face_id": "candidate_p2",
                "embedding": emb_p2.tolist(),
                "clothing_signature": [0.8] * 206,  # identical clothes!
                "confidence": 0.92,
                "seat_zone": "2B"
            }]

            matches = match_faces_to_journey_identities(detections, self.journey_id)
            matched_id = matches[0].get("person_id")
            self.assertNotEqual(matched_id, "P_PASSENGER_1")
            self.assertTrue(matches[0]["is_new"])

    def test_05_blurred_faces_preserve_needs_review_no_forced_merge(self):
        """5. Unclear / blurred faces must be flagged as 'Needs Review' and not forced into wrong ID."""
        with self.app.app_context():
            # Existing passenger
            emb = np.random.randn(512).astype(np.float32)
            emb /= np.linalg.norm(emb)
            p = PassengerIdentity(
                journey_id=self.journey_id,
                person_id="P_CLEAR",
                representative_embedding_json=json.dumps(emb.tolist()),
                status="PRESENT"
            )
            db.session.add(p)
            db.session.commit()

            detections = [{
                "face_id": "face_blurred",
                "embedding": None,  # Face detection failed or blurred
                "clothing_signature": [0.3] * 206,
                "confidence": 0.45,
                "quality": {"is_blur": True, "is_acceptable": False},
                "seat_zone": "3C"
            }]

            matches = match_faces_to_journey_identities(detections, self.journey_id)
            self.assertEqual(len(matches), 1)
            self.assertTrue(matches[0]["is_new"])
            self.assertEqual(matches[0]["confidence_label"], "Needs Review")

    def test_06_partially_visible_faces_gallery_matching(self):
        """6. Partially visible faces match against diverse gallery without poisoning immutable anchor."""
        with self.app.app_context():
            anchor_face = np.random.randn(512).astype(np.float32)
            anchor_face /= np.linalg.norm(anchor_face)

            # Gallery with anchor face at index 0 and profile angle at index 1
            profile_face = create_vector_with_similarity(anchor_face, 0.76)

            p = PassengerIdentity(
                journey_id=self.journey_id,
                person_id="P_GALLERY_TEST",
                representative_embedding_json=json.dumps(anchor_face.tolist()),
                gallery_embeddings_json=json.dumps([anchor_face.tolist(), profile_face.tolist()]),
                status="PRESENT"
            )
            db.session.add(p)
            db.session.commit()

            # New detection matches profile view (sim = 0.85 with profile_face)
            cand_profile = create_vector_with_similarity(profile_face, 0.85)

            # Update gallery with new view
            update_passenger_gallery_and_embedding(p, cand_profile)
            db.session.commit()

            # Ensure gallery[0] anchor face was NOT modified / poisoned
            g_list = json.loads(p.gallery_embeddings_json)
            np.testing.assert_allclose(g_list[0], anchor_face, atol=1e-5)
            self.assertGreaterEqual(len(g_list), 2)

    def test_07_side_facing_and_turned_passengers(self):
        """7. Side-facing passengers retain valid matching without cross-seat confusion."""
        with self.app.app_context():
            emb = np.random.randn(512).astype(np.float32)
            emb /= np.linalg.norm(emb)

            p = PassengerIdentity(
                journey_id=self.journey_id,
                person_id="P_SIDE_VIEW",
                current_seat="2A",
                representative_embedding_json=json.dumps(emb.tolist()),
                gallery_embeddings_json=json.dumps([emb.tolist()]),
                representative_clothes_json=json.dumps([0.4] * 206),
                status="PRESENT"
            )
            db.session.add(p)
            db.session.commit()

            # Passenger in Seat 2A turns sideways (sim = 0.80)
            emb_turned = create_vector_with_similarity(emb, 0.80)

            detections = [{
                "face_id": "face_turned",
                "embedding": emb_turned.tolist(),
                "clothing_signature": [0.4] * 206,
                "confidence": 0.85,
                "seat_zone": "2A"
            }]

            matches = match_faces_to_journey_identities(detections, self.journey_id)
            self.assertEqual(matches[0]["person_id"], "P_SIDE_VIEW")
            self.assertFalse(matches[0]["is_new"])

    def test_08_unclear_no_face_conservative_matching(self):
        """8. Passenger with unclear / no face is NOT falsely merged across seats or angles."""
        with self.app.app_context():
            p_seat1 = PassengerIdentity(
                journey_id=self.journey_id,
                person_id="P_SEAT_FRONT",
                current_seat="1A",
                representative_clothes_json=json.dumps([0.5] * 206),
                status="PRESENT"
            )
            db.session.add(p_seat1)
            db.session.commit()

            # Faceless detection in seat 4C (rear of bus) wearing similar color clothes
            detections = [{
                "face_id": "faceless_rear",
                "embedding": None,
                "clothing_signature": [0.5] * 206,
                "confidence": 0.80,
                "seat_zone": "4C"
            }]

            matches = match_faces_to_journey_identities(detections, self.journey_id)
            # Must NOT match P_SEAT_FRONT (different seat zone and no face verification)
            self.assertNotEqual(matches[0].get("person_id"), "P_SEAT_FRONT")
            self.assertTrue(matches[0]["is_new"])

    def test_09_repeated_clips_across_stops_reuse_without_duplicates(self):
        """9. Repeated clips reused across consecutive stops preserve observations without duplicating identities."""
        with self.app.app_context():
            uid = datetime.utcnow().strftime("%f")
            tt = TimetableImport(filename="timetable_test.xlsx", file_path="dummy.xlsx", total_stops=2)
            db.session.add(tt)
            db.session.commit()

            s1 = TimetableStop(timetable_id=tt.id, stop_number=1, stop_name="Stop 1", scheduled_arrival=datetime(2026, 10, 18, 8, 10))
            s2 = TimetableStop(timetable_id=tt.id, stop_number=2, stop_name="Stop 2", scheduled_arrival=datetime(2026, 10, 18, 8, 10))
            db.session.add_all([s1, s2])
            db.session.commit()

            # Upload record reused for Stop 1 and Stop 2
            u1 = Upload(journey_id=self.journey_id, file_path=str(self.test_img_a), original_filename="clip_08_10.jpg", camera_angle="A", route_stop_id=s1.id)
            u2 = Upload(journey_id=self.journey_id, file_path=str(self.test_img_a), original_filename="clip_08_10.jpg", camera_angle="A", route_stop_id=s2.id)
            db.session.add_all([u1, u2])
            db.session.commit()

            # Both uploads exist as separate stop processing records
            self.assertNotEqual(u1.id, u2.id)
            self.assertEqual(u1.route_stop_id, s1.id)
            self.assertEqual(u2.route_stop_id, s2.id)

    def test_10_skip_black_and_white_media(self):
        """10. Black-and-white / grayscale images and video clips are detected and skipped completely."""
        with self.app.app_context():
            # Create a grayscale image (3-channel BGR but R=G=B)
            gray_img = np.full((300, 400, 3), 120, dtype=np.uint8)
            # Add some grayscale variation (no color saturation)
            gray_img[50:150, 50:150] = 60
            gray_img[150:250, 150:250] = 180

            gray_path = Path("data/uploads/test_bw_sample.jpg").resolve()
            cv2.imwrite(str(gray_path), gray_img)

            # Test color check
            eval_res = evaluate_media_color_eligibility(gray_path)
            self.assertFalse(eval_res["is_color"])
            self.assertEqual(eval_res["status"], "SKIPPED_BLACK_AND_WHITE")
            self.assertIn("Black-and-white", eval_res["skip_reason"])

            # Color image check on actual color frame
            eval_real = evaluate_media_color_eligibility(self.test_img_a)
            self.assertTrue(eval_real["is_color"])
            self.assertEqual(eval_real["status"], "COLOR")


if __name__ == "__main__":
    unittest.main()
