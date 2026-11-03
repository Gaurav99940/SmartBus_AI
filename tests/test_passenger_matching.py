import unittest
import json
import numpy as np
from datetime import datetime
from app import create_app
from app.config import Config
from app.database import db
from app.models import Journey, Bus, Upload, PassengerIdentity, PassengerObservation, PresenceEvent, AuditLog
from app.ai.matcher import (
    cosine_similarity,
    match_faces_to_journey_identities,
    match_angle_detections_to_passengers,
    update_passenger_gallery_and_embedding,
    generate_next_person_id,
    get_passenger_reference_embeddings,
    compute_best_face_similarity
)
from app.services.journey_service import (
    process_upload_for_journey,
    process_multi_angle_batch,
    merge_duplicate_passengers
)


class TestPersistentPassengerMatching(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

        # Seed test bus and journeys
        self.bus = Bus.query.filter((Bus.bus_number == "BUS-PTEST-01") | (Bus.registration_number == "UP-14-BT-9999")).first()
        if not self.bus:
            self.bus = Bus(
                bus_number="BUS-PTEST-01",
                registration_number="UP-14-BT-9999",
                license_plate="UP14BT9999",
                capacity=40
            )
            db.session.add(self.bus)
            db.session.commit()

        self.journey1 = Journey.query.filter_by(journey_code="JNY-PTEST-01").first()
        if not self.journey1:
            self.journey1 = Journey(
                journey_code="JNY-PTEST-01",
                bus_id=self.bus.id,
                origin="Anand Vihar",
                destination="Noida Sector 62",
                status="in_progress",
                start_time=datetime.utcnow()
            )
            db.session.add(self.journey1)
            db.session.commit()

        self.journey2 = Journey.query.filter_by(journey_code="JNY-PTEST-02").first()
        if not self.journey2:
            self.journey2 = Journey(
                journey_code="JNY-PTEST-02",
                bus_id=self.bus.id,
                origin="Kashmere Gate",
                destination="Gurugram",
                status="in_progress",
                start_time=datetime.utcnow()
            )
            db.session.add(self.journey2)
            db.session.commit()

        # Clean prior test observations & passengers for isolation between test runs
        PassengerObservation.query.filter(PassengerObservation.journey_id.in_([self.journey1.id, self.journey2.id])).delete()
        PresenceEvent.query.filter(PresenceEvent.journey_id.in_([self.journey1.id, self.journey2.id])).delete()
        PassengerIdentity.query.filter(PassengerIdentity.journey_id.in_([self.journey1.id, self.journey2.id])).delete()
        db.session.commit()

        # Generate deterministic synthetic 512-D face embeddings
        np.random.seed(42)
        v1 = np.random.randn(512).astype(np.float32)
        self.emb_person1 = (v1 / np.linalg.norm(v1)).tolist()

        # Person 1 with head angle / lighting shift (cosine similarity ~0.68)
        v1_shifted = v1 + np.random.normal(0, 0.08, 512).astype(np.float32)
        self.emb_person1_shifted = (v1_shifted / np.linalg.norm(v1_shifted)).tolist()

        # Person 2 (completely different person, orthogonal vector)
        v2 = np.random.randn(512).astype(np.float32)
        v2 = v2 - np.dot(v2, v1) * (v1 / np.dot(v1, v1))
        self.emb_person2 = (v2 / np.linalg.norm(v2)).tolist()

        # Person 3 (another distinct individual)
        v3 = np.random.randn(512).astype(np.float32)
        self.emb_person3 = (v3 / np.linalg.norm(v3)).tolist()

    def tearDown(self):
        try:
            PassengerObservation.query.filter(PassengerObservation.journey_id.in_([self.journey1.id, self.journey2.id])).delete()
            PresenceEvent.query.filter(PresenceEvent.journey_id.in_([self.journey1.id, self.journey2.id])).delete()
            PassengerIdentity.query.filter(PassengerIdentity.journey_id.in_([self.journey1.id, self.journey2.id])).delete()
            db.session.commit()
        except Exception:
            db.session.rollback()
        finally:
            self.ctx.pop()

    def test_1_same_person_two_photos_receives_same_id(self):
        """1. The same person's face in two different photos receives the same passenger ID."""
        # Photo 1: Person 1 boards
        face_photo1 = {
            "bbox": [100, 150, 80, 120],
            "detector_prob": 0.95,
            "embedding": self.emb_person1,
            "clothing_signature": [0.5] * 206,
            "crop_path": "crops/test1.jpg",
            "quality": {"is_acceptable": True, "quality_score": 0.95, "is_blur": False}
        }
        res1 = match_faces_to_journey_identities([face_photo1], journey_id=self.journey1.id)
        self.assertEqual(len(res1), 1)
        self.assertTrue(res1[0]["is_new"])
        self.assertEqual(res1[0]["person_id"], "P001")

        # Save to DB as passenger P001
        p1 = PassengerIdentity(
            journey_id=self.journey1.id,
            person_id="P001",
            current_seat="1A",
            representative_embedding_json=json.dumps(self.emb_person1),
            gallery_embeddings_json=json.dumps([self.emb_person1]),
            status="PRESENT"
        )
        db.session.add(p1)
        db.session.commit()

        # Photo 2: Same Person 1 detected in next stop/photo
        face_photo2 = {
            "bbox": [120, 160, 80, 120],
            "detector_prob": 0.92,
            "embedding": self.emb_person1,
            "clothing_signature": [0.5] * 206,
            "crop_path": "crops/test2.jpg",
            "quality": {"is_acceptable": True, "quality_score": 0.92, "is_blur": False}
        }
        res2 = match_faces_to_journey_identities([face_photo2], journey_id=self.journey1.id)
        self.assertEqual(len(res2), 1)
        self.assertFalse(res2[0]["is_new"])
        self.assertEqual(res2[0]["person_id"], "P001")
        self.assertGreaterEqual(res2[0]["similarity_score"], 0.70)

    def test_2_head_angle_and_lighting_shift_matched_with_gallery(self):
        """2. The same person with head angle/lighting change is matched when recognition confidence supports it."""
        # Create passenger P001 with base embedding
        p1 = PassengerIdentity(
            journey_id=self.journey1.id,
            person_id="P001",
            current_seat="1A",
            representative_embedding_json=json.dumps(self.emb_person1),
            gallery_embeddings_json=json.dumps([self.emb_person1]),
            status="PRESENT"
        )
        db.session.add(p1)
        db.session.commit()

        # Verify similarity between shifted face and base face
        sim = cosine_similarity(self.emb_person1, self.emb_person1_shifted)
        self.assertGreaterEqual(sim, 0.58)

        # Photo with head tilt / angle change
        det_shifted = {
            "temp_id": "T-A-01",
            "bbox": [200, 200, 90, 130],
            "norm_bbox": [0.15, 0.25, 0.07, 0.18],
            "detection_confidence": 0.88,
            "embedding": self.emb_person1_shifted,
            "clothing_signature": [0.5] * 206,
            "quality": {"is_acceptable": True, "quality_score": 0.85, "is_blur": False}
        }
        matched = match_angle_detections_to_passengers([det_shifted], camera_angle="A", journey_id=self.journey1.id)
        self.assertEqual(matched[0]["person_id"], "P001")
        self.assertEqual(matched[0]["passenger_identity"].id, p1.id)

        # Enriched gallery test
        update_passenger_gallery_and_embedding(p1, self.emb_person1_shifted)
        refs = get_passenger_reference_embeddings(p1)
        self.assertGreaterEqual(len(refs), 2)

    def test_3_person_moving_seats_keeps_same_id(self):
        """3. The same person moving to a completely different seat retains the same ID."""
        # Passenger P001 sits in Row 1 (norm_bbox at top front of bus: y=0.10)
        p1 = PassengerIdentity(
            journey_id=self.journey1.id,
            person_id="P001",
            current_seat="1A",
            representative_embedding_json=json.dumps(self.emb_person1),
            gallery_embeddings_json=json.dumps([self.emb_person1]),
            status="PRESENT"
        )
        db.session.add(p1)
        db.session.commit()

        # Add past observation in front row
        obs1 = PassengerObservation(
            upload_id=1,
            journey_id=self.journey1.id,
            passenger_identity_id=p1.id,
            camera_angle="A",
            bbox_x=100, bbox_y=70, bbox_w=80, bbox_h=120,
            observation_time=datetime.utcnow()
        )
        db.session.add(obs1)
        db.session.commit()

        # At Stop 2, Person 1 moves to Row 5 (far back: y=0.75, x=0.80)
        # Spatial distance is huge (spat_sim = 0)
        det_moved_seat = {
            "temp_id": "T-A-02",
            "bbox": [900, 500, 90, 130],
            "norm_bbox": [0.80, 0.75, 0.08, 0.18],
            "detection_confidence": 0.90,
            "embedding": self.emb_person1, # Same face
            "clothing_signature": [0.5] * 206,
            "quality": {"is_acceptable": True, "quality_score": 0.90, "is_blur": False}
        }
        matched = match_angle_detections_to_passengers([det_moved_seat], camera_angle="A", journey_id=self.journey1.id)
        # MUST still identify as P001 despite changing seat!
        self.assertEqual(matched[0]["person_id"], "P001")
        self.assertEqual(matched[0]["passenger_identity"].id, p1.id)

    def test_4_two_different_people_receive_different_ids(self):
        """4. Two different people receive different IDs (no false collapsing)."""
        p1 = PassengerIdentity(
            journey_id=self.journey1.id,
            person_id="P001",
            current_seat="1A",
            representative_embedding_json=json.dumps(self.emb_person1),
            gallery_embeddings_json=json.dumps([self.emb_person1]),
            status="PRESENT"
        )
        db.session.add(p1)
        db.session.commit()

        # Candidate is Person 2
        det_person2 = {
            "temp_id": "T-A-03",
            "bbox": [300, 200, 80, 120],
            "norm_bbox": [0.25, 0.25, 0.06, 0.16],
            "detection_confidence": 0.88,
            "embedding": self.emb_person2,
            "clothing_signature": [0.1] * 206, # different clothes
            "quality": {"is_acceptable": True, "quality_score": 0.88, "is_blur": False}
        }
        matched = match_angle_detections_to_passengers([det_person2], camera_angle="A", journey_id=self.journey1.id)
        # Person 2 must NOT be assigned P001! Must receive P002
        self.assertNotEqual(matched[0]["person_id"], "P001")
        self.assertEqual(matched[0]["person_id"], "P002")
        self.assertIsNone(matched[0]["passenger_identity"])

    def test_5_ambiguous_or_low_quality_face_marked_for_review(self):
        """5. Ambiguous or low-quality face is marked for review rather than forced into a match."""
        # Unacceptable quality (severe blur)
        det_blurry = {
            "temp_id": "T-A-04",
            "bbox": [400, 250, 70, 100],
            "norm_bbox": [0.30, 0.30, 0.05, 0.14],
            "detection_confidence": 0.40,
            "embedding": None, # No clear face
            "clothing_signature": None,
            "quality": {"is_acceptable": False, "quality_score": 0.20, "is_blur": True}
        }
        matched = match_angle_detections_to_passengers([det_blurry], camera_angle="A", journey_id=self.journey1.id)
        self.assertEqual(matched[0]["confidence_label"], "Needs Review")
        self.assertEqual(matched[0]["review_status"], "NEEDS_REVIEW")

    def test_6_reuploading_same_image_is_idempotent(self):
        """6. Re-uploading the same image does not create duplicate passenger identities."""
        import cv2
        color_test_path = Config.UPLOAD_FOLDER / "test_color_idempotent.jpg"
        Config.UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
        img = np.full((100, 100, 3), 120, dtype=np.uint8)
        img[:, :, 0] = 220 # Strong color (blue)
        img[:, :, 2] = 50
        cv2.imwrite(str(color_test_path), img)

        upload = Upload(
            journey_id=self.journey1.id,
            bus_id=self.bus.id,
            camera_angle="A",
            file_path=str(color_test_path),
            original_filename="test_color_idempotent.jpg",
            status="uploaded"
        )
        db.session.add(upload)
        db.session.commit()

        faces = [{
            "bbox": [100, 100, 60, 80],
            "detector_prob": 0.95,
            "embedding": self.emb_person1,
            "crop_path": "crops/u1.jpg",
            "quality": {"is_acceptable": True, "quality_score": 0.95, "is_blur": False}
        }]

        # First run
        process_upload_for_journey(upload.id, faces)
        count_first = PassengerIdentity.query.filter_by(journey_id=self.journey1.id).count()
        obs_first = PassengerObservation.query.filter_by(upload_id=upload.id).count()
        self.assertEqual(count_first, 1)
        self.assertEqual(obs_first, 1)

        # Second run (re-processing the exact same upload)
        process_upload_for_journey(upload.id, faces)
        count_second = PassengerIdentity.query.filter_by(journey_id=self.journey1.id).count()
        obs_second = PassengerObservation.query.filter_by(upload_id=upload.id).count()
        # MUST remain 1 identity and 1 observation
        self.assertEqual(count_second, 1)
        self.assertEqual(obs_second, 1)

    def test_7_existing_passengers_retrieved_correctly_across_stops(self):
        """7. Existing passengers are retrieved correctly from database across multiple stops."""
        p1 = PassengerIdentity(
            journey_id=self.journey1.id,
            person_id="P001",
            current_seat="1A",
            representative_embedding_json=json.dumps(self.emb_person1),
            gallery_embeddings_json=json.dumps([self.emb_person1]),
            status="PRESENT",
            total_observations=1,
            stops_traveled=1
        )
        db.session.add(p1)
        db.session.commit()

        # Query retrieved profile
        refs = get_passenger_reference_embeddings(p1)
        self.assertEqual(len(refs), 1)

        # Stop 2 detection of P001
        det = {
            "temp_id": "T-A-05",
            "bbox": [110, 150, 80, 120],
            "norm_bbox": [0.08, 0.12, 0.06, 0.16],
            "detection_confidence": 0.92,
            "embedding": self.emb_person1,
            "clothing_signature": [0.5] * 206,
            "quality": {"is_acceptable": True, "quality_score": 0.92, "is_blur": False}
        }
        res = match_angle_detections_to_passengers([det], camera_angle="A", journey_id=self.journey1.id)
        self.assertEqual(res[0]["person_id"], "P001")
        self.assertIsNotNone(res[0]["passenger_identity"])
        self.assertEqual(res[0]["passenger_identity"].person_id, "P001")

    def test_8_two_simultaneous_allocations_handled_safely(self):
        """8. Two simultaneous uploads allocate unique IDs without collision."""
        allocated = set()
        pid1 = generate_next_person_id(self.journey1.id, allocated_ids=allocated)
        pid2 = generate_next_person_id(self.journey1.id, allocated_ids=allocated)
        pid3 = generate_next_person_id(self.journey1.id, allocated_ids=allocated)
        self.assertEqual(pid1, "P001")
        self.assertEqual(pid2, "P002")
        self.assertEqual(pid3, "P003")
        self.assertEqual(len(allocated), 3)

    def test_9_different_journeys_remain_isolated(self):
        """9. Different journeys remain strictly isolated (no cross-journey ID leakage)."""
        # Create P001 in Journey 1
        p_j1 = PassengerIdentity(
            journey_id=self.journey1.id,
            person_id="P001",
            current_seat="1A",
            representative_embedding_json=json.dumps(self.emb_person1),
            gallery_embeddings_json=json.dumps([self.emb_person1]),
            status="PRESENT"
        )
        db.session.add(p_j1)
        db.session.commit()

        # In Journey 2, first passenger should start at P001 (isolated journey scope)
        pid_j2 = generate_next_person_id(self.journey2.id)
        self.assertEqual(pid_j2, "P001")

        # Even with same face in Journey 2, matching against Journey 2 identities should NOT find Journey 1's P001
        det = {
            "bbox": [100, 100, 80, 120],
            "detector_prob": 0.95,
            "embedding": self.emb_person1,
            "clothing_signature": [0.5] * 206,
            "crop_path": "crops/j2.jpg",
            "quality": {"is_acceptable": True, "quality_score": 0.95, "is_blur": False}
        }
        res_j2 = match_faces_to_journey_identities([det], journey_id=self.journey2.id)
        # In Journey 2, this is a new boarding passenger for Journey 2
        self.assertTrue(res_j2[0]["is_new"])
        self.assertIsNone(res_j2[0]["passenger_identity"])

    def test_10_merge_duplicate_passengers_workflow(self):
        """10. Verify operator merge workflow safely consolidates duplicate passenger identities."""
        p1 = PassengerIdentity(
            journey_id=self.journey1.id,
            person_id="P001",
            current_seat="1A",
            representative_embedding_json=json.dumps(self.emb_person1),
            gallery_embeddings_json=json.dumps([self.emb_person1]),
            status="PRESENT",
            total_observations=2,
            stops_traveled=1
        )
        p7 = PassengerIdentity(
            journey_id=self.journey1.id,
            person_id="P007",
            current_seat="3B",
            representative_embedding_json=json.dumps(self.emb_person1_shifted),
            gallery_embeddings_json=json.dumps([self.emb_person1_shifted]),
            status="PRESENT",
            total_observations=3,
            stops_traveled=3,
            exit_stop_name="City Center",
            exit_stop_number=3
        )
        db.session.add_all([p1, p7])
        db.session.commit()

        obs_p7 = PassengerObservation(
            upload_id=1,
            journey_id=self.journey1.id,
            passenger_identity_id=p7.id,
            camera_angle="B",
            bbox_x=200, bbox_y=200, bbox_w=80, bbox_h=120,
            observation_time=datetime.utcnow()
        )
        db.session.add(obs_p7)
        db.session.commit()

        res = merge_duplicate_passengers(
            journey_id=self.journey1.id,
            source_person_id="P007",
            target_person_id="P001",
            reviewer_id=1,
            comment="Operator verified P007 is P001 after seat move"
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["observations_transferred"], 1)

        # Verify P007 is archived
        p7_updated = PassengerIdentity.query.filter_by(journey_id=self.journey1.id, person_id="P007").first()
        self.assertEqual(p7_updated.status, "MERGED_ARCHIVED")

        # Verify observation reassigned to P001
        self.assertEqual(obs_p7.passenger_identity_id, p1.id)

        # Verify P001 has merged gallery and updated stops traveled
        p1_updated = PassengerIdentity.query.filter_by(journey_id=self.journey1.id, person_id="P001").first()
        self.assertEqual(p1_updated.stops_traveled, 3)
        refs = get_passenger_reference_embeddings(p1_updated)
        self.assertGreaterEqual(len(refs), 2)


if __name__ == "__main__":
    unittest.main()
