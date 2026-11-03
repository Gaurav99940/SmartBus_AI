import json
import unittest
from datetime import datetime
from app import create_app
from app.database import db
from app.models import User, Bus, RouteStop, MasterRouteStop, Journey

class TestOperatorIsolationAndStopPicker(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

        # Create distinct test users
        self.admin = User.query.filter_by(username="test_admin_iso").first()
        if not self.admin:
            self.admin = User(username="test_admin_iso", email="test_admin_iso@smartbus.local", role="admin")
            self.admin.set_password("adminpass")
            db.session.add(self.admin)

        self.operator = User.query.filter_by(username="test_op_iso").first()
        if not self.operator:
            self.operator = User(username="test_op_iso", email="test_op_iso@smartbus.local", role="operator")
            self.operator.set_password("oppass")
            db.session.add(self.operator)

        db.session.commit()

    def tearDown(self):
        self.ctx.pop()

    def test_master_stops_library_accessible(self):
        """Verify admin master transit stops endpoint returns seeded library."""
        res = self.client.get("/api/route-stops/master-stops")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertGreaterEqual(data["count"], 31)
        names = [s["stop_name"] for s in data["stops"]]
        self.assertIn("Koshambi Bus Stand", names)
        self.assertIn("Kaushambi Metro (McD)", names)
        self.assertIn("Sector-1 Vaishali Terminal", names)

    def test_operator_bus_registration_and_isolation(self):
        """
        Verify:
        1. Operator registers a bus selecting 3 specific stops from master list + 1 custom stop.
        2. Operator sees only their bus on GET /api/buses.
        3. Operator sees only their 4 configured stops on GET /api/route-stops (no 31-stop auto-fill).
        4. Admin sees all fleet buses and can inspect this bus by ID.
        """
        # Login as operator
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.operator.id
            sess["username"] = self.operator.username
            sess["role"] = "operator"

        # Register bus with custom stops selection
        payload = {
            "registration_number": "UP14-CUSTOM-99",
            "bus_name": "Gaurav Express",
            "bus_type": "Standard Transit",
            "capacity": 42,
            "starting_point": "Koshambi Bus Stand",
            "destination": "Custom Terminal Anand Vihar",
            "route_stops": [
                {
                    "stop_name": "Koshambi Bus Stand",
                    "location_address": "Koshambi Bus Stand, Plot Alpha, Sahibabad",
                    "time_window": "06:00 PM",
                    "speed_kmh": 3.0,
                    "is_major_stop": True
                },
                {
                    "stop_name": "Pacific Mall Main Gate",
                    "location_address": "Pacific Mall, Plot Alpha, Sahibabad",
                    "time_window": "06:15 PM",
                    "speed_kmh": 10.0,
                    "is_major_stop": True
                },
                {
                    "stop_name": "Kaushambi Metro (McD)",
                    "location_address": "Kaushambi Metro, Sahibabad",
                    "time_window": "06:25 PM",
                    "speed_kmh": 15.0,
                    "is_major_stop": True
                },
                {
                    "stop_name": "Custom Stop — Anand Vihar Border", # Custom manual stop
                    "location_address": "Anand Vihar Border ISBT Link",
                    "time_window": "06:35 PM",
                    "speed_kmh": 12.0,
                    "is_major_stop": False
                }
            ]
        }
        res = self.client.post("/api/buses", json=payload)
        self.assertIn(res.status_code, (200, 201))
        data = res.get_json()
        bus_id = data["bus"]["id"]
        self.assertEqual(data["bus"]["registration_number"], "UP14-CUSTOM-99")

        # 1. Operator portal GET /api/buses returns strictly operator's bus
        res_buses = self.client.get("/api/buses")
        self.assertEqual(res_buses.status_code, 200)
        operator_buses = res_buses.get_json()
        self.assertEqual(len(operator_buses), 1)
        self.assertEqual(operator_buses[0]["id"], bus_id)
        self.assertEqual(operator_buses[0]["registration_number"], "UP14-CUSTOM-99")

        # 2. Operator portal GET /api/route-stops returns strictly the 4 chosen stops
        res_stops = self.client.get("/api/route-stops")
        self.assertEqual(res_stops.status_code, 200)
        stops_data = res_stops.get_json()
        self.assertEqual(stops_data["count"], 4)
        stop_names = [s["stop_name"] for s in stops_data["stops"]]
        self.assertEqual(stop_names, [
            "Koshambi Bus Stand",
            "Pacific Mall Main Gate",
            "Kaushambi Metro (McD)",
            "Custom Stop — Anand Vihar Border"
        ])
        self.assertNotIn("Sector-1 Vaishali Terminal", stop_names)

        # 3. Admin portal login
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin.id
            sess["username"] = self.admin.username
            sess["role"] = "admin"

        # Admin GET /api/buses sees fleet buses
        res_admin_buses = self.client.get("/api/buses")
        self.assertEqual(res_admin_buses.status_code, 200)
        admin_buses = res_admin_buses.get_json()
        self.assertGreaterEqual(len(admin_buses), 1)
        admin_bus_ids = [b["id"] for b in admin_buses]
        self.assertIn(bus_id, admin_bus_ids)

        # Admin inspects this specific bus's route timeline
        res_admin_route = self.client.get(f"/api/route-stops?bus_id={bus_id}")
        self.assertEqual(res_admin_route.status_code, 200)
        admin_route_data = res_admin_route.get_json()
        self.assertEqual(admin_route_data["count"], 4)
        self.assertEqual(admin_route_data["bus_id"], bus_id)

if __name__ == "__main__":
    unittest.main()
