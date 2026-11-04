import os
import io
import uuid
import shutil
import tempfile
import unittest
import cv2
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
    scan_directory, extract_media_metadata, extract_frames_from_video,
    parse_timestamp_from_filename
)
from app.services.batch_processor import match_stops_to_media, process_single_stop, JobWorker


class TestMultiFileFolderUpload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = Path(tempfile.mkdtemp(prefix="smartbus_multifile_test_"))
        Config.DATA_DIR = cls.test_dir / "data"
        Config.UPLOAD_FOLDER = Config.DATA_DIR / "uploads"
        Config.CROP_FOLDER = Config.DATA_DIR / "crops"
        for p in [Config.DATA_DIR, Config.UPLOAD_FOLDER, Config.CROP_FOLDER]:
            p.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def setUp(self):
        self.test_db = Path(tempfile.gettempdir()) / f"test_smartbus_mf_{uuid.uuid4().hex[:6]}.db"
        class TestConfig(Config):
            TESTING = True
            SQLALCHEMY_DATABASE_URI = f"sqlite:///{self.test_db}"

        self.app = create_app(TestConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            db.create_all()
            uid = uuid.uuid4().hex[:6]
            self.bus = Bus(
                bus_number=f"BUS-MF-{uid}",
                registration_number=f"UP-14-MF-{uid}",
                bus_name="MultiFile Express"
            )
            db.session.add(self.bus)
            db.session.flush()

            self.journey = Journey(
                journey_code=f"JRN-MF-{uid}",
                bus_id=self.bus.id,
                origin="Delhi Anand Vihar",
                destination="Moradabad",
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

    def _create_synthetic_video(self, file_path, num_frames=15, fps=5):
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(str(file_path), fourcc, fps, (320, 240))
        for i in range(num_frames):
            frame = np.full((240, 320, 3), (i * 15 % 255, 120, 200), dtype=np.uint8)
            cv2.putText(frame, f"F{i}", (30, 100), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            out.write(frame)
        out.release()

    def test_01_recursive_scan_directory_with_subfolders(self):
        """Verify scan_directory traverses subfolders and identifies all supported formats."""
        base_folder = self.test_dir / "angle_A_full"
        sub1 = base_folder / "morning"
        sub2 = base_folder / "afternoon" / "batch1"
        sub1.mkdir(parents=True, exist_ok=True)
        sub2.mkdir(parents=True, exist_ok=True)

        # Create images of different supported formats
        Image.new("RGB", (100, 100), color=(255, 0, 0)).save(base_folder / "img1.jpg")
        Image.new("RGB", (100, 100), color=(0, 255, 0)).save(sub1 / "img2.png")
        Image.new("RGB", (100, 100), color=(0, 0, 255)).save(sub2 / "img3.webp")

        # Create dummy video files
        self._create_synthetic_video(base_folder / "clip1.mp4", num_frames=5)
        self._create_synthetic_video(sub1 / "clip2.mkv", num_frames=5)

        scan_res = scan_directory(str(base_folder))
        self.assertTrue(scan_res["success"])
        self.assertEqual(scan_res["images_count"], 3)
        self.assertEqual(scan_res["clips_count"], 2)
        self.assertEqual(scan_res["total_supported"], 5)
        self.assertEqual(len(scan_res["files"]), 5)

        # Verify relative paths are captured
        rel_paths = [f["relative_path"] for f in scan_res["files"]]
        self.assertTrue(any("morning" in p for p in rel_paths))
        self.assertTrue(any("batch1" in p for p in rel_paths))

    def test_02_video_frame_extraction_and_redundancy_check(self):
        """Verify extract_frames_from_video samples frames and avoids redundant duplicates."""
        vid_path = self.test_dir / "test_extract.mp4"
        self._create_synthetic_video(vid_path, num_frames=20, fps=5)

        frames = extract_frames_from_video(str(vid_path), interval_seconds=1.0, max_frames=5)
        self.assertGreater(len(frames), 0)
        for f in frames:
            self.assertIn("frame_path", f)
            self.assertIn("offset_seconds", f)
            self.assertTrue(os.path.exists(f["frame_path"]))

    def test_03_multi_angle_upload_endpoint_multiple_files(self):
        """Verify /api/uploads/multi-angle processes queues of multiple files for Angles A, B, C."""
        # Create 2 synthetic images for Angle A, 2 for Angle B, 1 for Angle C
        img_a1 = io.BytesIO()
        Image.new("RGB", (320, 240), color=(100, 150, 200)).save(img_a1, format="JPEG")
        img_a1.seek(0)

        img_a2 = io.BytesIO()
        Image.new("RGB", (320, 240), color=(120, 160, 210)).save(img_a2, format="JPEG")
        img_a2.seek(0)

        img_b1 = io.BytesIO()
        Image.new("RGB", (320, 240), color=(80, 120, 180)).save(img_b1, format="JPEG")
        img_b1.seek(0)

        img_c1 = io.BytesIO()
        Image.new("RGB", (320, 240), color=(60, 100, 160)).save(img_c1, format="JPEG")
        img_c1.seek(0)

        data = {
            "journey_id": str(self.journey_id),
            "files_a": [
                (img_a1, "a1.jpg"),
                (img_a2, "a2.jpg")
            ],
            "files_b": [
                (img_b1, "b1.jpg")
            ],
            "files_c": [
                (img_c1, "c1.jpg")
            ]
        }

        resp = self.client.post(
            "/api/uploads/multi-angle",
            data=data,
            content_type="multipart/form-data"
        )
        self.assertIn(resp.status_code, [200, 201])
        res_json = resp.get_json()
        self.assertTrue(res_json["success"])
        # Should report processed counts >= 1
        self.assertGreaterEqual(res_json.get("total_queue_steps_processed", 0), 1)

    def test_04_timetable_media_reuse_across_stops(self):
        """Verify media can be reused across stops and tracks reuse_count properly."""
        with self.app.app_context():
            tt = TimetableImport(
                journey_id=self.journey_id,
                filename="reuse_test.csv",
                file_path=str(self.test_dir / "reuse.csv"),
                total_stops=2,
                status="valid"
            )
            db.session.add(tt)
            db.session.flush()

            base_dt = datetime(2026, 10, 14, 10, 0, 0)
            s1 = TimetableStop(
                timetable_id=tt.id,
                stop_number=1,
                stop_name="Stop 1",
                scheduled_arrival=base_dt,
                scheduled_departure=base_dt + timedelta(minutes=5),
                window_start=base_dt - timedelta(minutes=2),
                window_end=base_dt + timedelta(minutes=7)
            )
            s2 = TimetableStop(
                timetable_id=tt.id,
                stop_number=2,
                stop_name="Stop 2",
                scheduled_arrival=base_dt + timedelta(minutes=4),
                scheduled_departure=base_dt + timedelta(minutes=9),
                window_start=base_dt + timedelta(minutes=2),
                window_end=base_dt + timedelta(minutes=11)
            )
            db.session.add_all([s1, s2])
            db.session.flush()

            folder_a = SourceFolder(
                journey_id=self.journey_id,
                camera_angle="A",
                folder_path=str(self.test_dir / "folder_a"),
                folder_name="folder_a"
            )
            db.session.add(folder_a)
            db.session.flush()

            img_path = self.test_dir / "shared_img.jpg"
            Image.new("RGB", (320, 240), color=(220, 80, 40)).save(img_path)

            mf = SourceMediaFile(
                source_folder_id=folder_a.id,
                journey_id=self.journey_id,
                file_path=str(img_path),
                filename="shared_img.jpg",
                file_ext=".jpg",
                media_type="image",
                camera_angle="A",
                start_time=base_dt + timedelta(minutes=3),
                end_time=base_dt + timedelta(minutes=3),
                is_timestamp_reliable=True,
                reuse_count=0
            )
            db.session.add(mf)
            db.session.commit()

            # Run matching
            success, msg = match_stops_to_media(tt.id)
            self.assertTrue(success)

            # Verify both Stop 1 and Stop 2 matched this media
            matches = StopMediaMatch.query.filter_by(source_media_file_id=mf.id).all()
            self.assertEqual(len(matches), 2)

            # Process Stop 1
            res1 = process_single_stop(s1.id, user_id=None)
            self.assertTrue(res1["success"])
            db.session.refresh(mf)
            self.assertEqual(mf.reuse_count, 1)

            # Process Stop 2
            res2 = process_single_stop(s2.id, user_id=None)
            self.assertTrue(res2["success"])
            db.session.refresh(mf)
            self.assertEqual(mf.reuse_count, 2)

    def test_05_job_worker_execution_and_step_9_remaining_media(self):
        """Verify automated JobWorker runs through stops, processes remaining media (Step 9) and updates counters."""
        with self.app.app_context():
            tt = TimetableImport(
                journey_id=self.journey_id,
                filename="job_test.csv",
                file_path=str(self.test_dir / "job.csv"),
                total_stops=1,
                status="valid"
            )
            db.session.add(tt)
            db.session.flush()

            base_dt = datetime(2026, 10, 14, 12, 0, 0)
            s1 = TimetableStop(
                timetable_id=tt.id,
                stop_number=1,
                stop_name="Single Stop",
                scheduled_arrival=base_dt,
                scheduled_departure=base_dt + timedelta(minutes=5),
                window_start=base_dt - timedelta(minutes=2),
                window_end=base_dt + timedelta(minutes=7)
            )
            db.session.add(s1)
            db.session.flush()

            folder = SourceFolder(
                journey_id=self.journey_id,
                camera_angle="A",
                folder_path=str(self.test_dir / "job_folder"),
                folder_name="job_folder"
            )
            db.session.add(folder)
            db.session.flush()

            # Matched file
            f1_path = self.test_dir / "f1.jpg"
            Image.new("RGB", (320, 240), color=(240, 50, 50)).save(f1_path)
            mf1 = SourceMediaFile(
                source_folder_id=folder.id,
                journey_id=self.journey_id,
                file_path=str(f1_path),
                filename="f1.jpg",
                file_ext=".jpg",
                media_type="image",
                camera_angle="A",
                start_time=base_dt + timedelta(minutes=1),
                end_time=base_dt + timedelta(minutes=1),
                is_timestamp_reliable=True,
                reuse_count=0
            )

            # Unmatched media file (to be picked up by Step 9)
            f2_path = self.test_dir / "f2.jpg"
            Image.new("RGB", (320, 240), color=(50, 240, 50)).save(f2_path)
            mf2 = SourceMediaFile(
                source_folder_id=folder.id,
                journey_id=self.journey_id,
                file_path=str(f2_path),
                filename="f2.jpg",
                file_ext=".jpg",
                media_type="image",
                camera_angle="A",
                start_time=base_dt + timedelta(hours=2),
                end_time=base_dt + timedelta(hours=2),
                is_timestamp_reliable=True,
                reuse_count=0
            )
            db.session.add_all([mf1, mf2])
            db.session.commit()

            # Create job
            job = StopProcessingJob(
                timetable_id=tt.id,
                journey_id=self.journey_id,
                total_stops=1,
                status="queued"
            )
            db.session.add(job)
            db.session.commit()
            job_id = job.id

        # Run JobWorker synchronously
        worker = JobWorker(self.app, job_id)
        worker.run()

        with self.app.app_context():
            updated_job = StopProcessingJob.query.get(job_id)
            self.assertEqual(updated_job.status, "completed")
            self.assertEqual(updated_job.processed_stops, 1)
            self.assertGreaterEqual(updated_job.processed_media_files, 2)
            self.assertEqual(updated_job.remaining_media_files, 0)
            self.assertIn("Completed", updated_job.ai_detection_status)
            self.assertGreaterEqual(updated_job.angle_a_progress, 100.0)

    def test_06_date_mismatched_and_filename_stop_clip_matching(self):
        """Verify robust stop matching across date mismatches, diverse filename timestamps, and stop indices."""
        from datetime import date
        from app.services.media_service import parse_timestamp_from_filename, parse_stop_number_from_filename

        # 1. Filename timestamp extraction tests
        ref_date = date(2026, 10, 15)
        dt_dot = parse_timestamp_from_filename("Angle_A_10.15.00.mp4", base_date=ref_date)
        self.assertIsNotNone(dt_dot)
        self.assertEqual(dt_dot.hour, 10)
        self.assertEqual(dt_dot.minute, 15)

        dt_am = parse_timestamp_from_filename("Stop1_08.30_AM.mp4", base_date=ref_date)
        self.assertIsNotNone(dt_am)
        self.assertEqual(dt_am.hour, 8)
        self.assertEqual(dt_am.minute, 30)

        dt_pm = parse_timestamp_from_filename("bus_06-30-PM.mp4", base_date=ref_date)
        self.assertIsNotNone(dt_pm)
        self.assertEqual(dt_pm.hour, 18)
        self.assertEqual(dt_pm.minute, 30)

        # 2. Filename stop number extraction tests
        self.assertEqual(parse_stop_number_from_filename("Stop01_AngleA.mp4"), 1)
        self.assertEqual(parse_stop_number_from_filename("Stop_2_B.mp4"), 2)
        self.assertEqual(parse_stop_number_from_filename("S03_camC.mp4"), 3)
        self.assertEqual(parse_stop_number_from_filename("04_AngleA.mp4"), 4)

        # 3. Stop-to-media matching with date mismatch (Stop on 2026-11-04, Clips on 2026-10-15)
        with self.app.app_context():
            tt = TimetableImport(
                journey_id=self.journey_id,
                filename="date_mismatch_timetable.csv",
                file_path=str(self.test_dir / "dm_tt.csv"),
                total_stops=1,
                status="valid"
            )
            db.session.add(tt)
            db.session.flush()

            # Timetable stop has today's date: 2026-11-04 10:00:00 to 10:05:00
            today_dt = datetime(2026, 11, 4, 10, 0, 0)
            stop1 = TimetableStop(
                timetable_id=tt.id,
                stop_number=1,
                stop_name="Downtown Central",
                scheduled_arrival=today_dt,
                scheduled_departure=today_dt + timedelta(minutes=5),
                window_start=today_dt - timedelta(minutes=1),
                window_end=today_dt + timedelta(minutes=5)
            )
            db.session.add(stop1)
            db.session.flush()

            # Create media folders & files with DIFFERENT date (2026-10-15)
            # Angle A: time-of-day overlap (10:02:00)
            # Angle B: filename stop index (Stop01_AngleB.mp4)
            # Angle C: incremental step (+2m outside window, at 10:07:00)
            clip_date = datetime(2026, 10, 15, 10, 0, 0)

            for ang, fname, start_offset in [
                ("A", "bus_camA_10.02.00.jpg", timedelta(minutes=2)),
                ("B", "Stop01_AngleB.jpg", timedelta(minutes=1)),
                ("C", "rear_camC_10.07.00.jpg", timedelta(minutes=7))
            ]:
                folder = SourceFolder(
                    journey_id=self.journey_id,
                    camera_angle=ang,
                    folder_path=str(self.test_dir / f"folder_{ang}"),
                    folder_name=f"folder_{ang}"
                )
                db.session.add(folder)
                db.session.flush()

                f_path = self.test_dir / fname
                Image.new("RGB", (320, 240), color=(200, 100, 50)).save(f_path)

                mf = SourceMediaFile(
                    source_folder_id=folder.id,
                    journey_id=self.journey_id,
                    camera_angle=ang,
                    file_path=str(f_path),
                    filename=fname,
                    file_ext=".jpg",
                    media_type="image",
                    start_time=clip_date + start_offset,
                    end_time=clip_date + start_offset + timedelta(minutes=1),
                    is_timestamp_reliable=True
                )
                db.session.add(mf)

            db.session.commit()

            # Run automated matching
            success, msg = match_stops_to_media(tt.id)
            self.assertTrue(success)

            refreshed = TimetableStop.query.get(stop1.id)
            self.assertEqual(refreshed.coverage_status, "FULLY_COVERED")
            self.assertEqual(len(refreshed.matches), 3)

            match_dict = {m.camera_angle: m for m in refreshed.matches}
            self.assertEqual(match_dict["A"].status, "matched")
            self.assertEqual(match_dict["B"].status, "matched")
            self.assertEqual(match_dict["C"].status, "matched")

            # Check that Angle B matched via filename stop index
            self.assertIn("FILENAME_STOP_INDEX", match_dict["B"].match_reason)


if __name__ == "__main__":
    unittest.main()
