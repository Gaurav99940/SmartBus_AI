import os
from flask import Blueprint, request, jsonify, session, current_app
from werkzeug.utils import secure_filename
from pathlib import Path

from app.config import Config
from app.models import Journey
from app.services.cloud_sync_service import (
    test_remote_server_connection,
    upload_images_to_remote,
    fetch_remote_images,
    delete_remote_image,
    delete_remote_images_bulk,
    export_journey_sync_bundle,
    sync_data_to_github,
    ingest_published_data,
    get_latest_sync_status
)

cloud_sync_bp = Blueprint("cloud_sync", __name__, url_prefix="/api")


# ============================================================================
# 1. CLOUD SYNC & REPORT PUBLISHING ENDPOINTS
# ============================================================================

@cloud_sync_bp.route("/sync/status", methods=["GET"])
def api_sync_status():
    """Returns current cloud sync status, remote server metrics, and last sync log."""
    data = get_latest_sync_status()
    return jsonify(data), 200


@cloud_sync_bp.route("/sync/publish/<int:journey_id>", methods=["POST"])
def api_publish_journey_reports(journey_id):
    """
    Step 1: Exports journey data (passengers, attendance, timeline, stops, PDFs, CSV, Excel).
    Step 2: Uploads passenger face crops to remote image server.
    Step 3: Pushes bundle to GitHub repository.
    """
    journey = Journey.query.get(journey_id)
    if not journey:
        return jsonify({"success": False, "error": f"Journey #{journey_id} not found."}), 404

    req_data = request.get_json(silent=True) or {}
    upload_remote = req_data.get("upload_remote_crops", True)
    commit_msg = req_data.get("commit_message")

    user_id = session.get("user_id")

    # Step A: Export bundle
    export_res = export_journey_sync_bundle(
        journey_id=journey_id,
        upload_remote_crops=upload_remote,
        user_id=user_id
    )
    if not export_res.get("success"):
        return jsonify(export_res), 400

    # Step B: Synchronize to GitHub
    sync_res = sync_data_to_github(
        journey_id=journey_id,
        commit_message=commit_msg,
        user_id=user_id
    )

    return jsonify({
        "success": True,
        "message": f"Successfully published reports for Journey #{journey.journey_code or journey.id} to GitHub & live server.",
        "export_details": export_res,
        "sync_details": sync_res
    }), 200


@cloud_sync_bp.route("/sync/publish-latest", methods=["POST"])
def api_publish_latest_journey():
    """Publishes the latest completed or active journey."""
    journey = Journey.query.order_by(Journey.id.desc()).first()
    if not journey:
        return jsonify({"success": False, "error": "No journeys found to publish."}), 404
    return api_publish_journey_reports(journey.id)


@cloud_sync_bp.route("/sync/pull-from-github", methods=["POST"])
def api_pull_and_ingest_reports():
    """
    Called on the live server (or manually triggered):
    Pulls latest data from repository and ingests into DB without running AI inference.
    """
    res = ingest_published_data()
    return jsonify(res), 200 if res.get("success") else 400


# ============================================================================
# 2. REMOTE IMAGE SERVER MANAGEMENT (http://143.244.140.108:8060)
# ============================================================================

@cloud_sync_bp.route("/remote-images", methods=["GET"])
def api_get_remote_images():
    """Lists images from the remote image server."""
    limit = request.args.get("limit", 100, type=int)
    offset = request.args.get("offset", 0, type=int)
    result = fetch_remote_images(limit=limit, offset=offset)
    return jsonify(result), 200 if result.get("success") else 502


@cloud_sync_bp.route("/remote-images/test", methods=["GET"])
def api_test_remote_server():
    """Pings remote image server connection."""
    info = test_remote_server_connection()
    return jsonify(info), 200 if info.get("connected") else 502


@cloud_sync_bp.route("/remote-images/upload", methods=["POST"])
def api_upload_remote_images():
    """
    Accepts multipart files and forwards them in bulk to POST http://143.244.140.108:8060/upload/bulk
    """
    files = request.files.getlist("files") or request.files.getlist("file")
    if not files:
        return jsonify({"success": False, "error": "No files selected for upload."}), 400

    temp_paths = []
    temp_dir = Path(Config.DATA_DIR) / "temp_remote_uploads"
    temp_dir.mkdir(parents=True, exist_ok=True)

    try:
        for f in files:
            if not f or not f.filename:
                continue
            safe_name = secure_filename(f.filename)
            save_path = temp_dir / safe_name
            f.save(save_path)
            temp_paths.append(str(save_path))

        if not temp_paths:
            return jsonify({"success": False, "error": "No valid files received."}), 400

        result = upload_images_to_remote(temp_paths)
        return jsonify(result), 200 if result.get("success") else 502

    finally:
        for p in temp_paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


@cloud_sync_bp.route("/remote-images/<filename>", methods=["DELETE"])
def api_delete_remote_image(filename):
    """
    Deletes a single image on the remote server.
    Query param ?force=true overrides the active passenger reference guard.
    """
    force = request.args.get("force", "false").lower() in ["true", "1", "yes"]
    user_id = session.get("user_id")
    res = delete_remote_image(filename=filename, force=force, user_id=user_id)
    if res.get("requires_confirmation"):
        return jsonify(res), 409
    return jsonify(res), 200 if res.get("success") else 400


@cloud_sync_bp.route("/remote-images/delete-bulk", methods=["POST"])
def api_delete_remote_images_bulk():
    """
    Bulk deletes multiple images on the remote server.
    Accepts JSON: {"filenames": ["photo1.jpg", "photo2.png"], "force": false}
    """
    req_data = request.get_json(silent=True) or {}
    filenames = req_data.get("filenames", [])
    force = bool(req_data.get("force", False))
    user_id = session.get("user_id")

    res = delete_remote_images_bulk(filenames=filenames, force=force, user_id=user_id)
    if res.get("requires_confirmation"):
        return jsonify(res), 409
    return jsonify(res), 200 if res.get("success") else 400
