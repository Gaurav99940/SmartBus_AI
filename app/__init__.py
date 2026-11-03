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

    # Data file serving (uploads, face crops)
    @app.route("/data/<path:filename>")
    def serve_data_file(filename):
        # Security: protect against directory traversal
        safe_path = Path(Config.DATA_DIR) / filename
        if not str(safe_path.resolve()).startswith(str(Config.DATA_DIR.resolve())):
            abort(403)
        return send_from_directory(Config.DATA_DIR, filename)

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

