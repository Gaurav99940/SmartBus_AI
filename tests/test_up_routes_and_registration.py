import unittest
import json
from app import create_app
from app.config import Config
from app.database import db
from app.models import User, Bus, RouteStop, MasterUPRoute


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False


class TestUPRoutesAndRegistration(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        # Seed an admin and an operator
        self.admin = User(username="admin_test", email="admin_test@smartbus.local", role="admin")
        self.admin.set_password("adminpass")

        self.operator = User(username="operator_test", email="operator_test@smartbus.local", role="operator")
        self.operator.set_password("operatorpass")

        db.session.add_all([self.admin, self.operator])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_user_registration_success(self):
        """Test POST /api/auth/register creates operator and sets session"""
        res = self.client.post("/api/auth/register", json={
            "username": "up_driver_99",
            "password": "driverpassword",
            "full_name": "Ramesh Kumar",
            "email": "ramesh@smartbus.local",
            "phone": "+91 9876543210",
            "role": "operator"
        })
        self.assertEqual(res.status_code, 201)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["user"]["username"], "up_driver_99")
        self.assertEqual(data["redirect_url"], "/buses?setup=1")

        # Verify user is in database
        u = User.query.filter_by(username="up_driver_99").first()
        self.assertIsNotNone(u)
        self.assertTrue(u.check_password("driverpassword"))

        # Verify session is authenticated
        res_me = self.client.get("/api/auth/me")
        self.assertEqual(res_me.status_code, 200)
        me_data = res_me.get_json()
        self.assertTrue(me_data["authenticated"])
        self.assertEqual(me_data["user"]["username"], "up_driver_99")

    def test_user_registration_validation(self):
        """Test username collision and short password validations"""
        # Duplicate username
        res = self.client.post("/api/auth/register", json={
            "username": "operator_test",
            "password": "somepass"
        })
        self.assertEqual(res.status_code, 409)

        # Short password
        res = self.client.post("/api/auth/register", json={
            "username": "new_user",
            "password": "12"
        })
        self.assertEqual(res.status_code, 400)

    def test_up_routes_api_and_dynamic_creation(self):
        """Test listing UP Master Routes and creating a new UP Route"""
        # GET /api/route-stops/up-routes (seeded with 10 UP master corridors)
        res = self.client.get("/api/route-stops/up-routes")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertGreaterEqual(data["count"], 1)
        route_codes = [r["route_code"] for r in data["routes"]]
        self.assertIn("UP-RT-01", route_codes)

        # GET /api/route-stops/up-routes/<id>
        route1 = MasterUPRoute.query.filter_by(route_code="UP-RT-01").first()
        self.assertIsNotNone(route1)
        res_detail = self.client.get(f"/api/route-stops/up-routes/{route1.id}")
        self.assertEqual(res_detail.status_code, 200)
        detail_data = res_detail.get_json()
        self.assertEqual(detail_data["route"]["route_code"], "UP-RT-01")
        self.assertGreaterEqual(len(detail_data["route"]["stops"]), 2)

        # POST /api/route-stops/up-routes (Admin creates new UP route)
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin.id
            sess["role"] = "admin"

        new_route_payload = {
            "route_code": "UP-RT-20",
            "route_name": "Agra — Mathura — Vrindavan Heritage Route",
            "region": "Braj Region",
            "origin": "Idgah Bus Stand, Agra",
            "destination": "Vrindavan Bus Depot, Mathura",
            "total_distance_km": 75.0,
            "estimated_duration_hours": 1.5,
            "avg_speed_kmh": 12.0,
            "stops": [
                {"order": 1, "name": "Idgah Bus Stand", "address": "Agra, UP", "speed": 5.0, "major": True},
                {"order": 2, "name": "Vrindavan Terminal", "address": "Mathura, UP", "speed": 4.0, "major": True}
            ]
        }
        res_create = self.client.post("/api/route-stops/up-routes", json=new_route_payload)
        self.assertEqual(res_create.status_code, 201)
        cdata = res_create.get_json()
        self.assertTrue(cdata["success"])
        self.assertEqual(cdata["route"]["route_code"], "UP-RT-20")

    def test_operator_bus_registration_with_up_route_stops(self):
        """Test operator registers a bus selecting a UP route and syncing route stops"""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.operator.id
            sess["username"] = self.operator.username
            sess["role"] = "operator"

        bus_payload = {
            "registration_number": "UP32-EXPRESS-77",
            "bus_name": "Lucknow Rajdhani Superfast",
            "bus_type": "Coach",
            "capacity": 45,
            "route_number": "UP-RT-01",
            "starting_point": "Kaushambi Bus Stand, Ghaziabad",
            "destination": "Alambagh Bus Terminal, Lucknow",
            "route_name": "Delhi/NCR — Moradabad — Bareilly — Lucknow Express",
            "operator_name": "Driver Satish",
            "operator_contact": "+91 9988776655",
            "bus_status": "Active",
            "route_stops": [
                {"stop_order": 1, "stop_name": "Kaushambi ISBT, Ghaziabad", "location_address": "Kaushambi, Ghaziabad", "speed_kmh": 3.0, "time_window": "06:00 AM", "is_major_stop": True},
                {"stop_order": 2, "stop_name": "Moradabad Imperial Depot", "location_address": "Moradabad, UP", "speed_kmh": 9.0, "time_window": "09:30 AM", "is_major_stop": True},
                {"stop_order": 3, "stop_name": "Bareilly Satellite Terminal", "location_address": "Bareilly, UP", "speed_kmh": 8.0, "time_window": "11:45 AM", "is_major_stop": True},
                {"stop_order": 4, "stop_name": "Alambagh ISBT, Lucknow", "location_address": "Lucknow, UP", "speed_kmh": 5.0, "time_window": "03:30 PM", "is_major_stop": True}
            ]
        }
        res_bus = self.client.post("/api/buses", json=bus_payload)
        self.assertIn(res_bus.status_code, (200, 201))
        bus_id = res_bus.get_json()["bus"]["id"]

        # Operator route stops timeline endpoint returns these 4 stops
        res_timeline = self.client.get("/api/route-stops")
        self.assertEqual(res_timeline.status_code, 200)
        timeline_data = res_timeline.get_json()
        self.assertEqual(timeline_data["count"], 4)
        self.assertEqual(timeline_data["stops"][0]["stop_name"], "Kaushambi ISBT, Ghaziabad")
        self.assertEqual(timeline_data["stops"][3]["stop_name"], "Alambagh ISBT, Lucknow")

        # Admin checks fleet summary endpoint
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin.id
            sess["role"] = "admin"

        res_fleet = self.client.get("/api/route-stops/admin/fleet-summary")
        self.assertEqual(res_fleet.status_code, 200)
        fleet_data = res_fleet.get_json()
        self.assertTrue(fleet_data["success"])
        registered_bus = next((b for b in fleet_data["fleet"] if b["id"] == bus_id), None)
        self.assertIsNotNone(registered_bus)
        self.assertEqual(registered_bus["registration_number"], "UP32-EXPRESS-77")
        self.assertEqual(registered_bus["route_number"], "UP-RT-01")
        self.assertEqual(registered_bus["stops_count"], 4)


if __name__ == "__main__":
    unittest.main()
