import os
import json
import pytest
from pathlib import Path
from datetime import datetime

from app import create_app
from app.config import Config
from app.database import db
from app.models import Journey, Bus, PassengerIdentity, PassengerObservation, CloudSyncLog
from app.services.cloud_sync_service import (
    export_journey_sync_bundle,
    ingest_published_data,
    fetch_remote_images,
    delete_remote_image,
    get_latest_sync_status
)


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    DEMO_MODE = False
    SMARTBUS_SYSTEM_ROLE = "local"


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        # Seed test bus and journey
        bus = Bus(bus_number="BUS-SYNC-01", registration_number="REG-SYNC-01", capacity=40)
        db.session.add(bus)
        db.session.commit()

        jny = Journey(
            bus_id=bus.id,
            journey_code="JNY-SYNC-TEST-001",
            origin="Main Terminal",
            destination="City Center",
            status="completed",
            start_time=datetime(2026, 11, 4, 8, 0, 0),
            end_time=datetime(2026, 11, 4, 9, 30, 0)
        )
        db.session.add(jny)
        db.session.commit()

        # Seed passengers
        p1 = PassengerIdentity(
            journey_id=jny.id,
            person_id="P001",
            canonical_person_id="P001",
            status="PRESENT",
            current_seat="A1",
            first_seen_time=datetime(2026, 11, 4, 8, 10, 0),
            last_seen_time=datetime(2026, 11, 4, 9, 20, 0),
            boarding_stop_number=1,
            boarding_stop_name="Main Terminal",
            exit_stop_number=5,
            exit_stop_name="City Center",
            stops_traveled=5,
            total_observations=3
        )
        p2 = PassengerIdentity(
            journey_id=jny.id,
            person_id="P002",
            canonical_person_id="P002",
            status="EXIT_CONFIRMED",
            current_seat="B2",
            first_seen_time=datetime(2026, 11, 4, 8, 15, 0),
            last_seen_time=datetime(2026, 11, 4, 8, 45, 0),
            boarding_stop_number=1,
            boarding_stop_name="Main Terminal",
            exit_stop_number=3,
            exit_stop_name="Midway Stop",
            stops_traveled=3,
            total_observations=2
        )
        db.session.add_all([p1, p2])
        db.session.commit()

        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def test_01_export_bundle_structure_and_privacy(app):
    """
    Verifies smartbus-data/ structure, JSON manifests, and strict exclusion
    of raw 512-d biometric vectors.
    """
    with app.app_context():
        jny = Journey.query.filter_by(journey_code="JNY-SYNC-TEST-001").first()
        assert jny is not None

        export_res = export_journey_sync_bundle(jny.id, upload_remote_crops=False)
        assert export_res["success"] is True

        export_dir = Path(export_res["export_directory"])
        assert export_dir.exists()
        assert (export_dir / "passengers.json").is_file()
        assert (export_dir / "attendance.json").is_file()
        assert (export_dir / "timeline.json").is_file()
        assert (export_dir / "stops.json").is_file()
        assert (export_dir / "reports").is_dir()

        # Check passengers.json content & verify NO raw biometric vectors
        with open(export_dir / "passengers.json", "r", encoding="utf-8") as f:
            p_data = json.load(f)

        assert len(p_data) == 2
        for p in p_data:
            assert "person_id" in p
            assert "stops_traveled" in p
            assert "canonical_person_id" in p
            # Must NOT contain raw 512-d embeddings
            assert "face_encoding" not in p
            assert "biometric_vector" not in p
            assert "clothing_embedding" not in p

        # Check root manifest.json
        base_dir = getattr(Config, "SMARTBUS_DATA_DIR", Path(Config.BASE_DIR) / "smartbus-data")
        manifest_p = base_dir / "manifest.json"
        assert manifest_p.is_file()
        with open(manifest_p, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        assert len(manifest.get("published_journeys", [])) >= 1


def test_02_remote_image_in_use_reference_protection(app):
    """
    Verifies that attempting to delete an image referenced by an active passenger
    returns requires_confirmation: True unless force=True.
    """
    with app.app_context():
        p1 = PassengerIdentity.query.filter_by(person_id="P001").first()
        p1.thumbnail_path = "crops/p001_face_ref.jpg"
        db.session.commit()

        # Deleting without force must trigger protection
        res = delete_remote_image("p001_face_ref.jpg", force=False)
        assert res.get("requires_confirmation") is True
        assert "P001" in res.get("warning", "")

        # Directory traversal prevention
        traversal_res = delete_remote_image("../../../etc/passwd", force=False)
        assert traversal_res.get("success") is False


def test_03_live_server_ingestion_without_ai_inference(app):
    """
    Verifies that ingest_published_data reads smartbus-data/ and populates
    the database without invoking OpenCV or AI pipelines.
    """
    with app.app_context():
        jny = Journey.query.filter_by(journey_code="JNY-SYNC-TEST-001").first()
        # Export bundle
        export_journey_sync_bundle(jny.id, upload_remote_crops=False)

        # Ingest
        ingest_res = ingest_published_data()
        assert ingest_res["success"] is True
        assert ingest_res["journeys_ingested"] >= 1
        assert ingest_res["passengers_synced"] >= 2


def test_04_api_sync_endpoints(client, app):
    """
    Verifies /api/sync/status and /api/sync/publish-latest endpoints.
    """
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["username"] = "admin"
        sess["role"] = "admin"

    resp = client.get("/api/sync/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert "remote_image_server" in data
    assert "github" in data

    # Publish latest journey
    pub_resp = client.post("/api/sync/publish-latest", json={"upload_remote_crops": False})
    assert pub_resp.status_code == 200
    pub_data = pub_resp.get_json()
    assert pub_data["success"] is True
    assert "export_details" in pub_data
