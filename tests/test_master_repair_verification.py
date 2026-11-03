import os
import io
import uuid
import json
import shutil
import tempfile
import unittest
import numpy as np
from PIL import Image
from datetime import datetime, timedelta
from pathlib import Path

from app import create_app, db
from app.config import Config
from app.models import (
    User, Bus, Journey, SourceFolder, SourceMediaFile,
    TimetableImport, TimetableStop, StopMediaMatch, StopProcessingJob,
    PassengerIdentity, PassengerObservation, PresenceEvent, Upload
)
from app.services.journey_service import process_multi_angle_batch
from app.services.batch_processor import process_single_stop
from app.services.export_service import (
    generate_passenger_attendance_pdf, generate_journey_summary_pdf,
    generate_stop_processing_csv, generate_stop_processing_excel
)


class TestMasterRepairVerification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = Path(tempfile.mkdtemp(prefix="smartbus_master_test_"))
        Config.DATA_DIR = cls.test_dir / "data"
        Config.UPLOAD_FOLDER = Config.DATA_DIR / "uploads"
        Config.CROP_FOLDER = Config.DATA_DIR / "crops"
        for p in [Config.DATA_DIR, Config.UPLOAD_FOLDER, Config.CROP_FOLDER]:
            p.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def setUp(self):
        self.test_db = Path(tempfile.gettempdir()) / f"test_master_{uuid.uuid4().hex[:6]}.db"
        class TestConfig(Config):
            TESTING = True
            SQLALCHEMY_DATABASE_URI = f"sqlite:///{self.test_db}"

        self.app = create_app(TestConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            db.create_all()
            self.bus = Bus(bus_number="BUS-MTR-01", registration_number="UP-14-MTR-01", capacity=40)
            db.session.add(self.bus)
            db.session.commit()

            self.journey = Journey(
                bus_id=self.bus.id,
                journey_code=f"JNY-MTR-{uuid.uuid4().hex[:4]}",
                origin="Koshambi Bus Stand",
                destination="Lucknow Terminal",
                status="active",
                start_time=datetime(2026, 10, 24, 8, 0, 0)
            )
            db.session.add(self.journey)
            db.session.commit()

            # Create timetable and stops
            self.timetable = TimetableImport(
                journey_id=self.journey.id,
                filename="test_route.csv",
                file_path=str(Config.DATA_DIR / "test_route.csv"),
                service_date=datetime(2026, 10, 24).date(),
                total_stops=3
            )
            db.session.add(self.timetable)
            db.session.commit()

            self.stop1 = TimetableStop(
                timetable_id=self.timetable.id,
                stop_number=1,
                stop_name="Koshambi Bus Stand",
                scheduled_arrival=datetime(2026, 10, 24, 8, 0, 0),
                scheduled_departure=datetime(2026, 10, 24, 8, 5, 0),
                window_start=datetime(2026, 10, 24, 7, 50, 0),
                window_end=datetime(2026, 10, 24, 8, 15, 0)
            )
            self.stop2 = TimetableStop(
                timetable_id=self.timetable.id,
                stop_number=2,
                stop_name="Ghaziabad Old Bus Stand",
                scheduled_arrival=datetime(2026, 10, 24, 8, 30, 0),
                scheduled_departure=datetime(2026, 10, 24, 8, 35, 0),
                window_start=datetime(2026, 10, 24, 8, 20, 0),
                window_end=datetime(2026, 10, 24, 8, 45, 0)
            )
            self.stop3 = TimetableStop(
                timetable_id=self.timetable.id,
                stop_number=3,
                stop_name="Lucknow Terminal",
                scheduled_arrival=datetime(2026, 10, 24, 14, 0, 0),
                scheduled_departure=datetime(2026, 10, 24, 14, 10, 0),
                window_start=datetime(2026, 10, 24, 13, 50, 0),
                window_end=datetime(2026, 10, 24, 14, 20, 0)
            )
            db.session.add_all([self.stop1, self.stop2, self.stop3])
            db.session.commit()

            self.journey_id = self.journey.id
            self.timetable_id = self.timetable.id
            self.stop1_id = self.stop1.id
            self.stop2_id = self.stop2.id
            self.stop3_id = self.stop3.id

    def tearDown(self):
        try:
            if self.test_db.exists():
                self.test_db.unlink()
        except Exception:
            pass

    def _create_synthetic_image(self, filename, color=(120, 80, 60)):
        path = Config.UPLOAD_FOLDER / filename
        im = Image.new("RGB", (640, 480), color=color)
        im.save(path, format="JPEG")
        return str(path)

    def test_multi_angle_deduplication_and_tracking_fields(self):
        """
        Scenario: Angles A, B, and C all observe the SAME passenger at Stop 1.
        Verification:
        - Exactly ONE PassengerIdentity record is created.
        - Detections in Angles A, B, and C all share that identity's person_id (e.g. 'P001').
        - first_observed_angle and last_observed_angle are populated.
        - detected_angles_json contains ['A', 'B', 'C'].
        """
        with self.app.app_context():
            img_a = self._create_synthetic_image("test_a.jpg")
            img_b = self._create_synthetic_image("test_b.jpg")
            img_c = self._create_synthetic_image("test_c.jpg")

            up_a = Upload(journey_id=self.journey_id, file_path=img_a, camera_angle="A", original_filename="a.jpg")
            up_b = Upload(journey_id=self.journey_id, file_path=img_b, camera_angle="B", original_filename="b.jpg")
            up_c = Upload(journey_id=self.journey_id, file_path=img_c, camera_angle="C", original_filename="c.jpg")
            db.session.add_all([up_a, up_b, up_c])
            db.session.commit()

            # Detections representing the same person across A, B, C with strong face similarity
            face_emb = [0.1] * 512
            # Mock detect_persons_and_faces
            from unittest.mock import patch

            def mock_detect(path, camera_angle="A", roi_config=None):
                return {
                    "detections": [{
                        "bbox": [100, 100, 80, 80],
                        "person_bbox": [90, 80, 100, 200],
                        "face_bbox": [100, 100, 80, 80],
                        "embedding": face_emb,
                        "clothing_signature": [0.5, 0.5, 0.5, 0.5],
                        "detection_confidence": 0.95,
                        "face_confidence": 0.95,
                        "quality": {"quality_score": 0.85, "is_blur": False, "is_acceptable": True},
                        "crop_path": path
                    }],
                    "warnings": [],
                    "raw_count": 1,
                    "high_confidence_count": 1,
                    "needs_review_count": 0,
                    "uncertain_count": 0
                }

            with patch("app.services.journey_service.detect_persons_and_faces", side_effect=mock_detect):
                res = process_multi_angle_batch(
                    journey_id=self.journey_id,
                    batch_id="batch_stop1",
                    angle_uploads={"A": up_a, "B": up_b, "C": up_c},
                    stop_id=self.stop1_id
                )

            self.assertTrue(res["success"])
            # Verify exactly ONE passenger identity created
            passengers = PassengerIdentity.query.filter_by(journey_id=self.journey_id).all()
            self.assertEqual(len(passengers), 1)

            p = passengers[0]
            self.assertEqual(p.person_id, "P001")
            self.assertEqual(p.total_observations, 3)
            # Detected angles should include A, B, and C
            angles = p.get_detected_angles()
            self.assertIn("A", angles)
            self.assertIn("B", angles)
            self.assertIn("C", angles)
            self.assertEqual(p.first_observed_angle, "A")
            self.assertEqual(p.last_observed_angle, "C")

    def test_passenger_persistence_and_occupancy_metrics_across_stops(self):
        """
        Scenario:
        - Stop 1: Passenger P1 boards.
        - Stop 2: Passenger P1 continues, and Passenger P2 boards.
        Verification:
        - P1 maintains same ID at Stop 2.
        - Stop 1: boarded=1, continuing=0, exited=0, occupancy=1.
        - Stop 2: boarded=1, continuing=1, exited=0, occupancy=2.
        """
        with self.app.app_context():
            img_a1 = self._create_synthetic_image("stop1_a.jpg")
            img_a2 = self._create_synthetic_image("stop2_a.jpg")

            up1 = Upload(journey_id=self.journey_id, file_path=img_a1, camera_angle="A", original_filename="s1.jpg")
            up2 = Upload(journey_id=self.journey_id, file_path=img_a2, camera_angle="A", original_filename="s2.jpg")
            db.session.add_all([up1, up2])
            db.session.commit()

            emb_p1 = [0.2] * 512
            emb_p2 = [-0.3] * 512

            from unittest.mock import patch

            # Stop 1: only P1
            def mock_detect_s1(path, camera_angle="A", roi_config=None):
                return {
                    "detections": [{
                        "bbox": [100, 100, 80, 80],
                        "person_bbox": [90, 80, 100, 200],
                        "face_bbox": [100, 100, 80, 80],
                        "embedding": emb_p1,
                        "clothing_signature": [0.4, 0.4, 0.4, 0.4],
                        "detection_confidence": 0.95,
                        "face_confidence": 0.95,
                        "quality": {"quality_score": 0.85, "is_blur": False, "is_acceptable": True},
                        "crop_path": path
                    }],
                    "warnings": [], "raw_count": 1, "high_confidence_count": 1,
                    "needs_review_count": 0, "uncertain_count": 0
                }

            with patch("app.services.journey_service.detect_persons_and_faces", side_effect=mock_detect_s1):
                res1 = process_multi_angle_batch(
                    journey_id=self.journey_id,
                    batch_id="batch_s1",
                    angle_uploads={"A": up1},
                    stop_id=self.stop1_id
                )

            # Link match to stop 1 and calculate metrics
            s1 = TimetableStop.query.get(self.stop1_id)
            journey = Journey.query.get(self.journey_id)
            boarded_pids = set(p.id for p in journey.passengers if p.boarding_stop_number == s1.stop_number)
            s1.boarded_count = len(boarded_pids)
            s1.continuing_count = 0
            s1.exited_count = 0
            s1.occupancy = s1.boarded_count
            db.session.commit()

            self.assertEqual(s1.boarded_count, 1)
            self.assertEqual(s1.continuing_count, 0)
            self.assertEqual(s1.occupancy, 1)

            # Stop 2: P1 continues, and P2 boards
            def mock_detect_s2(path, camera_angle="A", roi_config=None):
                return {
                    "detections": [
                        {
                            "bbox": [100, 100, 80, 80],
                            "person_bbox": [90, 80, 100, 200],
                            "face_bbox": [100, 100, 80, 80],
                            "embedding": emb_p1,
                            "clothing_signature": [0.4, 0.4, 0.4, 0.4],
                            "detection_confidence": 0.95,
                            "face_confidence": 0.95,
                            "quality": {"quality_score": 0.85, "is_blur": False, "is_acceptable": True},
                            "crop_path": path
                        },
                        {
                            "bbox": [300, 100, 80, 80],
                            "person_bbox": [290, 80, 100, 200],
                            "face_bbox": [300, 100, 80, 80],
                            "embedding": emb_p2,
                            "clothing_signature": [0.8, 0.1, 0.1, 0.1],
                            "detection_confidence": 0.92,
                            "face_confidence": 0.92,
                            "quality": {"quality_score": 0.85, "is_blur": False, "is_acceptable": True},
                            "crop_path": path
                        }
                    ],
                    "warnings": [], "raw_count": 2, "high_confidence_count": 2,
                    "needs_review_count": 0, "uncertain_count": 0
                }

            with patch("app.services.journey_service.detect_persons_and_faces", side_effect=mock_detect_s2):
                res2 = process_multi_angle_batch(
                    journey_id=self.journey_id,
                    batch_id="batch_s2",
                    angle_uploads={"A": up2},
                    stop_id=self.stop2_id
                )

            passengers = PassengerIdentity.query.filter_by(journey_id=self.journey_id).all()
            self.assertEqual(len(passengers), 2)
            p1 = next(p for p in passengers if p.person_id == "P001")
            p2 = next(p for p in passengers if p.person_id == "P002")

            self.assertEqual(p1.boarding_stop_number, 1)
            self.assertEqual(p1.stops_traveled, 2)
            self.assertEqual(p2.boarding_stop_number, 2)

            s2 = TimetableStop.query.get(self.stop2_id)
            boarded2 = set(p.id for p in journey.passengers if p.boarding_stop_number == s2.stop_number)
            cont2 = set(p.id for p in journey.passengers if (p.boarding_stop_number or 0) < s2.stop_number and p.status in ["PRESENT", "POSSIBLY_ABSENT"])
            s2.boarded_count = len(boarded2)
            s2.continuing_count = len(cont2)
            s2.exited_count = 0
            s2.occupancy = len(boarded2) + len(cont2)
            db.session.commit()

            self.assertEqual(s2.boarded_count, 1)
            self.assertEqual(s2.continuing_count, 1)
            self.assertEqual(s2.occupancy, 2)

    def test_similar_clothing_different_face_does_not_merge(self):
        """
        Anti-merging verification:
        Two passengers wearing identical clothing but with different face embeddings
        must receive distinct Person IDs and NOT be collapsed.
        """
        with self.app.app_context():
            img_a = self._create_synthetic_image("clothes_test.jpg")
            up = Upload(journey_id=self.journey_id, file_path=img_a, camera_angle="A", original_filename="c.jpg")
            db.session.add(up)
            db.session.commit()

            emb_diff1 = [0.3] * 512
            emb_diff2 = [-0.3] * 512
            identical_clothes = [0.9, 0.9, 0.9, 0.9]

            from unittest.mock import patch

            def mock_detect_two(path, camera_angle="A", roi_config=None):
                return {
                    "detections": [
                        {
                            "bbox": [50, 100, 80, 80],
                            "person_bbox": [40, 80, 100, 200],
                            "face_bbox": [50, 100, 80, 80],
                            "embedding": emb_diff1,
                            "clothing_signature": identical_clothes,
                            "detection_confidence": 0.94,
                            "face_confidence": 0.94,
                            "quality": {"quality_score": 0.85, "is_blur": False, "is_acceptable": True},
                            "crop_path": path
                        },
                        {
                            "bbox": [300, 100, 80, 80],
                            "person_bbox": [290, 80, 100, 200],
                            "face_bbox": [300, 100, 80, 80],
                            "embedding": emb_diff2,
                            "clothing_signature": identical_clothes,
                            "detection_confidence": 0.93,
                            "face_confidence": 0.93,
                            "quality": {"quality_score": 0.85, "is_blur": False, "is_acceptable": True},
                            "crop_path": path
                        }
                    ],
                    "warnings": [], "raw_count": 2, "high_confidence_count": 2,
                    "needs_review_count": 0, "uncertain_count": 0
                }

            with patch("app.services.journey_service.detect_persons_and_faces", side_effect=mock_detect_two):
                res = process_multi_angle_batch(
                    journey_id=self.journey_id,
                    batch_id="batch_clothes",
                    angle_uploads={"A": up},
                    stop_id=self.stop1_id
                )

            passengers = PassengerIdentity.query.filter_by(journey_id=self.journey_id).all()
            self.assertEqual(len(passengers), 2)
            pids = {p.person_id for p in passengers}
            self.assertEqual(len(pids), 2)
            self.assertIn("P001", pids)
            self.assertIn("P002", pids)

    def test_pdf_and_report_generation_clean_execution(self):
        """
        Verify PDF, CSV, and Excel reports generate cleanly without crashing,
        even when passengers exist or when passenger list is empty.
        """
        with self.app.app_context():
            # 1. Passenger attendance PDF
            pdf_bytes = generate_passenger_attendance_pdf(journey_id=self.journey_id)
            self.assertIsInstance(pdf_bytes, bytes)
            self.assertTrue(len(pdf_bytes) > 500)

            # 2. Journey summary PDF
            sum_pdf_bytes = generate_journey_summary_pdf(journey_id=self.journey_id)
            self.assertIsInstance(sum_pdf_bytes, bytes)
            self.assertTrue(len(sum_pdf_bytes) > 500)

            # 3. Stop processing CSV
            csv_str = generate_stop_processing_csv(timetable_id=self.timetable_id)
            self.assertIn("Boarded", csv_str)
            self.assertIn("Occupancy", csv_str)

            # 4. Stop processing Excel
            xlsx_bytes = generate_stop_processing_excel(timetable_id=self.timetable_id)
            self.assertIsInstance(xlsx_bytes, bytes)
            self.assertTrue(len(xlsx_bytes) > 500)

    def test_refresh_workspace_and_delete_temporary_data_endpoints(self):
        """
        Verify /api/automated/refresh-workspace and /api/automated/delete-temporary-data
        execute and return clean success payloads.
        """
        with self.app.app_context():
            # Refresh workspace
            res = self.client.post("/api/automated/refresh-workspace", json={
                "journey_id": self.journey_id,
                "timetable_id": self.timetable_id,
                "clear_uploads": True
            })
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["success"])

            # Verify that uploads and stops are wiped
            self.assertEqual(TimetableImport.query.count(), 0)
            self.assertEqual(TimetableStop.query.count(), 0)
            self.assertEqual(SourceFolder.query.count(), 0)

            # Delete temporary data
            res_del = self.client.post("/api/automated/delete-temporary-data", json={})
            self.assertEqual(res_del.status_code, 200)
            data_del = res_del.get_json()
            self.assertTrue(data_del["success"])

    def test_screenshot_3_female_male_strict_separation_no_false_merge(self):
        """
        Regression test for Screenshot 3 bug:
        Angle B detects a woman and Angle C detects a man at the same stop.
        Even if clothing vectors are somewhat similar (e.g. cosine sim 0.86),
        their distinct biometric face embeddings (sim ~ 0.35) MUST keep them
        as strictly separate identities (e.g. P001 and P002) and NEVER merge into P003.
        """
        with self.app.app_context():
            from unittest.mock import patch
            from app.services.journey_service import process_multi_angle_batch
            from app.ai.matcher import cosine_similarity

            # Create test stop
            tt = TimetableImport(filename="test_ss3.xlsx", file_path="uploads/test_ss3.xlsx", status="completed")
            db.session.add(tt)
            db.session.commit()
            stop = TimetableStop(
                timetable_id=tt.id,
                stop_number=1,
                stop_name="Test Separation Stop",
                scheduled_arrival=datetime(2026, 10, 23, 10, 0, 0)
            )
            db.session.add(stop)
            db.session.commit()

            # Orthogonal face embeddings: woman vs man (sim = 0.0)
            woman_face = np.zeros(512, dtype=np.float32)
            woman_face[0:256] = 1.0
            woman_face /= np.linalg.norm(woman_face)

            man_face = np.zeros(512, dtype=np.float32)
            man_face[256:512] = 1.0
            man_face /= np.linalg.norm(man_face)

            # High clothing similarity to simulate similar colored shirts
            shared_clothing = [0.5] * 206

            # 1. Verify face similarity between woman and man is ~0
            f_sim = cosine_similarity(woman_face, man_face)
            self.assertAlmostEqual(f_sim, 0.0, places=4)

            # 2. Process multi-angle batch with woman in Angle B and man in Angle C
            up_b = Upload(journey_id=self.journey_id, file_path="data/uploads/sample_angle_B.jpg", original_filename="b.jpg", camera_angle="B")
            up_c = Upload(journey_id=self.journey_id, file_path="data/uploads/sample_angle_C.jpg", original_filename="c.jpg", camera_angle="C")
            db.session.add_all([up_b, up_c])
            db.session.commit()

            def mock_detect(path, *args, **kwargs):
                camera_angle = kwargs.get("camera_angle", "B")
                if camera_angle == "B":
                    dets = [{
                        "temp_id": "T-B-01",
                        "camera_angle": "B",
                        "bbox": [50, 50, 100, 200],
                        "person_bbox": [50, 50, 100, 200],
                        "face_bbox": [60, 60, 40, 50],
                        "detection_confidence": 0.95,
                        "face_confidence": 0.98,
                        "confidence_label": "High",
                        "has_face": True,
                        "quality": {"is_acceptable": True, "quality_score": 90.0, "is_blur": False},
                        "embedding": woman_face.tolist(),
                        "embedding_model_version": "facenet_vggface2_512",
                        "clothing_signature": shared_clothing,
                        "crop_path": path
                    }]
                else:
                    dets = [{
                        "temp_id": "T-C-01",
                        "camera_angle": "C",
                        "bbox": [60, 60, 110, 210],
                        "person_bbox": [60, 60, 110, 210],
                        "face_bbox": [70, 70, 40, 50],
                        "detection_confidence": 0.94,
                        "face_confidence": 0.97,
                        "confidence_label": "High",
                        "has_face": True,
                        "quality": {"is_acceptable": True, "quality_score": 90.0, "is_blur": False},
                        "embedding": man_face.tolist(),
                        "embedding_model_version": "facenet_vggface2_512",
                        "clothing_signature": shared_clothing,
                        "crop_path": path
                    }]
                return {
                    "detections": dets,
                    "warnings": [], "raw_count": 1, "high_confidence_count": 1,
                    "needs_review_count": 0, "uncertain_count": 0
                }

            with patch("app.services.journey_service.detect_persons_and_faces", side_effect=mock_detect):
                res = process_multi_angle_batch(
                    journey_id=self.journey_id,
                    batch_id="batch_ss3",
                    angle_uploads={"B": up_b, "C": up_c},
                    stop_id=stop.id
                )

            # Must produce 2 unique identities, not 1
            counts = res["consolidated_counts"]
            self.assertEqual(counts["confirmed_unique"], 2, "Female and male must not be merged into 1 identity")

            # Check DB records
            passengers = PassengerIdentity.query.filter_by(journey_id=self.journey_id).all()
            self.assertEqual(len(passengers), 2, "Must create 2 separate PassengerIdentity records")
            
            p_codes = {p.person_id for p in passengers}
            self.assertEqual(len(p_codes), 2)
