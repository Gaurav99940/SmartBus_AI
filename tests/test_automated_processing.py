import os
import io
import uuid
import json
import shutil
import tempfile
import unittest
import numpy as np
import pandas as pd
from PIL import Image
from datetime import datetime, timedelta
from pathlib import Path

from app import create_app, db
from app.config import Config
from app.models import (
    User, Bus, Journey, SourceFolder, SourceMediaFile,
    TimetableImport, TimetableStop, StopMediaMatch, StopProcessingJob
)
from app.services.media_service import (
    scan_directory, extract_media_metadata, extract_frame_at_timestamp,
    parse_timestamp_from_filename
)
from app.services.timetable_service import parse_timetable_file, parse_flexible_time
from app.services.batch_processor import match_stops_to_media, process_single_stop
from app.services.export_service import (
    generate_stop_processing_csv, generate_stop_processing_excel,
    generate_processing_manifest_csv
)


class TestAutomatedProcessing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = Path(tempfile.mkdtemp(prefix="smartbus_auto_test_"))
        Config.DATA_DIR = cls.test_dir / "data"
        Config.UPLOAD_FOLDER = Config.DATA_DIR / "uploads"
        Config.CROP_FOLDER = Config.DATA_DIR / "crops"
        for p in [Config.DATA_DIR, Config.UPLOAD_FOLDER, Config.CROP_FOLDER]:
            p.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def setUp(self):
        import tempfile
        self.test_db = Path(tempfile.gettempdir()) / f"test_smartbus_{uuid.uuid4().hex[:6]}.db"
        class TestConfig(Config):
            TESTING = True
            SQLALCHEMY_DATABASE_URI = f"sqlite:///{self.test_db}"

        self.app = create_app(TestConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            db.create_all()
            uid = uuid.uuid4().hex[:6]
            self.bus = Bus(
                bus_number=f"BUS-AUTO-{uid}",
                registration_number=f"UP-14-BT-{uid}",
                bus_name="Express Cruiser"
            )
            db.session.add(self.bus)
            db.session.flush()

            self.journey = Journey(
                journey_code=f"JRN-AUTO-{uid}",
                bus_id=self.bus.id,
                origin="Koshambi Bus Stand",
                destination="Bareilly Junction",
                status="active"
            )
            db.session.add(self.journey)
            db.session.commit()

            self.journey_id = self.journey.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
        if hasattr(self, "test_db") and self.test_db.exists():
            try:
                self.test_db.unlink()
            except Exception:
                pass

    def test_01_filename_timestamp_parsing(self):
        """Test timestamp extraction from various media filename conventions."""
        fn1 = "clip_A_20261014_080000.mp4"
        dt1 = parse_timestamp_from_filename(fn1)
        self.assertIsNotNone(dt1)
        self.assertEqual(dt1.hour, 8)
        self.assertEqual(dt1.minute, 0)

        fn2 = "angle_b_2026-10-14_08-25-30.avi"
        dt2 = parse_timestamp_from_filename(fn2)
        self.assertIsNotNone(dt2)
        self.assertEqual(dt2.minute, 25)
        self.assertEqual(dt2.second, 30)

    def test_02_source_folder_scanning_and_safe_access(self):
        """Test directory scanning, read-only behavior and media file categorization."""
        angle_a_dir = self.test_dir / "Angle_A"
        angle_a_dir.mkdir(exist_ok=True)

        # Create 2 dummy images
        img1 = Image.new("RGB", (640, 480), color=(100, 150, 200))
        img1.save(angle_a_dir / "angle_A_20261014_080000.jpg")
        img2 = Image.new("RGB", (640, 480), color=(50, 80, 120))
        img2.save(angle_a_dir / "angle_A_20261014_082500.jpg")

        res = scan_directory(str(angle_a_dir))
        self.assertTrue(res["success"])
        self.assertEqual(res["images_count"], 2)
        self.assertEqual(res["clips_count"], 0)
        self.assertEqual(len(res["files"]), 2)

        # Ensure source files were not modified
        self.assertTrue((angle_a_dir / "angle_A_20261014_080000.jpg").exists())

    def test_03_csv_and_excel_timetable_parsing(self):
        """Test parsing of CSV and Excel timetable files with custom columns."""
        csv_path = self.test_dir / "timetable.csv"
        csv_content = """Stop Number,Stop Name,Arrival Time,Departure Time
1,Koshambi Bus Stand,08:00:00,08:05:00
2,Ghaziabad Old Bus Stand,08:25:00,08:30:00
3,Moradabad Bypass,08:50:00,08:55:00
4,Bareilly Satellite,09:15:00,09:20:00
"""
        csv_path.write_text(csv_content)

        parsed = parse_timetable_file(
            file_path=str(csv_path),
            service_date="2026-10-14",
            tolerance_minutes=3
        )
        self.assertTrue(parsed["success"])
        self.assertEqual(parsed["total_stops"], 4)
        stops = parsed["stops"]
        self.assertEqual(stops[0]["stop_name"], "Koshambi Bus Stand")
        self.assertEqual(stops[0]["scheduled_arrival"].hour, 8)
        self.assertEqual(stops[0]["scheduled_departure"].minute, 5)

        # Test Excel generation & parsing
        xlsx_path = self.test_dir / "timetable.xlsx"
        df = pd.DataFrame([
            {"Stop #": 1, "Location": "Terminal 1", "IN": "10:00:00", "OUT": "10:10:00"},
            {"Stop #": 2, "Location": "Terminal 2", "IN": "10:30:00", "OUT": "10:40:00"}
        ])
        df.to_excel(xlsx_path, index=False)

        parsed_xlsx = parse_timetable_file(str(xlsx_path), service_date="2026-10-14")
        self.assertTrue(parsed_xlsx["success"])
        self.assertEqual(parsed_xlsx["total_stops"], 2)

    def test_04_synchronized_three_angle_matching(self):
        """Test matching media across Angles A, B, C for scheduled stop windows."""
        with self.app.app_context():
            tt = TimetableImport(
                journey_id=self.journey_id,
                filename="test_timetable.csv",
                file_path=str(self.test_dir / "tt.csv"),
                total_stops=2,
                status="valid"
            )
            db.session.add(tt)
            db.session.flush()

            base_dt = datetime(2026, 10, 14, 8, 0, 0)
            s1 = TimetableStop(
                timetable_id=tt.id,
                stop_number=1,
                stop_name="Stop Alpha",
                scheduled_arrival=base_dt,
                scheduled_departure=base_dt + timedelta(minutes=5),
                window_start=base_dt - timedelta(minutes=2),
                window_end=base_dt + timedelta(minutes=7)
            )
            db.session.add(s1)
            db.session.flush()

            # Create media folders & files for Angles A, B, C
            for ang in ["A", "B", "C"]:
                folder = SourceFolder(
                    journey_id=self.journey_id,
                    camera_angle=ang,
                    folder_path=str(self.test_dir / f"folder_{ang}"),
                    folder_name=f"folder_{ang}"
                )
                db.session.add(folder)
                db.session.flush()

                # Create dummy image on disk
                img_path = self.test_dir / f"img_{ang}.jpg"
                Image.new("RGB", (320, 240), color=(100, 100, 100)).save(img_path)

                mf = SourceMediaFile(
                    source_folder_id=folder.id,
                    journey_id=self.journey_id,
                    camera_angle=ang,
                    file_path=str(img_path),
                    filename=f"img_{ang}.jpg",
                    file_ext=".jpg",
                    media_type="image",
                    start_time=base_dt + timedelta(minutes=1),
                    end_time=base_dt + timedelta(minutes=1),
                    is_timestamp_reliable=True
                )
                db.session.add(mf)

            db.session.commit()

            success, msg = match_stops_to_media(tt.id)
            self.assertTrue(success)

            refreshed_s1 = TimetableStop.query.get(s1.id)
            self.assertEqual(refreshed_s1.coverage_status, "FULLY_COVERED")
            self.assertEqual(len(refreshed_s1.matches), 3)

    def test_05_missing_angle_partial_coverage(self):
        """Test partial coverage when Angle C media is missing."""
        with self.app.app_context():
            tt = TimetableImport(
                journey_id=self.journey_id,
                filename="test_tt2.csv",
                file_path=str(self.test_dir / "tt2.csv"),
                total_stops=1,
                status="valid"
            )
            db.session.add(tt)
            db.session.flush()

            base_dt = datetime(2026, 10, 14, 9, 0, 0)
            s = TimetableStop(
                timetable_id=tt.id,
                stop_number=1,
                stop_name="Stop Beta",
                scheduled_arrival=base_dt,
                scheduled_departure=base_dt + timedelta(minutes=5),
                window_start=base_dt - timedelta(minutes=2),
                window_end=base_dt + timedelta(minutes=7)
            )
            db.session.add(s)

            # Add media for Angles A and B only
            for ang in ["A", "B"]:
                folder = SourceFolder(
                    journey_id=self.journey_id,
                    camera_angle=ang,
                    folder_path=str(self.test_dir / f"f_{ang}"),
                    folder_name=f"f_{ang}"
                )
                db.session.add(folder)
                db.session.flush()

                img_path = self.test_dir / f"p_{ang}.jpg"
                Image.new("RGB", (320, 240)).save(img_path)

                mf = SourceMediaFile(
                    source_folder_id=folder.id,
                    journey_id=self.journey_id,
                    camera_angle=ang,
                    file_path=str(img_path),
                    filename=f"p_{ang}.jpg",
                    file_ext=".jpg",
                    media_type="image",
                    start_time=base_dt,
                    end_time=base_dt,
                    is_timestamp_reliable=True
                )
                db.session.add(mf)

            db.session.commit()

            success, msg = match_stops_to_media(tt.id)
            self.assertTrue(success)

            refreshed_s = TimetableStop.query.get(s.id)
            self.assertEqual(refreshed_s.coverage_status, "PARTIALLY_COVERED")
            self.assertIn("C", refreshed_s.missing_angles)

    def test_06_single_stop_processing_and_pax_counting(self):
        """Test end-to-end execution of process_single_stop."""
        with self.app.app_context():
            tt = TimetableImport(
                journey_id=self.journey_id,
                filename="test_run.csv",
                file_path=str(self.test_dir / "tt_run.csv"),
                total_stops=1,
                status="valid"
            )
            db.session.add(tt)
            db.session.flush()

            base_dt = datetime(2026, 10, 14, 11, 0, 0)
            stop = TimetableStop(
                timetable_id=tt.id,
                stop_number=1,
                stop_name="Terminal Alpha",
                scheduled_arrival=base_dt,
                scheduled_departure=base_dt + timedelta(minutes=5),
                window_start=base_dt - timedelta(minutes=2),
                window_end=base_dt + timedelta(minutes=7)
            )
            db.session.add(stop)
            db.session.flush()

            for ang in ["A", "B", "C"]:
                f = SourceFolder(journey_id=self.journey_id, camera_angle=ang, folder_path=str(self.test_dir), folder_name="test")
                db.session.add(f)
                db.session.flush()

                img_path = self.test_dir / f"test_{ang}.jpg"
                Image.new("RGB", (320, 240), color=(180, 80, 40)).save(img_path)

                mf = SourceMediaFile(
                    source_folder_id=f.id,
                    journey_id=self.journey_id,
                    camera_angle=ang,
                    file_path=str(img_path),
                    filename=f"test_{ang}.jpg",
                    file_ext=".jpg",
                    media_type="image",
                    start_time=base_dt
                )
                db.session.add(mf)
                db.session.flush()

                match = StopMediaMatch(
                    stop_id=stop.id,
                    camera_angle=ang,
                    source_media_file_id=mf.id,
                    source_filename=mf.filename,
                    source_folder=mf.file_path,
                    media_type="image",
                    capture_timestamp=base_dt,
                    status="matched"
                )
                db.session.add(match)

            db.session.commit()

            res = process_single_stop(stop.id)
            self.assertTrue(res["success"])

            refreshed = TimetableStop.query.get(stop.id)
            self.assertEqual(refreshed.processing_status, "completed")
            self.assertIn(refreshed.coverage_status, ["FULLY_COVERED", "NEEDS_REVIEW"])

    def test_07_csv_and_excel_export_generation(self):
        """Test export of stop-wise reports and processing manifest."""
        with self.app.app_context():
            tt = TimetableImport(
                journey_id=self.journey_id,
                filename="export_test.csv",
                file_path=str(self.test_dir / "exp.csv"),
                total_stops=1,
                status="valid"
            )
            db.session.add(tt)
            db.session.flush()

            stop = TimetableStop(
                timetable_id=tt.id,
                stop_number=1,
                stop_name="Express Junction",
                scheduled_arrival=datetime(2026, 10, 14, 12, 0, 0),
                scheduled_departure=datetime(2026, 10, 14, 12, 5, 0),
                coverage_status="FULLY_COVERED",
                processing_status="completed",
                angle_a_count=2,
                angle_b_count=3,
                angle_c_count=1,
                estimated_unique_passengers=5
            )
            db.session.add(stop)
            db.session.commit()

            csv_data = generate_stop_processing_csv(tt.id)
            self.assertIn("Express Junction", csv_data)
            self.assertIn("FULLY_COVERED", csv_data)

            excel_bytes = generate_stop_processing_excel(tt.id)
            self.assertGreater(len(excel_bytes), 0)

            manifest = generate_processing_manifest_csv(tt.id)
            self.assertIn("MEDIA PROCESSING MANIFEST", manifest)


if __name__ == "__main__":
    unittest.main()
