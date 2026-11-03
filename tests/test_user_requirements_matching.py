import json
import unittest
import numpy as np
from datetime import datetime
from app import create_app
from app.database import db
from app.models import Bus, Journey, PassengerIdentity
from app.ai.matcher import (
    cosine_similarity,
    check_biometric_identity_conflict,
    match_faces_to_journey_identities,
    match_angle_detections_to_passengers,
    update_passenger_gallery_and_embedding
)
from app.services.journey_service import audit_and_reconcile_journey_identities


def create_vector_with_similarity(base_vec, target_sim):
    """Creates a normalized 512-d embedding with exact cosine similarity to base_vec."""
    base = np.array(base_vec, dtype=np.float32)
    base /= np.linalg.norm(base)
    rand_noise = np.random.randn(len(base)).astype(np.float32)
    orthogonal = rand_noise - np.dot(rand_noise, base) * base
    orthogonal /= np.linalg.norm(orthogonal)
    res = target_sim * base + np.sqrt(max(0.0, 1.0 - target_sim**2)) * orthogonal
    return res / np.linalg.norm(res)


class TestPassengerUniquenessAndMatching10Scenarios(unittest.TestCase):
    """
    Validates all 10 specific real-world identity matching situations required:
    1. Same passenger detected at multiple stops
    2. Same passenger detected in Angle A and Angle B
    3. Same passenger changing seats
    4. Two different passengers wearing similar clothes
    5. Two passengers with similar appearances
    6. Same passenger under different lighting
    7. Blurred or partially visible faces
    8. Repeated images and video clips
    9. Same passenger appearing in subsequent analysis
    10. New passenger appearing for the first time
    """

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()

        uid = datetime.utcnow().strftime("%f")
        self.bus = Bus(bus_number=f"TEST-BUS-{uid}", capacity=40)
        db.session.add(self.bus)
        db.session.commit()

        self.journey = Journey(
            journey_code=f"TEST-JRN-{uid}",
            bus_id=self.bus.id,
            origin="Station A",
            destination="Station B",
            status="active"
        )
        db.session.add(self.journey)
        db.session.commit()
        self.journey_id = self.journey.id

    def tearDown(self):
        db.session.rollback()
        self.ctx.pop()

    def test_01_same_passenger_multiple_stops(self):
        """1. Same passenger detected at Stop 1 and Stop 2 retains same Person ID."""
        emb = np.random.randn(512).astype(np.float32)
        emb /= np.linalg.norm(emb)

        p = PassengerIdentity(
            journey_id=self.journey_id,
            person_id="P001",
            current_seat="2A",
            representative_embedding_json=json.dumps(emb.tolist()),
            gallery_embeddings_json=json.dumps([emb.tolist()]),
            status="PRESENT",
            stops_traveled=1
        )
        db.session.add(p)
        db.session.commit()

        # Stop 2 observation of Passenger A (sim = 0.72)
        emb_stop2 = create_vector_with_similarity(emb, 0.72)
        detections = [{
            "face_id": "det_stop2",
            "embedding": emb_stop2.tolist(),
            "clothing_signature": [0.4] * 206,
            "confidence": 0.92,
            "seat_zone": "2A"
        }]

        matches = match_faces_to_journey_identities(detections, self.journey_id)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["person_id"], "P001")
        self.assertFalse(matches[0]["is_new"])

    def test_02_same_passenger_detected_in_angle_a_and_b(self):
        """2. Same passenger detected in Angle A and Angle B maintains one canonical Person ID."""
        emb = np.random.randn(512).astype(np.float32)
        emb /= np.linalg.norm(emb)

        # Angle A detection
        p = PassengerIdentity(
            journey_id=self.journey_id,
            person_id="P001",
            first_observed_angle="A",
            representative_embedding_json=json.dumps(emb.tolist()),
            gallery_embeddings_json=json.dumps([emb.tolist()]),
            status="PRESENT"
        )
        db.session.add(p)
        db.session.commit()

        # Angle B detection (sim = 0.68)
        emb_angle_b = create_vector_with_similarity(emb, 0.68)
        detections = [{
            "bbox": [100, 100, 200, 300],
            "embedding": emb_angle_b.tolist(),
            "clothing_signature": [0.5] * 206,
            "detection_confidence": 0.90,
            "camera_angle": "B"
        }]

        matched = match_angle_detections_to_passengers(detections, "B", self.journey_id)
        self.assertEqual(matched[0]["person_id"], "P001")

    def test_03_same_passenger_changing_seats(self):
        """3. Passenger changing seats does NOT spawn a new ID when face is verified."""
        emb = np.random.randn(512).astype(np.float32)
        emb /= np.linalg.norm(emb)

        p = PassengerIdentity(
            journey_id=self.journey_id,
            person_id="P003",
            current_seat="1A",
            representative_embedding_json=json.dumps(emb.tolist()),
            gallery_embeddings_json=json.dumps([emb.tolist()]),
            status="PRESENT"
        )
        db.session.add(p)
        db.session.commit()

        # Detected at rear seat 7C
        emb_moved = create_vector_with_similarity(emb, 0.78)
        detections = [{
            "face_id": "face_moved",
            "embedding": emb_moved.tolist(),
            "clothing_signature": [0.4] * 206,
            "confidence": 0.88,
            "seat_zone": "7C"
        }]

        matches = match_faces_to_journey_identities(detections, self.journey_id)
        self.assertEqual(matches[0]["person_id"], "P003")
        self.assertFalse(matches[0]["is_new"])

    def test_04_different_passengers_wearing_similar_clothes(self):
        """4. Two different passengers wearing identical clothes must NEVER be merged (biometric conflict)."""
        emb1 = np.random.randn(512).astype(np.float32)
        emb1 /= np.linalg.norm(emb1)

        # emb2 is completely different / orthogonal face (sim ~ 0.0)
        emb2 = np.random.randn(512).astype(np.float32)
        emb2 -= np.dot(emb2, emb1) * emb1
        emb2 /= np.linalg.norm(emb2)

        p1 = PassengerIdentity(
            journey_id=self.journey_id,
            person_id="P001",
            representative_embedding_json=json.dumps(emb1.tolist()),
            gallery_embeddings_json=json.dumps([emb1.tolist()]),
            representative_clothes_json=json.dumps([0.75] * 206),
            status="PRESENT"
        )
        db.session.add(p1)
        db.session.commit()

        # Biometric conflict check:
        has_conflict, max_sim, _ = check_biometric_identity_conflict(emb2, p1)
        self.assertTrue(has_conflict)
        self.assertLess(max_sim, 0.46)

        # Second person detection with IDENTICAL clothes
        detections = [{
            "face_id": "det_diff_person",
            "embedding": emb2.tolist(),
            "clothing_signature": [0.75] * 206,  # identical clothes
            "confidence": 0.95,
            "seat_zone": "2B"
        }]

        matches = match_faces_to_journey_identities(detections, self.journey_id)
        self.assertNotEqual(matches[0]["person_id"], "P001")
        self.assertTrue(matches[0]["is_new"])

    def test_05_two_passengers_with_similar_appearances(self):
        """5. Two passengers with borderline similar look (sim = 0.42) are strictly separated."""
        emb1 = np.random.randn(512).astype(np.float32)
        emb1 /= np.linalg.norm(emb1)

        emb2 = create_vector_with_similarity(emb1, 0.42)

        p1 = PassengerIdentity(
            journey_id=self.journey_id,
            person_id="P_LOOKALIKE_1",
            representative_embedding_json=json.dumps(emb1.tolist()),
            gallery_embeddings_json=json.dumps([emb1.tolist()]),
            status="PRESENT"
        )
        db.session.add(p1)
        db.session.commit()

        has_conflict, max_sim, _ = check_biometric_identity_conflict(emb2, p1)
        self.assertTrue(has_conflict)

    def test_06_same_passenger_different_lighting(self):
        """6. Same passenger under different lighting (sim = 0.62) retains ID."""
        emb = np.random.randn(512).astype(np.float32)
        emb /= np.linalg.norm(emb)

        p = PassengerIdentity(
            journey_id=self.journey_id,
            person_id="P006",
            representative_embedding_json=json.dumps(emb.tolist()),
            gallery_embeddings_json=json.dumps([emb.tolist()]),
            representative_clothes_json=json.dumps([0.5] * 206),
            status="PRESENT"
        )
        db.session.add(p)
        db.session.commit()

        emb_dim_light = create_vector_with_similarity(emb, 0.62)
        detections = [{
            "face_id": "det_dim_light",
            "embedding": emb_dim_light.tolist(),
            "clothing_signature": [0.5] * 206,
            "confidence": 0.85,
            "seat_zone": "1B"
        }]

        matches = match_faces_to_journey_identities(detections, self.journey_id)
        self.assertEqual(matches[0]["person_id"], "P006")
        self.assertFalse(matches[0]["is_new"])

    def test_07_blurred_or_partially_visible_faces(self):
        """7. Blurred or low-quality faces are marked NEEDS REVIEW without forcing wrong ID."""
        detections = [{
            "face_id": "det_blur",
            "embedding": None,
            "clothing_signature": [0.3] * 206,
            "confidence": 0.45,
            "quality": {"is_blur": True, "is_acceptable": False},
            "seat_zone": "3C"
        }]

        matches = match_faces_to_journey_identities(detections, self.journey_id)
        self.assertEqual(matches[0]["confidence_label"], "Needs Review")

    def test_08_repeated_images_and_video_clips(self):
        """8. Repeated frames/clips of same passengers reuse existing IDs without duplicating."""
        emb = np.random.randn(512).astype(np.float32)
        emb /= np.linalg.norm(emb)

        p = PassengerIdentity(
            journey_id=self.journey_id,
            person_id="P008",
            representative_embedding_json=json.dumps(emb.tolist()),
            gallery_embeddings_json=json.dumps([emb.tolist()]),
            status="PRESENT"
        )
        db.session.add(p)
        db.session.commit()

        # Repeated detection of exact same face
        detections = [{
            "face_id": "det_repeat",
            "embedding": emb.tolist(),
            "clothing_signature": [0.4] * 206,
            "confidence": 0.98,
            "seat_zone": "1A"
        }]

        matches = match_faces_to_journey_identities(detections, self.journey_id)
        self.assertEqual(matches[0]["person_id"], "P008")
        self.assertFalse(matches[0]["is_new"])

    def test_09_same_passenger_appearing_in_subsequent_analysis(self):
        """9. Gallery accommodates subsequent angle/stop poses without anchor poisoning."""
        anchor = np.random.randn(512).astype(np.float32)
        anchor /= np.linalg.norm(anchor)

        p = PassengerIdentity(
            journey_id=self.journey_id,
            person_id="P009",
            representative_embedding_json=json.dumps(anchor.tolist()),
            gallery_embeddings_json=json.dumps([anchor.tolist()]),
            status="PRESENT"
        )
        db.session.add(p)
        db.session.commit()

        # Pose 2 from Angle B
        pose2 = create_vector_with_similarity(anchor, 0.70)
        update_passenger_gallery_and_embedding(p, pose2)
        db.session.commit()

        gallery = json.loads(p.gallery_embeddings_json)
        self.assertEqual(len(gallery), 2)
        # Anchor at index 0 unchanged
        np.testing.assert_allclose(gallery[0], anchor, atol=1e-5)

    def test_10_new_passenger_appearing_for_the_first_time(self):
        """10. Truly new passenger receives next Person ID."""
        emb_existing = np.random.randn(512).astype(np.float32)
        emb_existing /= np.linalg.norm(emb_existing)

        p = PassengerIdentity(
            journey_id=self.journey_id,
            person_id="P001",
            representative_embedding_json=json.dumps(emb_existing.tolist()),
            gallery_embeddings_json=json.dumps([emb_existing.tolist()]),
            status="PRESENT"
        )
        db.session.add(p)
        db.session.commit()

        # Truly new passenger (sim ~ 0.10)
        emb_new = np.random.randn(512).astype(np.float32)
        emb_new -= np.dot(emb_new, emb_existing) * emb_existing
        emb_new /= np.linalg.norm(emb_new)

        detections = [{
            "face_id": "det_brand_new",
            "embedding": emb_new.tolist(),
            "clothing_signature": [0.2] * 206,
            "confidence": 0.94,
            "seat_zone": "3D"
        }]

        matches = match_faces_to_journey_identities(detections, self.journey_id)
        self.assertTrue(matches[0]["is_new"])
        self.assertEqual(matches[0]["person_id"], "P002")


if __name__ == "__main__":
    unittest.main()
