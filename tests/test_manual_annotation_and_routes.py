import os
import json
import base64
import unittest
from datetime import datetime
from PIL import Image
import io

from app import create_app
from app.database import db
from app.models import (
    User, Bus, Journey, Upload, PassengerIdentity,
    PassengerObservation, RouteStop
)
from app.config import Config


class TestManualAnnotationAndRouteTimeline(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

        # Login as operator
        with self.client.session_transaction() as sess:
            sess["user_id"] = 2
            sess["username"] = "operator"
            sess["role"] = "operator"

        # Ensure active bus & journey exist
        self.bus = Bus.query.first()
        if not self.bus:
            self.bus = Bus(
                registration_number="DL-01-A-4421",
                bus_number="101",
                bus_name="Kaushambi Vaishali Express",
                bus_type="Standard Transit",
                capacity=40,
                route_number="ROUTE-K1",
                starting_point="Koshambi Bus Stand",
                destination="Sector-1 Vaishali Terminal",
                bus_status="Active",
                is_active=True
            )
            db.session.add(self.bus)
            db.session.commit()

        self.journey = Journey.query.filter_by(status="active").first()
        if not self.journey:
            self.journey = Journey(
                journey_code="JRN-TEST-ROUTE-01",
                bus_id=self.bus.id,
                origin="Koshambi Bus Stand",
                destination="Sector-1 Vaishali Terminal",
                status="active"
            )
            db.session.add(self.journey)
            db.session.commit()

        # Create dummy upload for annotation test
        dummy_file = Config.UPLOAD_FOLDER / "test_annotation_bg.jpg"
        if not dummy_file.exists():
            img = Image.new("RGB", (640, 480), color=(100, 150, 200))
            img.save(str(dummy_file))

        self.upload = Upload(
            journey_id=self.journey.id,
            bus_id=self.bus.id,
            camera_angle="A",
            file_path=str(dummy_file),
            original_filename="test_annotation_bg.jpg",
            file_size=1024,
            status="processed",
            passengers_detected_count=0
        )
        db.session.add(self.upload)
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()

    def test_get_route_stops(self):
        """Verify GET /api/route-stops returns seeded stops and summary."""
        res = self.client.get("/api/route-stops")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data.get("success"))
        self.assertGreaterEqual(len(data.get("stops", [])), 31)

        summary = data.get("route_summary", {})
        self.assertIn("Koshambi", summary.get("origin", ""))
        self.assertTrue(any(x in summary.get("destination", "") for x in ["Vaishali", "Lucknow", "Qaiserbagh"]))
        self.assertGreaterEqual(summary.get("max_speed_kmh", 0), 10.0)

    def test_manually_add_and_delete_route_stop(self):
        """Verify POST /api/route-stops and DELETE /api/route-stops/<id>."""
        payload = {
            "stop_name": "Test Interchange Station",
            "location_address": "Test Road, Sahibabad Site 4, Ghaziabad",
            "time_window": "15-10-2026 | 06:38:15 PM",
            "speed_kmh": 4.5,
            "latitude": 28.6482,
            "longitude": 77.3235,
            "is_major_stop": True,
            "insert_after_order": 4
        }
        res = self.client.post("/api/route-stops", json=payload)
        self.assertEqual(res.status_code, 201)
        data = json.loads(res.data)
        self.assertTrue(data.get("success"))
        new_stop = data.get("stop")
        self.assertEqual(new_stop["stop_order"], 5)
        stop_id = new_stop["id"]

        # Delete the created stop
        del_res = self.client.delete(f"/api/route-stops/{stop_id}")
        self.assertEqual(del_res.status_code, 200)
        del_data = json.loads(del_res.data)
        self.assertTrue(del_data.get("success"))

    def test_passenger_trajectories_calculation(self):
        """Verify GET /api/route-stops/passenger-trajectories divides route into green & red."""
        # Ensure a test passenger exists
        p = PassengerIdentity.query.filter_by(journey_id=self.journey.id).first()
        if not p:
            p = PassengerIdentity(
                journey_id=self.journey.id,
                person_id="P_TEST_01",
                status="PRESENT"
            )
            db.session.add(p)
            db.session.commit()

        res = self.client.get(f"/api/route-stops/passenger-trajectories?journey_id={self.journey.id}")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data.get("success"))
        passengers = data.get("passengers", [])
        self.assertGreater(len(passengers), 0)

        target = passengers[0]
        self.assertIn("traveled_stop_ids", target)
        self.assertIn("non_traveled_stop_ids", target)
        self.assertGreater(len(target["traveled_stop_ids"]), 0)
        self.assertIn("boarding_stop", target)
        self.assertIn("exit_stop", target)

    def test_save_manual_annotation(self):
        """Verify POST /api/uploads/manual-annotation creates observation and links identity."""
        # Create a tiny base64 image crop
        img = Image.new("RGB", (40, 40), color=(255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        b64 = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")

        payload = {
            "upload_id": self.upload.id,
            "camera_angle": "A",
            "annotations": [
                {
                    "label": "P_MANUAL_TEST_1",
                    "bbox": [100, 120, 200, 260],
                    "crop_data": b64
                }
            ],
            "comment": "Operator manual missed passenger correction"
        }

        res = self.client.post("/api/uploads/manual-annotation", json=payload)
        self.assertEqual(res.status_code, 201)
        data = json.loads(res.data)
        self.assertTrue(data.get("success"))
        created = data.get("created_observations", [])
        self.assertEqual(len(created), 1)

        obs = created[0]
        self.assertEqual(obs["temp_detection_id"], "P_MANUAL_TEST_1")
        self.assertEqual(obs["confidence_label"], "Manual")
        self.assertEqual(obs["review_status"], "USER_ADDED")

        # Verify passenger identity was created
        p_identity = PassengerIdentity.query.filter_by(
            journey_id=self.journey.id,
            person_id="P_MANUAL_TEST_1"
        ).first()
        self.assertIsNotNone(p_identity)
        self.assertEqual(p_identity.status, "PRESENT")

        # Test deleting the manual observation
        obs_id = obs["id"]
        del_res = self.client.delete(f"/api/uploads/observations/{obs_id}")
        self.assertEqual(del_res.status_code, 200)
        self.assertIsNone(PassengerObservation.query.get(obs_id))

    def test_route_timeline_page_render(self):
        """Verify /route-timeline renders with HTTP 200."""
        res = self.client.get("/route-timeline")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Route Stops & Passenger Trajectory", res.data)
        self.assertIn(b"Bus Speed (0-15 km/h) Timeline", res.data)
        self.assertIn(b"route-map", res.data)


if __name__ == "__main__":
    unittest.main()
