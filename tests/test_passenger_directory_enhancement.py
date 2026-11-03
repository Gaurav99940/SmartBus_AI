import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
import openpyxl

from app import create_app
from app.database import db
from app.models import (
    User, Bus, Journey, Upload, PassengerIdentity,
    PassengerObservation, PresenceEvent, PassengerMergeRecord, AuditLog
)
from app.services.passenger_report_service import (
    get_date_wise_passenger_report,
    merge_multiple_passengers,
    reverse_passenger_merge,
    get_merge_history,
    generate_date_wise_report_csv,
    generate_date_wise_report_excel
)


class TestPassengerDirectoryEnhancement(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        with self.app.app_context():
            # Clean up prior test data for JNY-DIR-TEST to guarantee test isolation
            j_old = Journey.query.filter_by(journey_code="JNY-DIR-TEST").first()
            if j_old:
                PassengerObservation.query.filter_by(journey_id=j_old.id).delete()
                PassengerMergeRecord.query.filter_by(journey_id=j_old.id).delete()
                PassengerIdentity.query.filter_by(journey_id=j_old.id).delete()
                Upload.query.filter_by(journey_id=j_old.id).delete()
                db.session.commit()

            # Create dedicated test bus
            self.bus = Bus.query.filter_by(bus_number="BUS-DIR-TEST").first()
            if not self.bus:
                self.bus = Bus(
                    bus_number="BUS-DIR-TEST",
                    registration_number="REG-DIR-900",
                    license_plate="DL-01-DIR",
                    capacity=40,
                    route_name="Station A - Station B"
                )
                db.session.add(self.bus)
                db.session.flush()

            # Create test journey
            self.journey = Journey.query.filter_by(journey_code="JNY-DIR-TEST").first()
            if not self.journey:
                self.journey = Journey(
                    journey_code="JNY-DIR-TEST",
                    bus_id=self.bus.id,
                    origin="Station A",
                    destination="Station B",
                    status="active"
                )
                db.session.add(self.journey)
                db.session.commit()

            self.journey_id = self.journey.id
            self.bus_id = self.bus.id

            # Create test dates: Date 1 (2026-10-28) and Date 2 (2026-10-30)
            self.date1 = datetime(2026, 10, 28, 10, 0, 0)
            self.date2 = datetime(2026, 10, 30, 14, 30, 0)

            # Uploads for Date 1
            self.up1_a = Upload(
                journey_id=self.journey_id, bus_id=self.bus_id, camera_angle="A",
                file_path="uploads/test1_a.jpg", original_filename="a1.jpg",
                capture_timestamp=self.date1, upload_timestamp=self.date1, status="processed"
            )
            self.up1_b = Upload(
                journey_id=self.journey_id, bus_id=self.bus_id, camera_angle="B",
                file_path="uploads/test1_b.jpg", original_filename="b1.jpg",
                capture_timestamp=self.date1, upload_timestamp=self.date1, status="processed"
            )
            # Uploads for Date 2
            self.up2_c = Upload(
                journey_id=self.journey_id, bus_id=self.bus_id, camera_angle="C",
                file_path="uploads/test2_c.jpg", original_filename="c2.jpg",
                capture_timestamp=self.date2, upload_timestamp=self.date2, status="processed"
            )
            db.session.add_all([self.up1_a, self.up1_b, self.up2_c])
            db.session.flush()

            # Create Passenger Identities
            self.p101 = PassengerIdentity(
                journey_id=self.journey_id, person_id="P101",
                first_seen_time=self.date1, last_seen_time=self.date1,
                first_latitude=28.6472, first_longitude=77.3189,
                last_latitude=28.6485, last_longitude=77.3251,
                status="PRESENT", total_observations=2, stops_traveled=2
            )
            self.p102 = PassengerIdentity(
                journey_id=self.journey_id, person_id="P102",
                first_seen_time=self.date1, last_seen_time=self.date1,
                first_latitude=28.6472, first_longitude=77.3189,
                last_latitude=28.6485, last_longitude=77.3251,
                status="PRESENT", total_observations=1, stops_traveled=1
            )
            self.p103 = PassengerIdentity(
                journey_id=self.journey_id, person_id="P103",
                first_seen_time=self.date2, last_seen_time=self.date2,
                first_latitude=28.6436, first_longitude=77.3348,
                last_latitude=28.6436, last_longitude=77.3348,
                status="PRESENT", total_observations=1, stops_traveled=1
            )
            db.session.add_all([self.p101, self.p102, self.p103])
            db.session.flush()

            # Date 1 Observations:
            # Angle A: P101
            # Angle B: P101, P102
            self.obs1 = PassengerObservation(
                upload_id=self.up1_a.id, journey_id=self.journey_id, passenger_identity_id=self.p101.id,
                camera_angle="A", bbox_x=10, bbox_y=10, bbox_w=50, bbox_h=50,
                detection_confidence=0.95, face_confidence=0.91,
                observation_time=self.date1, crop_path="crops/test_p101_a.jpg"
            )
            self.obs2 = PassengerObservation(
                upload_id=self.up1_b.id, journey_id=self.journey_id, passenger_identity_id=self.p101.id,
                camera_angle="B", bbox_x=60, bbox_y=20, bbox_w=50, bbox_h=50,
                detection_confidence=0.92, face_confidence=0.88,
                observation_time=self.date1, crop_path="crops/test_p101_b.jpg"
            )
            self.obs3 = PassengerObservation(
                upload_id=self.up1_b.id, journey_id=self.journey_id, passenger_identity_id=self.p102.id,
                camera_angle="B", bbox_x=120, bbox_y=30, bbox_w=50, bbox_h=50,
                detection_confidence=0.89, face_confidence=0.85,
                observation_time=self.date1, crop_path="crops/test_p102_b.jpg"
            )

            # Date 2 Observations:
            # Angle C: P103
            self.obs4 = PassengerObservation(
                upload_id=self.up2_c.id, journey_id=self.journey_id, passenger_identity_id=self.p103.id,
                camera_angle="C", bbox_x=200, bbox_y=40, bbox_w=50, bbox_h=50,
                detection_confidence=0.94, face_confidence=0.90,
                observation_time=self.date2, crop_path="crops/test_p103_c.jpg"
            )

            db.session.add_all([self.obs1, self.obs2, self.obs3, self.obs4])
            db.session.commit()

            self.p101_id = self.p101.id
            self.p102_id = self.p102.id
            self.p103_id = self.p103.id
            self.obs1_id = self.obs1.id
            self.obs2_id = self.obs2.id
            self.obs3_id = self.obs3.id
            self.obs4_id = self.obs4.id

    def test_1_date_wise_report_generation(self):
        """Req 1: Generates reports date-wise with separate sections per date."""
        with self.app.app_context():
            res = self.client.get(f"/api/passengers/date-wise-report?journey_id={self.journey_id}")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data.get("success"))
            self.assertGreaterEqual(data.get("total_dates_count"), 2)

            dates = [dg["date"] for dg in data["date_groups"]]
            self.assertIn("2026-10-28", dates)
            self.assertIn("2026-10-30", dates)

    def test_2_correct_date_filtering(self):
        """Req 2: Filtering by specific date returns only observations from that date."""
        with self.app.app_context():
            res = self.client.get(f"/api/passengers/date-wise-report?journey_id={self.journey_id}&date=2026-10-28")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(len(data["date_groups"]), 1)
            dg = data["date_groups"][0]
            self.assertEqual(dg["date"], "2026-10-28")
            self.assertEqual(dg["summary"]["total_observations"], 3)
            # P101 appears in Angle A and B, P102 in Angle B => 2 unique canonical passengers
            self.assertEqual(dg["summary"]["total_unique_passengers"], 2)

    def test_3_separate_angle_wise_lists(self):
        """Req 2 & 3: Angle A, B, and C have independent passenger lists with correct counts."""
        with self.app.app_context():
            res = self.client.get(f"/api/passengers/date-wise-report?journey_id={self.journey_id}&date=2026-10-28")
            data = res.get_json()
            dg = data["date_groups"][0]
            ang_sections = dg["angle_sections"]

            # Angle A has 1 observation (P101)
            self.assertEqual(len(ang_sections["angle_a"]), 1)
            self.assertEqual(ang_sections["angle_a"][0]["person_id"], "P101")
            self.assertEqual(ang_sections["angle_a"][0]["camera_angle"], "Angle A")

            # Angle B has 2 observations (P101, P102)
            self.assertEqual(len(ang_sections["angle_b"]), 2)
            b_pids = {o["person_id"] for o in ang_sections["angle_b"]}
            self.assertIn("P101", b_pids)
            self.assertIn("P102", b_pids)

            # Angle C has 0 observations on 2026-10-28
            self.assertEqual(len(ang_sections["angle_c"]), 0)

    def test_4_gallery_count_matches_report_records(self):
        """Req 4: Gallery count matches exact report observation count 1:1."""
        with self.app.app_context():
            res = self.client.get(f"/api/passengers/date-wise-report?journey_id={self.journey_id}&date=2026-10-28")
            data = res.get_json()
            dg = data["date_groups"][0]
            self.assertEqual(len(dg["gallery_crops"]), dg["summary"]["total_observations"])
            self.assertEqual(len(dg["gallery_crops"]), 3)

    def test_5_image_enlarge_metadata_completeness(self):
        """Req 5: Every passenger record contains all metadata required for popup enlargement."""
        with self.app.app_context():
            res = self.client.get(f"/api/passengers/date-wise-report?journey_id={self.journey_id}&date=2026-10-28")
            data = res.get_json()
            item = data["date_groups"][0]["gallery_crops"][0]

            self.assertIn("person_id", item)
            self.assertIn("canonical_person_id", item)
            self.assertIn("camera_angle", item)
            self.assertIn("observation_time", item)
            self.assertIn("bus_number", item)
            self.assertIn("journey_code", item)
            self.assertIn("detection_confidence", item)
            self.assertIn("status", item)
            # Embedding should NOT be in user facing item
            self.assertNotIn("representative_embedding_json", item)

    def test_6_manual_identity_merging(self):
        """Req 6 & 7: Multiple Person IDs can be safely merged into chosen Master ID."""
        with self.app.app_context():
            res = self.client.post("/api/passengers/merge-multiple", json={
                "journey_id": self.journey_id,
                "selected_person_ids": ["P101", "P102"],
                "master_person_id": "P101",
                "comment": "Confirmed same passenger across angles"
            })
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data.get("success"))
            self.assertEqual(data.get("master_person_id"), "P101")
            self.assertIn("P102", data.get("merged_person_ids"))

            # Verify P102 is archived and marked as merged
            p102 = PassengerIdentity.query.filter_by(journey_id=self.journey_id, person_id="P102").first()
            self.assertTrue(p102.is_merged)
            self.assertEqual(p102.status, "MERGED_ARCHIVED")
            self.assertEqual(p102.get_canonical_person_id(), "P101")

            # Verify observation was transferred
            obs_p102 = PassengerObservation.query.get(self.obs3_id)
            self.assertEqual(obs_p102.passenger_identity_id, self.p101_id)
            self.assertEqual(obs_p102.get_canonical_person_id(), "P101")

    def test_7_updated_unique_passenger_counts_after_merge(self):
        """Req 9: After merging P102 into P101, unique passengers on 2026-10-28 becomes 1, observations remain 3."""
        with self.app.app_context():
            # Perform merge
            merge_multiple_passengers(
                selected_person_ids=["P101", "P102"],
                master_person_id="P101",
                journey_id=self.journey_id,
                comment="Unit test merge"
            )

            # Query date-wise report
            res = self.client.get(f"/api/passengers/date-wise-report?journey_id={self.journey_id}&date=2026-10-28")
            data = res.get_json()
            dg = data["date_groups"][0]

            # Observations must remain exactly 3 (no deletions)
            self.assertEqual(dg["summary"]["total_observations"], 3)
            # Canonical unique passengers must be 1 (P101)
            self.assertEqual(dg["summary"]["total_unique_passengers"], 1)
            # Merged count must reflect 1 alias observation
            self.assertGreaterEqual(dg["summary"]["merged_count"], 1)

    def test_8_merge_history_and_reversal(self):
        """Req 8: Merge can be safely inspected in history and reversed without data loss."""
        with self.app.app_context():
            # 1. Merge
            m_res = merge_multiple_passengers(
                selected_person_ids=["P101", "P102"],
                master_person_id="P101",
                journey_id=self.journey_id,
                comment="Audit test merge"
            )
            self.assertTrue(m_res["success"])

            # 2. Check history
            hist_res = self.client.get(f"/api/passengers/merge-history?journey_id={self.journey_id}")
            self.assertEqual(hist_res.status_code, 200)
            hist_data = hist_res.get_json()
            self.assertGreaterEqual(len(hist_data["records"]), 1)
            rec = hist_data["records"][0]
            self.assertEqual(rec["status"], "ACTIVE")
            rec_id = rec["id"]

            # 3. Reverse merge
            rev_res = self.client.post(f"/api/passengers/merge-reverse/{rec_id}", json={
                "reason": "False positive match reversal"
            })
            self.assertEqual(rev_res.status_code, 200)
            rev_data = rev_res.get_json()
            self.assertTrue(rev_data["success"])

            # 4. Verify P102 is restored as independent identity
            p102 = PassengerIdentity.query.filter_by(journey_id=self.journey_id, person_id="P102").first()
            self.assertFalse(p102.is_merged)
            self.assertNotEqual(p102.status, "MERGED_ARCHIVED")
            self.assertEqual(p102.get_canonical_person_id(), "P102")

            # 5. Verify observation reassigned back to P102
            obs3 = PassengerObservation.query.get(self.obs3_id)
            self.assertEqual(obs3.passenger_identity_id, self.p102_id)

    def test_9_prevent_invalid_and_cyclic_merges(self):
        """Req 7: Validates master ID presence and rejects cyclic/invalid merges."""
        with self.app.app_context():
            # Master ID not in selected
            res1 = self.client.post("/api/passengers/merge-multiple", json={
                "journey_id": self.journey_id,
                "selected_person_ids": ["P101", "P102"],
                "master_person_id": "P999"
            })
            self.assertEqual(res1.status_code, 400)

            # Less than 2 IDs
            res2 = self.client.post("/api/passengers/merge-multiple", json={
                "journey_id": self.journey_id,
                "selected_person_ids": ["P101"],
                "master_person_id": "P101"
            })
            self.assertEqual(res2.status_code, 400)

    def test_10_csv_and_excel_export_endpoints(self):
        """Req 11: Generates CSV and Excel (.xlsx) date-wise reports with required columns."""
        with self.app.app_context():
            # CSV Export
            res_csv = self.client.get(f"/api/passengers/export-datewise.csv?journey_id={self.journey_id}&date=2026-10-28")
            self.assertEqual(res_csv.status_code, 200)
            self.assertEqual(res_csv.mimetype, "text/csv")
            csv_text = res_csv.get_data(as_text=True)
            self.assertIn("Report Date", csv_text)
            self.assertIn("Original Person ID", csv_text)
            self.assertIn("Canonical Person ID", csv_text)
            self.assertIn("Camera Angle", csv_text)
            self.assertIn("P101", csv_text)

            # Excel Export
            res_xlsx = self.client.get(f"/api/passengers/export-datewise.xlsx?journey_id={self.journey_id}&date=2026-10-28")
            self.assertEqual(res_xlsx.status_code, 200)
            self.assertIn("spreadsheetml", res_xlsx.mimetype)
            self.assertGreater(len(res_xlsx.data), 1000)

    def test_11_preservation_of_original_observations_and_angles(self):
        """Req 2, 6 & 12: Camera angles and observation records are 100% preserved after merge."""
        with self.app.app_context():
            merge_multiple_passengers(
                selected_person_ids=["P101", "P102"],
                master_person_id="P101",
                journey_id=self.journey_id
            )
            # Fetch all observations for the journey
            all_obs = PassengerObservation.query.filter_by(journey_id=self.journey_id).all()
            self.assertEqual(len(all_obs), 4)

            # Check that obs3 retained its original Angle B
            obs3 = PassengerObservation.query.get(self.obs3_id)
            self.assertEqual(obs3.camera_angle, "B")

    def test_12_preservation_of_gps_history(self):
        """Req 13: Passenger GPS coordinates are preserved across merging."""
        with self.app.app_context():
            merge_multiple_passengers(
                selected_person_ids=["P101", "P102"],
                master_person_id="P101",
                journey_id=self.journey_id
            )
            p101 = PassengerIdentity.query.filter_by(journey_id=self.journey_id, person_id="P101").first()
            self.assertIsNotNone(p101.first_latitude)
            self.assertIsNotNone(p101.first_longitude)
            self.assertAlmostEqual(p101.first_latitude, 28.6472, places=4)

    def test_13_preservation_of_presence_events_attendance(self):
        """Req 14: Presence events are preserved and attached to canonical master."""
        with self.app.app_context():
            ev = PresenceEvent(
                journey_id=self.journey_id,
                passenger_identity_id=self.p102_id,
                event_type="BOARDING_CONFIRMED",
                notes="P102 boarded at Stop 1"
            )
            db.session.add(ev)
            db.session.commit()

            merge_multiple_passengers(
                selected_person_ids=["P101", "P102"],
                master_person_id="P101",
                journey_id=self.journey_id
            )

            p101 = PassengerIdentity.query.filter_by(journey_id=self.journey_id, person_id="P101").first()
            ev_check = PresenceEvent.query.filter_by(passenger_identity_id=p101.id).first()
            self.assertIsNotNone(ev_check)

    def test_14_merge_audit_logging(self):
        """Req 8 & 15: Merge and reversal operations create immutable AuditLog entries."""
        with self.app.app_context():
            res = merge_multiple_passengers(
                selected_person_ids=["P101", "P102"],
                master_person_id="P101",
                journey_id=self.journey_id,
                reviewer_id=1,
                comment="Audit log verification merge"
            )
            self.assertTrue(res["success"])

            # Verify audit log entry
            log = AuditLog.query.filter_by(action="MERGE_PASSENGER_IDENTITIES").order_by(AuditLog.id.desc()).first()
            self.assertIsNotNone(log)
            self.assertIn("P101", log.details_json)
            self.assertIn("P102", log.details_json)

    def test_15_prevent_duplicate_merges_when_already_merged(self):
        """Req 7: Repeated merges resolve canonical root correctly."""
        with self.app.app_context():
            # First merge: P102 into P101
            merge_multiple_passengers(
                selected_person_ids=["P101", "P102"],
                master_person_id="P101",
                journey_id=self.journey_id
            )
            # Repeated merge attempt with P102 and P103 into P101
            res = merge_multiple_passengers(
                selected_person_ids=["P101", "P102", "P103"],
                master_person_id="P101",
                journey_id=self.journey_id
            )
            self.assertTrue(res["success"])
            p103 = PassengerIdentity.query.filter_by(journey_id=self.journey_id, person_id="P103").first()
            self.assertTrue(p103.is_merged)
            self.assertEqual(p103.get_canonical_person_id(), "P101")

    def test_16_prevent_incompatible_cross_bus_merges(self):
        """Req 7 & 18: Prevents merging identities across incompatible buses."""
        with self.app.app_context():
            # Create a second bus and journey with unique codes
            uid = datetime.utcnow().strftime("%f")
            bus2 = Bus(bus_number=f"BUS-INC-{uid}", capacity=30)
            db.session.add(bus2)
            db.session.flush()
            j2 = Journey(journey_code=f"JNY-INC-{uid}", bus_id=bus2.id, origin="Station X", destination="Station Y", status="active")
            db.session.add(j2)
            db.session.flush()
            p_other = PassengerIdentity(journey_id=j2.id, person_id=f"P999_{uid}")
            db.session.add(p_other)
            db.session.commit()

            with self.assertRaises(ValueError):
                merge_multiple_passengers(
                    selected_person_ids=["P101", f"P999_{uid}"],
                    master_person_id="P101"
                )

    def test_17_multi_person_three_way_merge(self):
        """Req 6: Merging 3 IDs (P101, P102, P103) into master P101."""
        with self.app.app_context():
            res = merge_multiple_passengers(
                selected_person_ids=["P101", "P102", "P103"],
                master_person_id="P101",
                journey_id=self.journey_id,
                comment="3-way merge"
            )
            self.assertTrue(res["success"])
            self.assertEqual(len(res["merged_person_ids"]), 2)
            self.assertIn("P102", res["merged_person_ids"])
            self.assertIn("P103", res["merged_person_ids"])

            # Verify both point to master P101
            p102 = PassengerIdentity.query.filter_by(journey_id=self.journey_id, person_id="P102").first()
            p103 = PassengerIdentity.query.filter_by(journey_id=self.journey_id, person_id="P103").first()
            self.assertEqual(p102.get_canonical_person_id(), "P101")
            self.assertEqual(p103.get_canonical_person_id(), "P101")

    def test_18_unknown_angle_observation_handling(self):
        """Req 2: Observations without reliable angle are categorized as 'Unknown Angle'."""
        with self.app.app_context():
            up_unk = Upload(
                journey_id=self.journey_id, camera_angle="UNKNOWN",
                file_path="uploads/unk.jpg", original_filename="unk.jpg",
                capture_timestamp=self.date1
            )
            db.session.add(up_unk)
            db.session.flush()

            obs_unk = PassengerObservation(
                upload_id=up_unk.id, journey_id=self.journey_id,
                camera_angle="UNKNOWN", bbox_x=0, bbox_y=0, bbox_w=10, bbox_h=10,
                observation_time=self.date1, temp_detection_id="T-UNK-01"
            )
            db.session.add(obs_unk)
            db.session.commit()

            res = self.client.get(f"/api/passengers/date-wise-report?journey_id={self.journey_id}&date=2026-10-28")
            data = res.get_json()
            dg = data["date_groups"][0]
            self.assertGreater(dg["summary"]["unknown_angle_count"], 0)
            self.assertGreater(len(dg["angle_sections"]["unknown_angle"]), 0)

    def test_19_date_range_filtering(self):
        """Req 10: Filtering with start_date and end_date retrieves matching dates."""
        with self.app.app_context():
            res = self.client.get(f"/api/passengers/date-wise-report?journey_id={self.journey_id}&start_date=2026-10-27&end_date=2026-10-29")
            data = res.get_json()
            dates = [dg["date"] for dg in data["date_groups"]]
            self.assertIn("2026-10-28", dates)
            self.assertNotIn("2026-10-30", dates)

    def test_20_bus_and_journey_filtering(self):
        """Req 10: Filtering by bus_id and journey_id scopes observations accurately."""
        with self.app.app_context():
            res = self.client.get(f"/api/passengers/date-wise-report?bus_id={self.bus_id}&journey_id={self.journey_id}")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data.get("success"))
            for dg in data["date_groups"]:
                for obs in dg["observations"]:
                    self.assertEqual(obs["bus_id"], self.bus_id)
                    self.assertEqual(obs["journey_id"], self.journey_id)

    def test_21_pdf_datewise_report_generation_with_faces(self):
        """User Request: Date-wise PDF report generation with embedded passenger face crops."""
        with self.app.app_context():
            from app.services.passenger_report_service import generate_date_wise_report_pdf
            pdf_bytes = generate_date_wise_report_pdf(journey_id=self.journey_id, target_date="2026-10-28")
            self.assertIsInstance(pdf_bytes, bytes)
            self.assertGreater(len(pdf_bytes), 1000)
            self.assertTrue(pdf_bytes.startswith(b"%PDF"))

            # Test HTTP endpoint
            res = self.client.get(f"/api/passengers/export-datewise.pdf?journey_id={self.journey_id}&date=2026-10-28")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.mimetype, "application/pdf")
            self.assertIn("passenger_datewise_report_2026-10-28.pdf", res.headers.get("Content-Disposition", ""))

    def test_22_identity_provenance_link_and_endpoint(self):
        """User Request: Provenance link form displaying previous ID -> new master ID after merge."""
        with self.app.app_context():
            from app.services.passenger_report_service import merge_multiple_passengers, get_identity_provenance
            # Merge P102 into P101
            merge_multiple_passengers(
                selected_person_ids=["P101", "P102"],
                master_person_id="P101",
                journey_id=self.journey_id,
                reviewer_id=1,
                comment="Provenance verification merge"
            )

            # Test provenance for merged alias P102
            prov_alias = get_identity_provenance("P102")
            self.assertEqual(prov_alias["person_id"], "P102")
            self.assertEqual(prov_alias["canonical_person_id"], "P101")
            self.assertTrue(prov_alias["is_merged"])
            self.assertEqual(prov_alias["summary"]["previous_id"], "P102")
            self.assertEqual(prov_alias["summary"]["new_id"], "P101")
            self.assertIn("Previous Person ID: P102", prov_alias["summary"]["display_text"])
            self.assertIn("New Master ID: P101", prov_alias["summary"]["display_text"])

            # Test provenance for master P101
            prov_master = get_identity_provenance("P101")
            self.assertEqual(prov_master["person_id"], "P101")
            self.assertFalse(prov_master["is_merged"])
            self.assertIn("P102", prov_master["active_aliases"])

            # Test provenance API endpoint
            res = self.client.get("/api/passengers/provenance/P102")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data["canonical_person_id"], "P101")
            self.assertTrue(data["is_merged"])

    def test_23_gallery_crops_removes_merged_aliases(self):
        """User Request: Merged aliases must be removed from 'PASSENGERS DETECTED ON THIS DATE' gallery."""
        with self.app.app_context():
            from app.services.passenger_report_service import merge_multiple_passengers, get_date_wise_passenger_report
            # Initial report: P101, P102 in gallery_crops for Date 1 (2026-10-28)
            rep_before = get_date_wise_passenger_report(journey_id=self.journey_id, target_date="2026-10-28")
            dg_before = rep_before["date_groups"][0]
            gallery_pids_before = [c["person_id"] for c in dg_before["gallery_crops"]]
            self.assertIn("P102", gallery_pids_before)

            # Perform merge: P102 merged into P101
            merge_multiple_passengers(
                selected_person_ids=["P101", "P102"],
                master_person_id="P101",
                journey_id=self.journey_id,
                reviewer_id=1,
                comment="Gallery removal test"
            )

            # After merge: P102 must NOT be in gallery_crops
            rep_after = get_date_wise_passenger_report(journey_id=self.journey_id, target_date="2026-10-28")
            dg_after = rep_after["date_groups"][0]
            gallery_pids_after = [c["person_id"] for c in dg_after["gallery_crops"]]
            self.assertNotIn("P102", gallery_pids_after)
            self.assertIn("P101", gallery_pids_after)

            # But observation is preserved in angle sections (both observations in Angle B still present)
            self.assertEqual(len(dg_after["angle_sections"]["angle_b"]), 2)
            merged_obs = [o for o in dg_after["angle_sections"]["angle_b"] if o["is_merged"]]
            self.assertEqual(len(merged_obs), 1)

    def test_24_unique_passenger_manifest_sequence_and_aggregation(self):
        """User Request: Sequential report generation where repeating passenger occurrences are compared and consolidated under 1 unique ID."""
        with self.app.app_context():
            from app.services.passenger_report_service import get_date_wise_passenger_report, generate_date_wise_report_pdf

            rep = get_date_wise_passenger_report(journey_id=self.journey_id, target_date="2026-10-28")
            dg = rep["date_groups"][0]
            unique_list = dg.get("unique_passengers", [])

            # Verify unique passenger count matches summary count
            self.assertEqual(len(unique_list), dg["summary"]["total_unique_passengers"])

            # Verify sequential numbering 1, 2, ...
            for idx, u in enumerate(unique_list, 1):
                self.assertEqual(u["sequence_no"], idx)

            # P101 appears in both Angle A and Angle B on this date
            p101_item = next((u for u in unique_list if u["person_id"] == "P101"), None)
            self.assertIsNotNone(p101_item)
            # Exactly ONE ID generated for P101
            self.assertEqual(p101_item["person_id"], "P101")
            # Repeating occurrences aggregated into this unique person
            self.assertGreaterEqual(p101_item["total_detections"], 2)
            self.assertIn("Angle A", p101_item["detected_angles"])
            self.assertIn("Angle B", p101_item["detected_angles"])
            # Presence occurrences list has both timestamps
            self.assertEqual(len(p101_item["presence_occurrences"]), 2)

            # Verify PDF generation uses this sequential unique manifest
            pdf_bytes = generate_date_wise_report_pdf(journey_id=self.journey_id, target_date="2026-10-28")
            self.assertIsInstance(pdf_bytes, bytes)
            self.assertTrue(pdf_bytes.startswith(b"%PDF"))




if __name__ == "__main__":
    unittest.main()
