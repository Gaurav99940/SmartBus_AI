import os
from pathlib import Path
from flask import Flask, render_template, send_from_directory, redirect, url_for, session, abort
from app.config import Config
from app.database import db, init_db
from app.models import User, Bus, Journey

def create_app(config_class=Config):
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.from_object(config_class)

    # Ensure necessary folders exist
    for p in [Config.DATA_DIR, Config.UPLOAD_FOLDER, Config.CROP_FOLDER]:
        Path(p).mkdir(parents=True, exist_ok=True)

    # Initialize Database
    init_db(app)

    # Register Blueprints
    from app.routes.auth import auth_bp
    from app.routes.buses import buses_bp
    from app.routes.journeys import journeys_bp
    from app.routes.uploads import uploads_bp
    from app.routes.passengers import passengers_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.map import map_bp
    from app.routes.reports import reports_bp
    from app.routes.settings import settings_bp
    from app.routes.route_stops import route_stops_bp
    from app.routes.automated import automated_bp
    from app.routes.cloud_sync import cloud_sync_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(buses_bp)
    app.register_blueprint(journeys_bp)
    app.register_blueprint(uploads_bp)
    app.register_blueprint(passengers_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(map_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(route_stops_bp)
    app.register_blueprint(automated_bp)
    app.register_blueprint(cloud_sync_bp)

    # Data file serving (uploads, face crops, cloud thumbnails, with fallback placeholders)
    @app.route("/data/<path:filename>")
    def serve_data_file(filename):
        from flask import Response
        # Security: protect against directory traversal
        clean_fn = filename.replace("\\", "/").lstrip("/")
        if ".." in clean_fn:
            abort(403)

        base_data = Path(Config.DATA_DIR).resolve()
        safe_path = (Path(Config.DATA_DIR) / clean_fn).resolve()
        if str(safe_path).startswith(str(base_data)) and safe_path.is_file():
            return send_from_directory(Config.DATA_DIR, clean_fn)

        # Fallback 1: check inside data/crops
        base_name = os.path.basename(clean_fn)
        crops_p = Path(Config.DATA_DIR) / "crops" / base_name
        if crops_p.is_file():
            return send_from_directory(Path(Config.DATA_DIR) / "crops", base_name)

        # Fallback 2: check inside smartbus-data/journeys/*/thumbnails/
        sb_journeys = Path(Config.BASE_DIR) / "smartbus-data" / "journeys"
        if sb_journeys.is_dir():
            target_names = [base_name]
            if not base_name.endswith(".jpg"):
                target_names.append(f"{base_name}_face.jpg")
            stem = Path(base_name).stem.split("_")[0]
            if stem.startswith("P"):
                target_names.append(f"{stem}_face.jpg")

            for j_folder in sb_journeys.iterdir():
                t_folder = j_folder / "thumbnails"
                if t_folder.is_dir():
                    for tn in target_names:
                        tf = t_folder / tn
                        if tf.is_file():
                            return send_from_directory(t_folder, tn)

        # Fallback 3: check remote checksum mapping
        try:
            from app.services.cloud_sync_service import load_image_checksum_cache
            cache = load_image_checksum_cache()
            for entry in cache.values():
                if entry.get("orig_basename") == base_name or entry.get("filename") == base_name:
                    rem_url = entry.get("static_url") or entry.get("view_url")
                    if rem_url:
                        return redirect(rem_url)
        except Exception:
            pass

        # Fallback 4: Return clean inline SVG avatar placeholder (prevents broken image icons)
        pid = Path(clean_fn).stem.split("_")[0][:4].upper()
        if not pid or pid in ["NONE", "NULL", "THUMBNAIL"]:
            pid = "PAX"
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120" viewBox="0 0 120 120">
            <rect width="120" height="120" rx="16" fill="#1e293b"/>
            <circle cx="60" cy="48" r="22" fill="#334155"/>
            <path d="M26 102c0-18 15-32 34-32s34 14 34 32" fill="#334155"/>
            <text x="60" y="54" font-family="system-ui, -apple-system, sans-serif" font-size="14" font-weight="700" fill="#38bdf8" text-anchor="middle">{pid}</text>
        </svg>'''
        return Response(svg, mimetype="image/svg+xml")

    # UI Page Routes
    @app.route("/")
    def index():
        if "user_id" not in session:
            return redirect(url_for("page_login"))
        return redirect(url_for("page_dashboard"))

    @app.route("/login")
    def page_login():
        return render_template("login.html")

    @app.route("/dashboard")
    def page_dashboard():
        if "user_id" not in session:
            return redirect(url_for("page_login"))
        role = session.get("role", "operator")
        if role == "admin":
            return render_template("admin_dashboard.html", active_page="dashboard", role="admin")
        else:
            return render_template("operator_dashboard.html", active_page="dashboard", role="operator")

    @app.route("/buses")
    def page_buses():
        if "user_id" not in session:
            return redirect(url_for("page_login"))
        role = session.get("role", "operator")
        return render_template("buses.html", active_page="buses", role=role)

    @app.route("/operators")
    def page_operators():
        if "user_id" not in session:
            return redirect(url_for("page_login"))
        role = session.get("role", "operator")
        return render_template("operators.html", active_page="operators", role=role)

    @app.route("/upload")
    @app.route("/uploads")
    def page_upload():
        if "user_id" not in session:
            return redirect(url_for("page_login"))
        role = session.get("role", "operator")
        if role == "admin":
            return redirect(url_for("page_dashboard"))
        return render_template("upload.html", active_page="upload", role="operator")

    @app.route("/monitoring")
    def page_monitoring():
        if "user_id" not in session:
            return redirect(url_for("page_login"))
        role = session.get("role", "operator")
        return render_template("monitoring.html", active_page="monitoring", role=role)

    @app.route("/passengers")
    def page_passengers():
        if "user_id" not in session:
            return redirect(url_for("page_login"))
        role = session.get("role", "operator")
        return render_template("passengers.html", active_page="passengers", role=role)

    @app.route("/journeys")
    def page_journeys():
        if "user_id" not in session:
            return redirect(url_for("page_login"))
        role = session.get("role", "operator")
        return render_template("journeys.html", active_page="journeys", role=role)

    @app.route("/map")
    def page_map():
        if "user_id" not in session:
            return redirect(url_for("page_login"))
        role = session.get("role", "operator")
        return render_template("map.html", active_page="map", role=role)

    @app.route("/route-timeline")
    def page_route_timeline():
        if "user_id" not in session:
            return redirect(url_for("page_login"))
        role = session.get("role", "operator")
        return render_template("route_timeline.html", active_page="route_timeline", role=role)

    @app.route("/reports")
    def page_reports():
        if "user_id" not in session:
            return redirect(url_for("page_login"))
        role = session.get("role", "operator")
        return render_template("reports.html", active_page="reports", role=role)

    @app.route("/settings")
    def page_settings():
        if "user_id" not in session:
            return redirect(url_for("page_login"))
        role = session.get("role", "operator")
        return render_template("settings.html", active_page="settings", role=role)

    # Initialize MongoDB Atlas connection
    from app.mongodb import get_mongo_client, sync_document_to_mongo
    with app.app_context():
        get_mongo_client(timeout_ms=3000)
        _seed_initial_data()

    return app


def _seed_initial_data():
    """Seeds default admin, operator, and auditor credentials if empty."""
    from app.mongodb import sync_document_to_mongo
    if User.query.count() == 0:
        admin = User(username="admin", email="admin@smartbus.local", role="admin", full_name="System Administrator", consent_accepted=True)
        admin.set_password("admin123")
        
        operator = User(username="operator", email="operator@smartbus.local", role="operator", full_name="Bus Operator Dave", consent_accepted=True)
        operator.set_password("operator123")

        auditor = User(username="auditor", email="auditor@smartbus.local", role="auditor", full_name="Audit Officer Sarah", consent_accepted=True)
        auditor.set_password("auditor123")

        db.session.add_all([admin, operator, auditor])
        db.session.commit()

        # Sync users to MongoDB Atlas
        for u in [admin, operator, auditor]:
            sync_document_to_mongo("users", u.id, u.to_dict())

