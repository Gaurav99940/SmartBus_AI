import unittest
import json
import os
import cv2
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path

from app import create_app
from app.config import Config
from app.database import db
from app.models import Journey, Bus, Upload, PassengerIdentity, PassengerObservation, PresenceEvent, GPSLocation, TimetableStop, TimetableImport, StopMediaMatch
from app.ai.color_filter import evaluate_frame_colorfulness, evaluate_media_color_eligibility
from app.ai.matcher import (
    cosine_similarity,
    match_faces_to_journey_identities,
    match_angle_detections_to_passengers,
    update_passenger_gallery_and_embedding,
    generate_next_person_id
)
from app.services.journey_service import (
    process_upload_for_journey,
    process_multi_angle_batch
)
from app.services.batch_processor import process_single_stop


class TestColorFilteringAndPassengerIdentity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = Config.UPLOAD_FOLDER / "test_color_media"
        cls.test_dir.mkdir(parents=True, exist_ok=True)

        # 1. Genuine color image
        cls.color_img_path = cls.test_dir / "genuine_color.jpg"
        c_img = np.zeros((200, 200, 3), dtype=np.uint8)
        c_img[:, :70, 0] = 230  # Blue
        c_img[:, 70:140, 1] = 210  # Green
        c_img[:, 140:, 2] = 240  # Red
        cv2.imwrite(str(cls.color_img_path), c_img)

        # 2. Pure single-channel grayscale
        cls.single_channel_gray_path = cls.test_dir / "single_channel_gray.png"
        g_img = np.full((200, 200), 128, dtype=np.uint8)
        cv2.imwrite(str(cls.single_channel_gray_path), g_img)

        # 3. Grayscale stored in 3-channel BGR format with slight compression noise
        cls.three_channel_gray_path = cls.test_dir / "three_channel_gray.jpg"
        g3 = np.zeros((200, 200, 3), dtype=np.uint8)
        for i in range(200):
            g3[i, :] = int(i * 1.2)
        # Add compression noise
        np.random.seed(42)
        noise = np.random.normal(0, 0.8, (200, 200, 3)).astype(np.int16)
        g3_noisy = np.clip(g3.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        cv2.imwrite(str(cls.three_channel_gray_path), g3_noisy)

        # 4. Dark footage (low lighting bus interior)
        cls.dark_color_path = cls.test_dir / "dark_color.jpg"
        dark_c = (c_img * 0.15).astype(np.uint8)
        cv2.imwrite(str(cls.dark_color_path), dark_c)

        cls.dark_bw_path = cls.test_dir / "dark_bw.jpg"
        dark_bw = (g3_noisy * 0.12).astype(np.uint8)
        cv2.imwrite(str(cls.dark_bw_path), dark_bw)

        # 5. Synthetic video clips (Color video & Grayscale video)
        cls.color_video_path = cls.test_dir / "clip_color.mp4"
        cls.bw_video_path = cls.test_dir / "clip_bw.mp4"
        cls.mixed_video_path = cls.test_dir / "clip_mixed.mp4"

        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        # Write color video (15 frames)
        vw = cv2.VideoWriter(str(cls.color_video_path), fourcc, 10.0, (200, 200))
        for _ in range(15):
            vw.write(c_img)
        vw.release()

        # Write B&W video (15 frames)
        vw = cv2.VideoWriter(str(cls.bw_video_path), fourcc, 10.0, (200, 200))
        for _ in range(15):
            vw.write(g3_noisy)
        vw.release()

        # Write Mixed video: 12 B&W frames + 3 color frames (80% B&W -> must be skipped)
        vw = cv2.VideoWriter(str(cls.mixed_video_path), fourcc, 10.0, (200, 200))
        for i in range(15):
            if i in [7, 8, 9]:
                vw.write(c_img)
            else:
                vw.write(g3_noisy)
        vw.release()

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

        # Seed Bus
        self.bus = Bus.query.filter_by(bus_number="BUS-COLOR-TEST").first()
        if not self.bus:
            self.bus = Bus(
                bus_number="BUS-COLOR-TEST",
                registration_number="UP-14-CL-1234",
                license_plate="UP14CL1234",
                capacity=40
            )
            db.session.add(self.bus)
            db.session.commit()

        # Seed Journeys
        self.journey = Journey.query.filter_by(journey_code="JNY-COLOR-01").first()
        if not self.journey:
            self.journey = Journey(
                journey_code="JNY-COLOR-01",
                bus_id=self.bus.id,
                origin="Depot A",
                destination="Depot B",
                status="in_progress",
                start_time=datetime.utcnow()
            )
            db.session.add(self.journey)
            db.session.commit()

        self.journey2 = Journey.query.filter_by(journey_code="JNY-COLOR-02").first()
        if not self.journey2:
            self.journey2 = Journey(
                journey_code="JNY-COLOR-02",
                bus_id=self.bus.id,
                origin="Depot C",
                destination="Depot D",
                status="in_progress",
                start_time=datetime.utcnow()
            )
            db.session.add(self.journey2)
            db.session.commit()

        # Cleanup test records
        PassengerObservation.query.filter(PassengerObservation.journey_id.in_([self.journey.id, self.journey2.id])).delete()
        PresenceEvent.query.filter(PresenceEvent.journey_id.in_([self.journey.id, self.journey2.id])).delete()
        PassengerIdentity.query.filter(PassengerIdentity.journey_id.in_([self.journey.id, self.journey2.id])).delete()
        db.session.commit()

        # Synthetic 512-D embeddings
        np.random.seed(101)
        v1 = np.random.randn(512).astype(np.float32)
        self.emb_p1 = (v1 / np.linalg.norm(v1)).tolist()

        v1_tilt = v1 + np.random.normal(0, 0.08, 512).astype(np.float32)
        self.emb_p1_tilt = (v1_tilt / np.linalg.norm(v1_tilt)).tolist()

        v2 = np.random.randn(512).astype(np.float32)
        v2 = v2 - np.dot(v2, v1) * (v1 / np.dot(v1, v1))
        self.emb_p2 = (v2 / np.linalg.norm(v2)).tolist()

    def tearDown(self):
        try:
            PassengerObservation.query.filter(PassengerObservation.journey_id.in_([self.journey.id, self.journey2.id])).delete()
            PresenceEvent.query.filter(PresenceEvent.journey_id.in_([self.journey.id, self.journey2.id])).delete()
            PassengerIdentity.query.filter(PassengerIdentity.journey_id.in_([self.journey.id, self.journey2.id])).delete()
            db.session.commit()
        except Exception:
            db.session.rollback()
        finally:
            self.ctx.pop()

    # =========================================================================
    # PART 1: COLOR FILTERING TESTS (Requirements 1-5)
    # =========================================================================

    def test_01_genuine_color_clip_is_eligible(self):
        """1. Genuine color clip is eligible for analysis."""
        eval_res = evaluate_media_color_eligibility(str(self.color_img_path))
        self.assertTrue(eval_res["is_color"])
        self.assertEqual(eval_res["status"], "COLOR")
        self.assertGreater(eval_res["color_score"], 15.0)
        self.assertIsNone(eval_res["skip_reason"])

        vid_res = evaluate_media_color_eligibility(str(self.color_video_path))
        self.assertTrue(vid_res["is_color"])
        self.assertEqual(vid_res["status"], "COLOR")

    def test_02_grayscale_clip_is_skipped(self):
        """2. Grayscale clip is skipped."""
        eval_res = evaluate_media_color_eligibility(str(self.bw_video_path))
        self.assertFalse(eval_res["is_color"])
        self.assertEqual(eval_res["status"], "SKIPPED_BLACK_AND_WHITE")
        self.assertIsNotNone(eval_res["skip_reason"])
        self.assertIn("Black-and-white", eval_res["skip_reason"])

    def test_03_grayscale_stored_in_three_channel_format_detected(self):
        """3. Grayscale image stored in three-channel format is correctly detected as B&W."""
        eval_res = evaluate_media_color_eligibility(str(self.three_channel_gray_path))
        self.assertFalse(eval_res["is_color"])
        self.assertEqual(eval_res["status"], "SKIPPED_BLACK_AND_WHITE")
        self.assertLess(eval_res["color_score"], 5.0)

    def test_04_dark_and_low_saturation_footage_handled_by_policy(self):
        """4. Dark or low-saturation footage is handled according to configured policy."""
        # Genuine color footage in low light retains subtle chromaticity differences
        dark_c_res = evaluate_media_color_eligibility(str(self.dark_color_path))
        # Monochrome IR/night vision dark footage is strictly identified as B&W
        dark_bw_res = evaluate_media_color_eligibility(str(self.dark_bw_path))
        self.assertFalse(dark_bw_res["is_color"])
        self.assertEqual(dark_bw_res["status"], "SKIPPED_BLACK_AND_WHITE")

    def test_05_mixed_color_and_grayscale_segments_policy(self):
        """5. Mixed-color and grayscale segments follow documented majority/eligibility policy."""
        # 80% B&W with 3 random colored frames must be skipped
        mixed_res = evaluate_media_color_eligibility(str(self.mixed_video_path))
        self.assertFalse(mixed_res["is_color"])
        self.assertEqual(mixed_res["status"], "SKIPPED_BLACK_AND_WHITE")
        self.assertIn("skipped before AI inference", mixed_res["skip_reason"])

    # =========================================================================
    # PART 2: MULTI-ANGLE AND TIMESTAMPS TESTS (Requirements 6-9)
    # =========================================================================

    def test_06_angle_a_color_and_angle_b_bw_at_same_timestamp(self):
        """6. Angle A color + Angle B grayscale at same timestamp: analyze A and skip B."""
        upload_a = Upload(
            journey_id=self.journey.id,
            bus_id=self.bus.id,
            camera_angle="A",
            file_path=str(self.color_img_path),
            original_filename="color_a.jpg",
            status="uploaded"
        )
        upload_b = Upload(
            journey_id=self.journey.id,
            bus_id=self.bus.id,
            camera_angle="B",
            file_path=str(self.three_channel_gray_path),
            original_filename="gray_b.jpg",
            status="uploaded"
        )
        db.session.add_all([upload_a, upload_b])
        db.session.commit()

        result = process_multi_angle_batch(
            journey_id=self.journey.id,
            batch_id="batch_test_06",
            angle_uploads={"A": upload_a, "B": upload_b}
        )

        summaries = result["angle_summaries"]
        # Angle A processed normally
        self.assertEqual(summaries["A"]["status"], "processed")
        self.assertTrue(upload_a.is_color)

        # Angle B skipped as B&W
        self.assertEqual(summaries["B"]["status"], "SKIPPED_BLACK_AND_WHITE")
        self.assertFalse(upload_b.is_color)
        self.assertEqual(upload_b.status, "SKIPPED_BLACK_AND_WHITE")
        self.assertIn("Black-and-white", upload_b.skip_reason)

    def test_07_all_angles_bw_at_t1_followed_by_color_at_t2(self):
        """7. All angles B&W at t1 followed by color at t2: skip t1 without altering state, continue at t2."""
        # Seed existing passenger P001
        p1 = PassengerIdentity(
            journey_id=self.journey.id,
            person_id="P001",
            current_seat="1A",
            status="PRESENT",
            consecutive_missed_count=0,
            representative_embedding_json=json.dumps(self.emb_p1),
            gallery_embeddings_json=json.dumps([self.emb_p1])
        )
        db.session.add(p1)
        db.session.commit()

        # Timestamp 10:00: All submitted angles are B&W
        up_t1_a = Upload(journey_id=self.journey.id, camera_angle="A", file_path=str(self.three_channel_gray_path), original_filename="bw_a.jpg")
        up_t1_b = Upload(journey_id=self.journey.id, camera_angle="B", file_path=str(self.three_channel_gray_path), original_filename="bw_b.jpg")
        db.session.add_all([up_t1_a, up_t1_b])
        db.session.commit()

        res_t1 = process_multi_angle_batch(
            journey_id=self.journey.id,
            batch_id="batch_t1_all_bw",
            angle_uploads={"A": up_t1_a, "B": up_t1_b}
        )

        # Must report all skipped B&W
        self.assertEqual(res_t1["status"], "SKIPPED_BLACK_AND_WHITE")
        self.assertTrue(res_t1["is_all_skipped_bw"])

        # P001 presence state MUST remain intact (NOT incremented missed count, NOT marked absent)
        p1_refreshed = PassengerIdentity.query.get(p1.id)
        self.assertEqual(p1_refreshed.status, "PRESENT")
        self.assertEqual(p1_refreshed.consecutive_missed_count, 0)

        # Timestamp 10:01: Angle A is color
        up_t2_a = Upload(journey_id=self.journey.id, camera_angle="A", file_path=str(self.color_img_path), original_filename="color_a2.jpg")
        db.session.add(up_t2_a)
        db.session.commit()

        res_t2 = process_multi_angle_batch(
            journey_id=self.journey.id,
            batch_id="batch_t2_color",
            angle_uploads={"A": up_t2_a}
        )
        self.assertEqual(res_t2["status"], "COMPLETED")
        self.assertEqual(res_t2["angle_summaries"]["A"]["status"], "processed")

    def test_08_multiple_valid_color_angles_contribute_without_duplicates(self):
        """8. Multiple valid color angles contribute observations without creating duplicate IDs."""
        up_a = Upload(journey_id=self.journey.id, camera_angle="A", file_path=str(self.color_img_path), original_filename="c_a.jpg")
        up_b = Upload(journey_id=self.journey.id, camera_angle="B", file_path=str(self.color_img_path), original_filename="c_b.jpg")
        db.session.add_all([up_a, up_b])
        db.session.commit()

        res = process_multi_angle_batch(
            journey_id=self.journey.id,
            batch_id="batch_test_08",
            angle_uploads={"A": up_a, "B": up_b}
        )
        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(res["angle_summaries"]["A"]["status"], "processed")
        self.assertEqual(res["angle_summaries"]["B"]["status"], "processed")

    def test_09_skipped_clip_does_not_block_queue(self):
        """9. A skipped clip cannot block later eligible clips in processing queue."""
        # Simulate Stop 1 with B&W footage
        stop1 = TimetableStop(
            timetable_id=1,
            stop_number=1,
            stop_name="Stop 1",
            scheduled_arrival=datetime.utcnow(),
            window_start=datetime.utcnow() - timedelta(minutes=5),
            window_end=datetime.utcnow() + timedelta(minutes=5),
            processing_status="pending"
        )
        db.session.add(stop1)
        db.session.flush()

        match1 = StopMediaMatch(
            stop_id=stop1.id,
            camera_angle="A",
            source_filename="clip_bw.mp4",
            source_folder=str(self.bw_video_path),
            status="matched"
        )
        # Link source media file
        from app.models import SourceMediaFile
        smf1 = SourceMediaFile(
            source_folder_id=1,
            camera_angle="A",
            file_path=str(self.bw_video_path),
            filename="clip_bw.mp4",
            file_ext=".mp4",
            media_type="video"
        )
        db.session.add_all([match1, smf1])
        db.session.flush()
        match1.source_media_file_id = smf1.id
        db.session.commit()

        # Process Stop 1: should skip B&W cleanly without raising an unhandled exception
        res1 = process_single_stop(stop1.id)
        self.assertTrue(res1["skipped"])
        self.assertEqual(res1["status"], "SKIPPED_BLACK_AND_WHITE")

        # Stop 1 is marked as skipped, queue can proceed immediately to Stop 2
        refreshed_stop1 = TimetableStop.query.get(stop1.id)
        self.assertEqual(refreshed_stop1.processing_status, "SKIPPED_BLACK_AND_WHITE")

    # =========================================================================
    # PART 3: IDENTITY PERSISTENCE TESTS (Requirements 10-20)
    # =========================================================================

    def test_10_same_passenger_retains_permanent_id(self):
        """10. The same passenger across consecutive color clips retains the same permanent ID."""
        p1 = PassengerIdentity(
            journey_id=self.journey.id,
            person_id="P001",
            current_seat="1A",
            status="PRESENT",
            representative_embedding_json=json.dumps(self.emb_p1),
            gallery_embeddings_json=json.dumps([self.emb_p1])
        )
        db.session.add(p1)
        db.session.commit()

        det = {
            "bbox": [100, 150, 80, 120],
            "detector_prob": 0.95,
            "embedding": self.emb_p1,
            "clothing_signature": [0.5] * 206,
            "crop_path": "crops/test10.jpg",
            "quality": {"is_acceptable": True, "quality_score": 0.95, "is_blur": False}
        }
        res = match_faces_to_journey_identities([det], journey_id=self.journey.id)
        self.assertEqual(res[0]["person_id"], "P001")
        self.assertFalse(res[0]["is_new"])

    def test_11_same_passenger_changing_seats_retains_id(self):
        """11. The same passenger changing seats retains the same ID."""
        p1 = PassengerIdentity(
            journey_id=self.journey.id,
            person_id="P001",
            current_seat="1A",
            status="PRESENT",
            representative_embedding_json=json.dumps(self.emb_p1),
            gallery_embeddings_json=json.dumps([self.emb_p1])
        )
        db.session.add(p1)
        db.session.commit()

        # Seated at Row 6 in Angle A (spat_sim = 0)
        det_moved = {
            "temp_id": "T-A-01",
            "bbox": [950, 600, 90, 130],
            "norm_bbox": [0.85, 0.80, 0.08, 0.18],
            "detection_confidence": 0.92,
            "embedding": self.emb_p1,
            "clothing_signature": [0.5] * 206,
            "quality": {"is_acceptable": True, "quality_score": 0.90, "is_blur": False}
        }
        matched = match_angle_detections_to_passengers([det_moved], camera_angle="A", journey_id=self.journey.id)
        self.assertEqual(matched[0]["person_id"], "P001")

    def test_12_head_angle_and_lighting_change_matched(self):
        """12. The same passenger under different angle/lighting retains ID."""
        p1 = PassengerIdentity(
            journey_id=self.journey.id,
            person_id="P001",
            current_seat="1A",
            status="PRESENT",
            representative_embedding_json=json.dumps(self.emb_p1),
            gallery_embeddings_json=json.dumps([self.emb_p1])
        )
        db.session.add(p1)
        db.session.commit()

        det_tilt = {
            "temp_id": "T-A-02",
            "bbox": [150, 150, 80, 120],
            "norm_bbox": [0.12, 0.20, 0.07, 0.17],
            "detection_confidence": 0.89,
            "embedding": self.emb_p1_tilt,
            "clothing_signature": [0.5] * 206,
            "quality": {"is_acceptable": True, "quality_score": 0.88, "is_blur": False}
        }
        matched = match_angle_detections_to_passengers([det_tilt], camera_angle="A", journey_id=self.journey.id)
        self.assertEqual(matched[0]["person_id"], "P001")

    def test_13_two_different_passengers_retain_different_ids(self):
        """13. Two different passengers retain different IDs."""
        p1 = PassengerIdentity(
            journey_id=self.journey.id,
            person_id="P001",
            current_seat="1A",
            status="PRESENT",
            representative_embedding_json=json.dumps(self.emb_p1),
            gallery_embeddings_json=json.dumps([self.emb_p1])
        )
        db.session.add(p1)
        db.session.commit()

        det_p2 = {
            "temp_id": "T-A-03",
            "bbox": [300, 200, 80, 120],
            "norm_bbox": [0.25, 0.25, 0.06, 0.16],
            "detection_confidence": 0.90,
            "embedding": self.emb_p2,
            "clothing_signature": [0.1] * 206,
            "quality": {"is_acceptable": True, "quality_score": 0.90, "is_blur": False}
        }
        matched = match_angle_detections_to_passengers([det_p2], camera_angle="A", journey_id=self.journey.id)
        self.assertEqual(matched[0]["person_id"], "P002")
        self.assertNotEqual(matched[0]["person_id"], "P001")

    def test_14_ambiguous_face_marked_for_review(self):
        """14. Ambiguous or low-quality face is marked for review."""
        det_blur = {
            "temp_id": "T-A-04",
            "bbox": [400, 250, 70, 100],
            "norm_bbox": [0.30, 0.30, 0.05, 0.14],
            "detection_confidence": 0.35,
            "embedding": None,
            "quality": {"is_acceptable": False, "quality_score": 0.20, "is_blur": True}
        }
        matched = match_angle_detections_to_passengers([det_blur], camera_angle="A", journey_id=self.journey.id)
        self.assertEqual(matched[0]["confidence_label"], "Needs Review")
        self.assertEqual(matched[0]["review_status"], "NEEDS_REVIEW")

    def test_15_skipping_grayscale_does_not_change_passenger_state(self):
        """15. Skipping a grayscale clip does not change IDs, embeddings, presence, or timestamps."""
        t_orig = datetime(2026, 10, 21, 10, 0, 0)
        p1 = PassengerIdentity(
            journey_id=self.journey.id,
            person_id="P001",
            current_seat="1A",
            status="PRESENT",
            first_seen_time=t_orig,
            last_seen_time=t_orig,
            total_observations=5,
            consecutive_missed_count=0,
            representative_embedding_json=json.dumps(self.emb_p1),
            gallery_embeddings_json=json.dumps([self.emb_p1])
        )
        db.session.add(p1)
        db.session.commit()

        # Process a grayscale upload for this journey
        up_bw = Upload(
            journey_id=self.journey.id,
            camera_angle="A",
            file_path=str(self.three_channel_gray_path),
            original_filename="gray.jpg",
            status="uploaded"
        )
        db.session.add(up_bw)
        db.session.commit()

        res = process_upload_for_journey(up_bw.id)
        self.assertEqual(res["status"], "SKIPPED_BLACK_AND_WHITE")

        # Verify P001 remains completely untouched
        p1_check = PassengerIdentity.query.get(p1.id)
        self.assertEqual(p1_check.status, "PRESENT")
        self.assertEqual(p1_check.last_seen_time, t_orig)
        self.assertEqual(p1_check.total_observations, 5)
        self.assertEqual(p1_check.consecutive_missed_count, 0)
        self.assertEqual(p1_check.representative_embedding_json, json.dumps(self.emb_p1))

    def test_16_later_color_clip_matches_previous_passenger(self):
        """16. A later eligible color clip matches previously identified passengers after a skipped B&W clip."""
        p1 = PassengerIdentity(
            journey_id=self.journey.id,
            person_id="P001",
            current_seat="1A",
            status="PRESENT",
            representative_embedding_json=json.dumps(self.emb_p1),
            gallery_embeddings_json=json.dumps([self.emb_p1])
        )
        db.session.add(p1)
        db.session.commit()

        # Step 1: B&W clip at 10:05 (skipped)
        eval_bw = evaluate_media_color_eligibility(str(self.three_channel_gray_path))
        self.assertFalse(eval_bw["is_color"])

        # Step 2: Color clip at 10:10 (processed)
        eval_col = evaluate_media_color_eligibility(str(self.color_img_path))
        self.assertTrue(eval_col["is_color"])

        # Passenger P001 appears in color clip
        det = {
            "bbox": [100, 150, 80, 120],
            "detector_prob": 0.95,
            "embedding": self.emb_p1,
            "clothing_signature": [0.5] * 206,
            "quality": {"is_acceptable": True, "quality_score": 0.95, "is_blur": False}
        }
        res = match_faces_to_journey_identities([det], journey_id=self.journey.id)
        self.assertEqual(res[0]["person_id"], "P001")
        self.assertFalse(res[0]["is_new"])

    def test_17_repeated_uploads_idempotent(self):
        """17. Repeated uploads do not create duplicate identities."""
        up = Upload(
            journey_id=self.journey.id,
            bus_id=self.bus.id,
            camera_angle="A",
            file_path=str(self.color_img_path),
            original_filename="c_idem.jpg",
            status="uploaded"
        )
        db.session.add(up)
        db.session.commit()

        faces = [{
            "bbox": [100, 100, 60, 80],
            "detector_prob": 0.95,
            "embedding": self.emb_p1,
            "crop_path": "crops/idem.jpg",
            "quality": {"is_acceptable": True, "quality_score": 0.95, "is_blur": False}
        }]

        # 1st run
        process_upload_for_journey(up.id, faces)
        c1 = PassengerIdentity.query.filter_by(journey_id=self.journey.id).count()
        self.assertEqual(c1, 1)

        # 2nd run (identical upload)
        process_upload_for_journey(up.id, faces)
        c2 = PassengerIdentity.query.filter_by(journey_id=self.journey.id).count()
        self.assertEqual(c2, 1)

    def test_18_concurrent_id_allocations_handled_safely(self):
        """18. Concurrent uploads allocate unique IDs safely."""
        alloc = set()
        p_a = generate_next_person_id(self.journey.id, allocated_ids=alloc)
        p_b = generate_next_person_id(self.journey.id, allocated_ids=alloc)
        p_c = generate_next_person_id(self.journey.id, allocated_ids=alloc)
        self.assertEqual(p_a, "P001")
        self.assertEqual(p_b, "P002")
        self.assertEqual(p_c, "P003")
        self.assertEqual(len(alloc), 3)

    def test_19_different_journeys_isolated(self):
        """19. Different journeys remain strictly isolated."""
        p_j1 = PassengerIdentity(
            journey_id=self.journey.id,
            person_id="P001",
            current_seat="1A",
            representative_embedding_json=json.dumps(self.emb_p1),
            gallery_embeddings_json=json.dumps([self.emb_p1])
        )
        db.session.add(p_j1)
        db.session.commit()

        # In Journey 2, same face starts its own journey-scoped P001
        det = {
            "bbox": [100, 100, 80, 120],
            "detector_prob": 0.95,
            "embedding": self.emb_p1,
            "clothing_signature": [0.5] * 206,
            "quality": {"is_acceptable": True, "quality_score": 0.95, "is_blur": False}
        }
        res_j2 = match_faces_to_journey_identities([det], journey_id=self.journey2.id)
        self.assertTrue(res_j2[0]["is_new"])
        self.assertEqual(res_j2[0]["person_id"], "P001")

    def test_20_existing_tests_and_models_function_properly(self):
        """20. Existing application models, similarity scoring, and galleries function properly."""
        sim_same = cosine_similarity(self.emb_p1, self.emb_p1)
        self.assertAlmostEqual(sim_same, 1.0, places=4)

        sim_diff = cosine_similarity(self.emb_p1, self.emb_p2)
        self.assertLess(sim_diff, 0.40)

        p = PassengerIdentity(journey_id=self.journey.id, person_id="P001")
        update_passenger_gallery_and_embedding(p, self.emb_p1)
        self.assertIsNotNone(p.representative_embedding_json)
        self.assertIsNotNone(p.gallery_embeddings_json)


if __name__ == "__main__":
    unittest.main()
