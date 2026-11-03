import unittest
import json
from datetime import datetime, timedelta
from app import create_app
from app.database import db
from app.models import User, Bus, Journey, OperatorBusAssignment, BusLocationLog, PassengerIdentity

from app.config import Config

class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"

class TestBusesAndAdmin(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestingConfig)
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        # Seed test admin & test operator
        self.admin_user = User(username="test_admin", email="admin@test.com", role="admin", full_name="Admin Test")
        self.admin_user.set_password("adminpass")
        db.session.add(self.admin_user)

        self.operator_user = User(username="test_operator", email="op@test.com", role="operator", full_name="Driver Joe")
        self.operator_user.set_password("oppass")
        db.session.add(self.operator_user)

        # Seed sample bus
        self.bus1 = Bus(
            bus_number="BUS-TEST-01",
            registration_number="TEST-REG-01",
            license_plate="TEST-REG-01",
            bus_name="Fleet Test Alpha",
            bus_type="Standard Transit",
            capacity=40,
            route_name="Station A - Station B",
            route_number="10",
            starting_point="Station A",
            destination="Station B",
            bus_status="Active",
            assigned_operator_id=self.operator_user.id
        )
        db.session.add(self.bus1)
        db.session.commit()

        self.operator_user.current_bus_id = self.bus1.id
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def login_as(self, username, password):
        return self.client.post("/api/auth/login", json={
            "username": username,
            "password": password
        })

    def test_bus_registration_validation(self):
        """Test registration validation: registration_number or bus_number required."""
        self.login_as("test_operator", "oppass")
        res = self.client.post("/api/buses", json={
            "bus_name": "Incomplete Bus"
        })
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertIn("error", data)

    def test_bus_registration_success(self):
        """Test registering a brand new bus."""
        self.login_as("test_operator", "oppass")
        res = self.client.post("/api/buses", json={
            "registration_number": "NEW-REG-99",
            "bus_number": "BUS-999",
            "bus_name": "Metro Express",
            "bus_type": "Electric Bus",
            "capacity": 55,
            "route_number": "Line 5",
            "starting_point": "Downtown",
            "destination": "Airport",
            "bus_status": "Active"
        })
        self.assertEqual(res.status_code, 201)
        data = res.get_json()
        self.assertFalse(data["is_existing"])
        self.assertEqual(data["bus"]["registration_number"], "NEW-REG-99")
        self.assertEqual(data["bus"]["capacity"], 55)

        # Verify bus is in DB
        bus = Bus.query.filter_by(registration_number="NEW-REG-99").first()
        self.assertIsNotNone(bus)
        self.assertEqual(bus.bus_name, "Metro Express")

    def test_duplicate_bus_prevention(self):
        """Test registering an already existing registration number does not create duplicate."""
        self.login_as("test_operator", "oppass")
        initial_count = Bus.query.count()

        # Try registering same registration number
        res = self.client.post("/api/buses", json={
            "registration_number": "TEST-REG-01",
            "bus_name": "Updated Fleet Alpha",
            "capacity": 50
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["is_existing"])
        self.assertEqual(Bus.query.count(), initial_count, "Should not create duplicate bus record")

        # Verify updated info
        bus = Bus.query.filter_by(registration_number="TEST-REG-01").first()
        self.assertEqual(bus.bus_name, "Updated Fleet Alpha")
        self.assertEqual(bus.capacity, 50)

    def test_operator_select_and_switch_bus(self):
        """Test operator switching to another bus."""
        # Create second bus
        bus2 = Bus(
            bus_number="BUS-TEST-02",
            registration_number="TEST-REG-02",
            bus_name="Fleet Test Beta",
            bus_status="Active"
        )
        db.session.add(bus2)
        db.session.commit()

        self.login_as("test_operator", "oppass")
        res = self.client.post(f"/api/buses/{bus2.id}/select")
        self.assertEqual(res.status_code, 200)

        # Check operator's current_bus_id
        op = User.query.get(self.operator_user.id)
        self.assertEqual(op.current_bus_id, bus2.id)

        # Check operator's assigned buses endpoint
        res_my = self.client.get("/api/buses/operator/my-buses")
        self.assertEqual(res_my.status_code, 200)
        data = res_my.get_json()
        self.assertEqual(data["current_bus_id"], bus2.id)
        bus_ids = [b["id"] for b in data["buses"]]
        self.assertIn(bus2.id, bus_ids)

    def test_operator_gps_location_reporting(self):
        """Test operator reporting live GPS location with valid and invalid coordinates."""
        self.login_as("test_operator", "oppass")

        # Invalid coordinates (lat > 90)
        res_bad = self.client.post("/api/buses/operator/location", json={
            "latitude": 95.0,
            "longitude": -122.4,
            "location_source": "DEVICE_API"
        })
        self.assertEqual(res_bad.status_code, 400)

        # Valid coordinates
        res_good = self.client.post("/api/buses/operator/location", json={
            "latitude": 37.7749,
            "longitude": -122.4194,
            "accuracy": 8.5,
            "location_source": "DEVICE_API",
            "bus_id": self.bus1.id
        })
        self.assertEqual(res_good.status_code, 200)
        data = res_good.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["location"]["freshness"], "Live")
        self.assertEqual(data["location"]["latitude"], 37.7749)
        self.assertEqual(data["location"]["accuracy"], 8.5)

        # Verify location log saved in DB
        log = BusLocationLog.query.filter_by(bus_id=self.bus1.id).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.latitude, 37.7749)

    def test_gps_location_freshness_states(self):
        """Test Live vs Stale vs Offline vs Manual location freshness calculation."""
        bus = self.bus1

        # Offline (no coordinates or location_time)
        bus.latest_latitude = None
        bus.latest_location_time = None
        self.assertEqual(bus.get_location_freshness(), "Offline")

        # Live (< 2 minutes ago)
        bus.latest_latitude = 37.77
        bus.latest_longitude = -122.41
        bus.latest_location_source = "DEVICE_API"
        bus.latest_location_time = datetime.utcnow() - timedelta(seconds=30)
        self.assertEqual(bus.get_location_freshness(), "Live")

        # Stale (> 2 minutes ago)
        bus.latest_location_time = datetime.utcnow() - timedelta(minutes=5)
        self.assertEqual(bus.get_location_freshness(), "Stale")

        # Manual Location
        bus.latest_location_source = "MANUAL_ENTRY"
        self.assertEqual(bus.get_location_freshness(), "Manual Location")

    def test_admin_dashboard_stats_and_filtering(self):
        """Test dashboard stats endpoint with 8 summary cards, active buses, and bus filter."""
        self.login_as("test_admin", "adminpass")

        # All buses
        res = self.client.get("/api/dashboard/stats")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("summary_cards", data)
        self.assertIn("total_registered_buses", data["summary_cards"])
        self.assertIn("active_buses_count", data["summary_cards"])
        self.assertIn("active_buses", data)
        self.assertIn("recent_activity", data)
        self.assertIsNone(data["selected_bus_details"])

        # Filter by specific bus
        res_filter = self.client.get(f"/api/dashboard/stats?bus_id={self.bus1.id}")
        self.assertEqual(res_filter.status_code, 200)
        data_filter = res_filter.get_json()
        self.assertEqual(data_filter["selected_bus_id"], self.bus1.id)
        self.assertIsNotNone(data_filter["selected_bus_details"])
        self.assertEqual(data_filter["selected_bus_details"]["bus"]["id"], self.bus1.id)

    def test_admin_upload_access_restriction(self):
        """Test that Admin is redirected from /upload to /dashboard."""
        self.login_as("test_admin", "adminpass")
        res = self.client.get("/upload")
        # Should redirect to /dashboard
        self.assertEqual(res.status_code, 302)
        self.assertIn("/dashboard", res.location)

        # Operator can access /upload
        self.login_as("test_operator", "oppass")
        res_op = self.client.get("/upload")
        self.assertEqual(res_op.status_code, 200)

    def test_operators_api_and_assignment(self):
        """Test listing operators and assigning an operator to a bus."""
        self.login_as("test_admin", "adminpass")

        res = self.client.get("/api/buses/operators")
        self.assertEqual(res.status_code, 200)
        ops = res.get_json()
        self.assertTrue(len(ops) >= 1)
        usernames = [o["username"] for o in ops]
        self.assertIn("test_operator", usernames)

        # Create new bus and assign operator to it
        bus3 = Bus(bus_number="BUS-ASSIGN-01", registration_number="ASSIGN-REG-01")
        db.session.add(bus3)
        db.session.commit()

        res_assign = self.client.post("/api/buses/operators/assign", json={
            "operator_id": self.operator_user.id,
            "bus_id": bus3.id
        })
        self.assertEqual(res_assign.status_code, 200)
        bus3_refreshed = Bus.query.get(bus3.id)
        self.assertEqual(bus3_refreshed.assigned_operator_id, self.operator_user.id)

    def test_passenger_seat_assignment_and_reassign(self):
        """Test passenger seat calculation and dynamic seat reassignment."""
        self.login_as("test_operator", "oppass")

        # Create active journey and passenger
        journey = Journey(
            journey_code="JNY-SEAT-TEST",
            bus_id=self.bus1.id,
            origin="Station A",
            destination="Station B",
            status="active"
        )
        db.session.add(journey)
        db.session.commit()

        p = PassengerIdentity(
            journey_id=journey.id,
            person_id="P003",
            status="PRESENT",
            first_seen_time=datetime.utcnow(),
            last_seen_time=datetime.utcnow()
        )
        db.session.add(p)
        db.session.commit()

        # Check default deterministic seat derivation (P003 -> "1C")
        self.assertEqual(p.get_seat_number(), "1C")

        # Reassign to "3B"
        res = self.client.post(f"/api/passengers/{p.id}/seat", json={"seat": "3B"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["passenger"]["current_seat"], "3B")
        self.assertEqual(data["passenger"]["previous_seat"], "1C")

        # Verify DB updated
        p_refreshed = db.session.get(PassengerIdentity, p.id)
        self.assertEqual(p_refreshed.current_seat, "3B")

    def test_data_analysis_and_cleanup(self):
        """Test storage analysis API and historical journey cleanup."""
        self.login_as("test_admin", "adminpass")

        # Create a completed old journey with uploads
        old_journey = Journey(
            journey_code="JNY-OLD-001",
            bus_id=self.bus1.id,
            origin="Depot",
            destination="Terminal",
            status="completed",
            created_at=datetime.utcnow() - timedelta(days=45)
        )
        db.session.add(old_journey)
        db.session.commit()

        old_journey_id = old_journey.id

        # 1. Test data analysis endpoint
        res_analysis = self.client.get("/api/settings/data-analysis")
        self.assertEqual(res_analysis.status_code, 200)
        analysis_data = res_analysis.get_json()
        self.assertIn("storage", analysis_data)
        self.assertIn("uploads_mb", analysis_data["storage"])
        self.assertIn("counts", analysis_data)
        self.assertIn("journeys", analysis_data)
        self.assertTrue(len(analysis_data["journeys"]) >= 1)

        # 2. Test single journey deletion
        res_del = self.client.post("/api/settings/data-cleanup", json={
            "cleanup_type": "single_journey",
            "journey_id": old_journey_id,
            "delete_disk_files": True
        })
        self.assertEqual(res_del.status_code, 200)
        del_data = res_del.get_json()
        self.assertTrue(del_data["success"])
        self.assertEqual(del_data["deleted_journeys"], 1)

        # Verify old journey removed from DB
        deleted_check = db.session.get(Journey, old_journey_id)
        self.assertIsNone(deleted_check)

if __name__ == "__main__":
    unittest.main()

