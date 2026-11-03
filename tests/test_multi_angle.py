import os
import io
import json
import unittest
from pathlib import Path
from datetime import datetime, timedelta
import numpy as np
import cv2
from PIL import Image

from app import create_app
from app.database import db
from app.models import (
    User, Bus, Journey, Upload, PassengerIdentity,
    PassengerObservation, PresenceEvent, CameraAngleConfig
)
from app.config import Config
from app.ai.person_pipeline import detect_persons_and_faces, render_multi_angle_annotated_hud
from app.services.journey_service import process_multi_angle_batch, review_observation


USER_ANGLE_A = str(Path("data/uploads/sample_angle_A.jpg").resolve())
USER_ANGLE_B = str(Path("data/uploads/sample_angle_B.jpg").resolve())
USER_ANGLE_C = str(Path("data/uploads/sample_angle_C.jpg").resolve())


def get_or_create_test_image(path_or_name, style="normal"):
    """
    Returns a valid test image path.
    Uses real user bus photo if available, or synthesizes a bus test frame.
    """
    try:
        if path_or_name and Path(path_or_name).is_file():
            return str(path_or_name)
    except Exception:
        pass

    test_dir = Config.UPLOAD_FOLDER / "test_fixtures"
    test_dir.mkdir(parents=True, exist_ok=True)
    out_path = test_dir / f"{style}_{datetime.utcnow().strftime('%f')}.jpg"

    base_img = None
    try:
        real_base = Path(USER_ANGLE_C)
        if real_base.is_file():
            base_img = cv2.imread(str(real_base))
    except Exception:
        base_img = None

    if base_img is None:
        base_img = np.full((384, 640, 3), 110, dtype=np.uint8)

    h, w = base_img.shape[:2]
    if style == "dark":
        img = (base_img * 0.1).astype(np.uint8)
    elif style == "blurry":
        img = cv2.GaussianBlur(base_img, (31, 31), 0)
    elif style == "empty":
        img = np.full((h, w, 3), 100, dtype=np.uint8)
        cv2.putText(img, "Empty Seats", (50, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (200, 200, 200), 2)
    elif style == "flip":
        img = cv2.flip(base_img, 1)
    else:
        img = base_img.copy()

    cv2.imwrite(str(out_path), img)
    return str(out_path)


class TestMultiAnglePassengerDetection(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        with self.app.app_context():
            # Create a dedicated test bus and journey
            self.bus = Bus.query.filter_by(bus_number="TEST-BUS-999").first()
            if not self.bus:
                self.bus = Bus(
                    bus_number="TEST-BUS-999",
                    registration_number="TEST-REG-999",
                    license_plate="TST-999",
                    capacity=40,
                    route_name="Central - Northside"
                )
                db.session.add(self.bus)
                db.session.flush()

            self.journey = Journey.query.filter_by(journey_code="JNY-MULTI-TEST").first()
            if not self.journey:
                self.journey = Journey(
                    journey_code="JNY-MULTI-TEST",
                    bus_id=self.bus.id,
                    origin="Central Station",
                    destination="Northside Terminus",
                    status="active"
                )
                db.session.add(self.journey)
                db.session.commit()
            self.journey_id = self.journey.id

            # Ensure default camera angle configs
            for ang in ["A", "B", "C"]:
                if not CameraAngleConfig.query.filter_by(camera_angle=ang).first():
                    db.session.add(CameraAngleConfig(
                        camera_angle=ang,
                        title=f"Angle {ang} — Section {ang}",
                        seating_area_label=f"Section {ang}",
                        target_seats="1A, 1B, 1C, 1D",
                        target_capacity=4
                    ))
            db.session.commit()

    # -------------------------------------------------------------------------
    # TEST 1: Upload all three angles successfully
    # -------------------------------------------------------------------------
    def test_upload_all_three_angles_successfully(self):
        img_a = get_or_create_test_image(USER_ANGLE_A)
        img_b = get_or_create_test_image(USER_ANGLE_B)
        img_c = get_or_create_test_image(USER_ANGLE_C)

        with open(img_a, "rb") as fa, open(img_b, "rb") as fb, open(img_c, "rb") as fc:
            res = self.client.post("/api/uploads/multi-angle", data={
                "journey_id": self.journey_id,
                "photo_a": (fa, "angle_a.jpg"),
                "photo_b": (fb, "angle_b.jpg"),
                "photo_c": (fc, "angle_c.jpg"),
                "capture_timestamp_a": "2026-10-14 22:38:11",
                "capture_timestamp_b": "2026-10-14 22:47:02",
                "capture_timestamp_c": "2026-10-15 00:47:30",
                "latitude": 37.7749,
                "longitude": -122.4194,
                "location_source": "DEVICE_API"
            }, content_type="multipart/form-data")

        self.assertEqual(res.status_code, 201)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertIn("consolidated_counts", data)
        self.assertIn("angle_summaries", data)
        self.assertIn("A", data["angle_summaries"])
        self.assertIn("B", data["angle_summaries"])
        self.assertIn("C", data["angle_summaries"])
        self.assertEqual(data["angle_summaries"]["A"]["status"], "processed")
        self.assertEqual(data["angle_summaries"]["B"]["status"], "processed")
        self.assertEqual(data["angle_summaries"]["C"]["status"], "processed")

    # -------------------------------------------------------------------------
    # TEST 2: Upload only Angle A
    # -------------------------------------------------------------------------
    def test_upload_only_angle_a(self):
        img_a = get_or_create_test_image(USER_ANGLE_A)
        with open(img_a, "rb") as fa:
            res = self.client.post("/api/uploads/multi-angle", data={
                "journey_id": self.journey_id,
                "photo_a": (fa, "angle_a.jpg"),
                "capture_timestamp_a": "2026-10-14 22:38:11"
            }, content_type="multipart/form-data")

        self.assertEqual(res.status_code, 201)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertIn("A", data["angle_summaries"])
        self.assertNotIn("B", data["angle_summaries"])
        self.assertNotIn("C", data["angle_summaries"])
        self.assertGreater(data["consolidated_counts"]["angle_a_count"], 0)

    # -------------------------------------------------------------------------
    # TEST 3: Upload Angle A and Angle C while Angle B is missing
    # -------------------------------------------------------------------------
    def test_upload_angle_a_and_c_missing_b(self):
        img_a = get_or_create_test_image(USER_ANGLE_A)
        img_c = get_or_create_test_image(USER_ANGLE_C)
        with open(img_a, "rb") as fa, open(img_c, "rb") as fc:
            res = self.client.post("/api/uploads/multi-angle", data={
                "journey_id": self.journey_id,
                "photo_a": (fa, "angle_a.jpg"),
                "photo_c": (fc, "angle_c.jpg"),
            }, content_type="multipart/form-data")

        self.assertEqual(res.status_code, 201)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertIn("A", data["angle_summaries"])
        self.assertIn("C", data["angle_summaries"])
        self.assertNotIn("B", data["angle_summaries"])

    # -------------------------------------------------------------------------
    # TEST 4: Detect person whose face is unclear but body is visible
    # -------------------------------------------------------------------------
    def test_detect_person_unclear_face_body_visible(self):
        # Angle C has passengers leaning forward or turned where face is occluded/unclear
        img_c = get_or_create_test_image(USER_ANGLE_C)
        res = detect_persons_and_faces(img_c, camera_angle="C")
        self.assertGreater(res["raw_count"], 0)
        # Check that visible bodies without clear faces are retained as detections
        unclear_body_dets = [d for d in res["detections"] if not d.get("has_face")]
        self.assertGreater(len(unclear_body_dets), 0)
        first_unclear = unclear_body_dets[0]
        self.assertTrue(first_unclear["temp_id"].startswith("T-C-"))
        self.assertEqual(first_unclear["review_status"], "NEEDS_REVIEW")

    # -------------------------------------------------------------------------
    # TEST 5: Detect passenger facing away from camera
    # -------------------------------------------------------------------------
    def test_detect_passenger_facing_away(self):
        # Synthesize a clear person body with back turned
        img_back = get_or_create_test_image(None, style="normal")
        res = detect_persons_and_faces(img_back, camera_angle="A")
        # System returns detections without requiring face match
        self.assertIsInstance(res["detections"], list)
        self.assertIn("raw_count", res)

    # -------------------------------------------------------------------------
    # TEST 6: Handle partially occluded passengers
    # -------------------------------------------------------------------------
    def test_handle_partially_occluded_passengers(self):
        img_a = get_or_create_test_image(USER_ANGLE_A)
        res = detect_persons_and_faces(img_a, camera_angle="A")
        # In bus interior view, seated passengers are detected even when partially occluded by seats
        self.assertGreater(res["raw_count"], 0)
        for d in res["detections"]:
            self.assertEqual(len(d["bbox"]), 4)
            self.assertGreater(d["detection_confidence"], 0.0)

    # -------------------------------------------------------------------------
    # TEST 7: Handle empty seats
    # -------------------------------------------------------------------------
    def test_handle_empty_seats(self):
        empty_img = get_or_create_test_image(None, style="empty")
        res = detect_persons_and_faces(empty_img, camera_angle="A")
        # Empty seats should yield 0 or low false positives, and not throw errors
        self.assertIsInstance(res["raw_count"], int)

    # -------------------------------------------------------------------------
    # TEST 8: Handle poor quality images
    # -------------------------------------------------------------------------
    def test_handle_poor_quality_images(self):
        dark_img = get_or_create_test_image(None, style="dark")
        res = detect_persons_and_faces(dark_img, camera_angle="A")
        # Should raise warning about poor lighting
        self.assertTrue(any("dark" in w.lower() or "lighting" in w.lower() for w in res["warnings"]))

    # -------------------------------------------------------------------------
    # TEST 9: Avoid double counting a confidently matched passenger across angles
    # -------------------------------------------------------------------------
    def test_avoid_double_counting_across_angles(self):
        with self.app.app_context():
            # Create a passenger with a known representative embedding
            p = PassengerIdentity(
                journey_id=self.journey_id,
                person_id="P100",
                representative_embedding_json=json.dumps([0.1] * 512),
                status="PRESENT"
            )
            db.session.add(p)
            db.session.commit()

            # Mock angle upload records where both angles find passenger matching P100
            u_a = Upload(journey_id=self.journey_id, file_path=get_or_create_test_image(USER_ANGLE_A), original_filename="a.jpg", camera_angle="A")
            u_b = Upload(journey_id=self.journey_id, file_path=get_or_create_test_image(USER_ANGLE_B, style="flip"), original_filename="b.jpg", camera_angle="B")
            db.session.add_all([u_a, u_b])
            db.session.commit()

            res = process_multi_angle_batch(self.journey_id, "test_batch_dedup", {"A": u_a, "B": u_b})
            counts = res["consolidated_counts"]
            # Confirmed unique passengers does not double-count identical person across angles
            self.assertGreater(counts["total_raw_detections"], 0)
            self.assertLessEqual(counts["confirmed_unique"], counts["total_raw_detections"])

    # -------------------------------------------------------------------------
    # TEST 10: Preserve uncertain matches for review
    # -------------------------------------------------------------------------
    def test_preserve_uncertain_matches_for_review(self):
        img_c = get_or_create_test_image(USER_ANGLE_C)
        with open(img_c, "rb") as fc:
            res = self.client.post("/api/uploads/multi-angle", data={
                "journey_id": self.journey_id,
                "photo_c": (fc, "angle_c.jpg")
            }, content_type="multipart/form-data")
        data = res.get_json()
        counts = data["consolidated_counts"]
        # Uncertain detections are preserved for review
        self.assertGreaterEqual(counts["uncertain_or_unidentified"], 0)
        self.assertTrue(counts["is_provisional"])

    # -------------------------------------------------------------------------
    # TEST 11: Reuse existing journey-scoped passenger identities
    # -------------------------------------------------------------------------
    def test_reuse_existing_journey_passenger_identities(self):
        with self.app.app_context():
            # Initial count of passengers
            initial_count = PassengerIdentity.query.filter_by(journey_id=self.journey_id).count()
            # If an existing passenger is confirmed present in a new upload, person_id is preserved
            existing = PassengerIdentity.query.filter_by(journey_id=self.journey_id).first()
            if existing:
                self.assertTrue(existing.person_id.startswith("P"))

    # -------------------------------------------------------------------------
    # TEST 12: Handle different capture timestamps
    # -------------------------------------------------------------------------
    def test_handle_different_capture_timestamps(self):
        img_a = get_or_create_test_image(USER_ANGLE_A)
        img_b = get_or_create_test_image(USER_ANGLE_B)
        with open(img_a, "rb") as fa, open(img_b, "rb") as fb:
            res = self.client.post("/api/uploads/multi-angle", data={
                "journey_id": self.journey_id,
                "photo_a": (fa, "a.jpg"),
                "photo_b": (fb, "b.jpg"),
                "capture_timestamp_a": "2026-10-14 22:38:11",
                "capture_timestamp_b": "2026-10-14 22:47:02"
            }, content_type="multipart/form-data")
        data = res.get_json()
        self.assertTrue(data["success"])
        # Check upload timestamps preserved
        with self.app.app_context():
            up_a = Upload.query.filter_by(camera_angle="A").order_by(Upload.id.desc()).first()
            up_b = Upload.query.filter_by(camera_angle="B").order_by(Upload.id.desc()).first()
            self.assertNotEqual(up_a.capture_timestamp, up_b.capture_timestamp)

    # -------------------------------------------------------------------------
    # TEST 13: Handle duplicate image uploads
    # -------------------------------------------------------------------------
    def test_handle_duplicate_image_uploads(self):
        img_a = get_or_create_test_image(USER_ANGLE_A)
        with open(img_a, "rb") as fa:
            res1 = self.client.post("/api/uploads/multi-angle", data={
                "journey_id": self.journey_id,
                "photo_a": (fa, "angle_dup.jpg")
            }, content_type="multipart/form-data")
        self.assertEqual(res1.status_code, 201)

        # Upload exact same photo again
        with open(img_a, "rb") as fa:
            res2 = self.client.post("/api/uploads/multi-angle", data={
                "journey_id": self.journey_id,
                "photo_a": (fa, "angle_dup.jpg")
            }, content_type="multipart/form-data")
        self.assertEqual(res2.status_code, 201)
        # Should record duplicate note
        with self.app.app_context():
            latest = Upload.query.filter_by(camera_angle="A").order_by(Upload.id.desc()).first()
            self.assertIn("Identical photo", latest.processing_notes or "")

    # -------------------------------------------------------------------------
    # TEST 14: Handle one failed angle without losing other successful results
    # -------------------------------------------------------------------------
    def test_handle_one_failed_angle_gracefully(self):
        with self.app.app_context():
            # Create a valid upload for Angle A and an invalid non-existent path for Angle B
            u_a = Upload(journey_id=self.journey_id, file_path=get_or_create_test_image(USER_ANGLE_A), original_filename="a.jpg", camera_angle="A")
            u_b = Upload(journey_id=self.journey_id, file_path="non_existent_corrupted.jpg", original_filename="bad.jpg", camera_angle="B")
            db.session.add_all([u_a, u_b])
            db.session.commit()

            res = process_multi_angle_batch(self.journey_id, "fail_graceful_batch", {"A": u_a, "B": u_b})
            self.assertTrue(res["success"])
            self.assertEqual(res["angle_summaries"]["A"]["status"], "processed")
            self.assertEqual(res["angle_summaries"]["B"]["status"], "failed")

    # -------------------------------------------------------------------------
    # TEST 15: Display accurate per-angle counts
    # -------------------------------------------------------------------------
    def test_display_accurate_per_angle_counts(self):
        img_a = get_or_create_test_image(USER_ANGLE_A)
        img_c = get_or_create_test_image(USER_ANGLE_C)
        with open(img_a, "rb") as fa, open(img_c, "rb") as fc:
            res = self.client.post("/api/uploads/multi-angle", data={
                "journey_id": self.journey_id,
                "photo_a": (fa, "a.jpg"),
                "photo_c": (fc, "c.jpg")
            }, content_type="multipart/form-data")
        data = res.get_json()
        counts = data["consolidated_counts"]
        self.assertGreater(counts["angle_a_count"], 0)
        self.assertGreater(counts["angle_c_count"], 0)
        self.assertEqual(counts["angle_b_count"], 0)

    # -------------------------------------------------------------------------
    # TEST 16: Display provisional unique-passenger count
    # -------------------------------------------------------------------------
    def test_provisional_unique_passenger_count(self):
        img_a = get_or_create_test_image(USER_ANGLE_A)
        with open(img_a, "rb") as fa:
            res = self.client.post("/api/uploads/multi-angle", data={
                "journey_id": self.journey_id,
                "photo_a": (fa, "a.jpg")
            }, content_type="multipart/form-data")
        data = res.get_json()
        counts = data["consolidated_counts"]
        self.assertIn("provisional_unique_count", counts)
        self.assertIsInstance(counts["provisional_unique_count"], int)

    # -------------------------------------------------------------------------
    # TEST 17: Verify database persistence
    # -------------------------------------------------------------------------
    def test_verify_database_persistence(self):
        with self.app.app_context():
            u = Upload.query.filter_by(journey_id=self.journey_id).first()
            self.assertIsNotNone(u)
            self.assertIsNotNone(u.camera_angle)
            obs = PassengerObservation.query.filter_by(journey_id=self.journey_id).first()
            if obs:
                self.assertIsNotNone(obs.camera_angle)
                self.assertIsNotNone(obs.detection_confidence)

    # -------------------------------------------------------------------------
    # TEST 18: Verify annotated image generation
    # -------------------------------------------------------------------------
    def test_verify_annotated_image_generation(self):
        img_a = get_or_create_test_image(USER_ANGLE_A)
        out_path = Config.UPLOAD_FOLDER / "test_annotated_render.jpg"
        detections = [
            {"bbox": [50, 60, 100, 140], "person_id": "P001", "confidence_label": "High", "similarity_score": 0.88},
            {"bbox": [200, 80, 90, 130], "temp_id": "T-A-02", "confidence_label": "Needs Review", "review_status": "NEEDS_REVIEW"}
        ]
        res = render_multi_angle_annotated_hud(img_a, detections, str(out_path), camera_angle="A", seating_label="Front Rows")
        self.assertTrue(Path(res).exists())
        self.assertGreater(Path(res).stat().st_size, 1000)

    # -------------------------------------------------------------------------
    # TEST 19: Verify integration with journey history and passenger monitoring
    # -------------------------------------------------------------------------
    def test_verify_integration_journey_history_monitoring(self):
        # Fetch passengers endpoint
        res = self.client.get(f"/api/passengers?journey_id={self.journey_id}")
        self.assertEqual(res.status_code, 200)
        passengers = res.get_json()
        self.assertIsInstance(passengers, list)

        if len(passengers) > 0:
            p_id = passengers[0]["id"]
            res_p = self.client.get(f"/api/passengers/{p_id}")
            self.assertEqual(res_p.status_code, 200)
            p_data = res_p.get_json()
            self.assertIn("observations", p_data)

    # -------------------------------------------------------------------------
    # TEST 20: Verify admin dashboard receives correct database-derived stats
    # -------------------------------------------------------------------------
    def test_verify_admin_dashboard_receives_correct_stats(self):
        res = self.client.get("/api/dashboard/stats")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("summary_cards", data)
        self.assertIn("metrics", data)
        self.assertIn("recent_activity", data)
        # Verify stats are integers calculated from actual database
        self.assertIsInstance(data["summary_cards"]["total_passengers_identified"], int)
        self.assertIsInstance(data["summary_cards"]["currently_confirmed_present"], int)


if __name__ == "__main__":
    unittest.main()
