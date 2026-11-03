import unittest
from datetime import datetime
from flask import Flask
from app.config import Config
from app.database import db
from app.models import Journey, Bus, Upload, PassengerIdentity, PresenceEvent
from app.services.journey_service import process_upload_for_journey, update_manual_passenger_status

class TestStateMachine(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        self.app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        self.app.config["TESTING"] = True
        Config.MISSED_OBSERVATIONS_FOR_EXIT_UNCONFIRMED = 3
        
        db.init_app(self.app)
        with self.app.app_context():
            db.create_all()
            # Seed bus and journey
            bus = Bus(bus_number="TEST-BUS", capacity=30)
            db.session.add(bus)
            db.session.flush()

            journey = Journey(
                journey_code="TEST-JNY-01",
                bus_id=bus.id,
                origin="Point A",
                destination="Point B",
                status="active"
            )
            db.session.add(journey)
            db.session.commit()
            self.journey_id = journey.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def test_passenger_presence_and_absence_transitions(self):
        with self.app.app_context():
            # Create Upload 1: Passenger P001 detected
            up1 = Upload(
                journey_id=self.journey_id,
                file_path="dummy.jpg",
                original_filename="dummy.jpg",
                capture_timestamp=datetime.utcnow(),
                latitude=37.7749,
                longitude=-122.4194
            )
            db.session.add(up1)
            db.session.commit()

            # Mock detected face
            detected_face_1 = {
                "bbox": [10, 10, 50, 50],
                "detector_prob": 0.99,
                "quality": {"is_acceptable": True, "is_blur": False, "quality_score": 0.9},
                "crop_path": "crops/test.jpg",
                "embedding": [0.1] * 512
            }

            res1 = process_upload_for_journey(up1.id, [detected_face_1])
            self.assertEqual(res1["passengers_detected"], 1)

            p1 = PassengerIdentity.query.filter_by(journey_id=self.journey_id).first()
            self.assertIsNotNone(p1)
            self.assertEqual(p1.status, "PRESENT")
            self.assertEqual(p1.total_observations, 1)
            self.assertEqual(p1.consecutive_missed_count, 0)

            # Upload 2: Passenger not detected -> Must become POSSIBLY_ABSENT (not immediately exited!)
            up2 = Upload(
                journey_id=self.journey_id,
                file_path="dummy2.jpg",
                original_filename="dummy2.jpg",
                capture_timestamp=datetime.utcnow()
            )
            db.session.add(up2)
            db.session.commit()

            res2 = process_upload_for_journey(up2.id, [])
            p1_refreshed = PassengerIdentity.query.get(p1.id)
            self.assertEqual(p1_refreshed.status, "POSSIBLY_ABSENT")
            self.assertEqual(p1_refreshed.consecutive_missed_count, 1)

            # Upload 3: Still not detected (missed = 2) -> remains POSSIBLY_ABSENT
            up3 = Upload(
                journey_id=self.journey_id,
                file_path="dummy3.jpg",
                original_filename="dummy3.jpg",
                capture_timestamp=datetime.utcnow()
            )
            db.session.add(up3)
            db.session.commit()
            process_upload_for_journey(up3.id, [])
            p1_refreshed = PassengerIdentity.query.get(p1.id)
            self.assertEqual(p1_refreshed.status, "POSSIBLY_ABSENT")
            self.assertEqual(p1_refreshed.consecutive_missed_count, 2)

            # Upload 4: Missed for 3rd consecutive photo (threshold = 3) -> transitions to EXIT_UNCONFIRMED
            up4 = Upload(
                journey_id=self.journey_id,
                file_path="dummy4.jpg",
                original_filename="dummy4.jpg",
                capture_timestamp=datetime.utcnow()
            )
            db.session.add(up4)
            db.session.commit()
            process_upload_for_journey(up4.id, [])
            p1_refreshed = PassengerIdentity.query.get(p1.id)
            self.assertEqual(p1_refreshed.status, "EXIT_UNCONFIRMED")
            self.assertEqual(p1_refreshed.consecutive_missed_count, 3)

            # Operator manual confirmation -> transitions to EXIT_CONFIRMED
            updated_p = update_manual_passenger_status(
                passenger_id=p1.id,
                new_status="EXIT_CONFIRMED",
                comment="Passenger seen stepping down at stop",
                reviewer_id=None
            )
            self.assertEqual(updated_p.status, "EXIT_CONFIRMED")

if __name__ == "__main__":
    unittest.main()
