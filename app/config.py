import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-bus-tracking-key-2026")
    BASE_DIR = BASE_DIR
    DATA_DIR = BASE_DIR / "data"
    UPLOAD_FOLDER = DATA_DIR / "uploads"
    CROP_FOLDER = DATA_DIR / "crops"

    # Database (SQL + MongoDB Atlas)
    DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'bus_tracker.db'}")
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "connect_args": {
            "timeout": 60,
            "check_same_thread": False
        }
    }

    # MongoDB Atlas Configuration
    MONGO_URI = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or ""
    MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "bus_tracker")

    # AI Matcher Thresholds
    SIMILARITY_HIGH_THRESHOLD = float(os.getenv("SIMILARITY_HIGH_THRESHOLD", "0.72"))
    SIMILARITY_MEDIUM_THRESHOLD = float(os.getenv("SIMILARITY_MEDIUM_THRESHOLD", "0.58"))
    SIMILARITY_LOW_THRESHOLD = float(os.getenv("SIMILARITY_LOW_THRESHOLD", "0.45"))
    MIN_FACE_SIZE = int(os.getenv("MIN_FACE_SIZE", "28"))
    BLUR_THRESHOLD = float(os.getenv("BLUR_THRESHOLD", "50.0"))

    # Multi-Angle Person Detection Thresholds
    DETECTION_CONF_HIGH = float(os.getenv("DETECTION_CONF_HIGH", "0.70"))
    DETECTION_CONF_MEDIUM = float(os.getenv("DETECTION_CONF_MEDIUM", "0.45"))
    DETECTION_CONF_LOW = float(os.getenv("DETECTION_CONF_LOW", "0.25"))
    YOLO_MODEL_PATH = os.getenv("YOLO_MODEL_PATH", "yolo11n.pt")

    # Temporal State Transition Thresholds
    MISSED_OBSERVATIONS_FOR_EXIT_UNCONFIRMED = int(os.getenv("MISSED_OBSERVATIONS_FOR_EXIT_UNCONFIRMED", "3"))
    MISSED_MINUTES_FOR_EXIT_UNCONFIRMED = int(os.getenv("MISSED_MINUTES_FOR_EXIT_UNCONFIRMED", "5"))

    # Color Filtering Configuration (Black & White vs Color Clip Gating)
    COLOR_SATURATION_THRESHOLD = float(os.getenv("COLOR_SATURATION_THRESHOLD", "8.0"))       # Mean HSV Saturation threshold (0-255)
    COLOR_CHANNEL_DIFF_THRESHOLD = float(os.getenv("COLOR_CHANNEL_DIFF_THRESHOLD", "3.5"))    # Mean |B-G| + |G-R| + |R-B| channel diff
    COLORFULNESS_METRIC_THRESHOLD = float(os.getenv("COLORFULNESS_METRIC_THRESHOLD", "9.5"))  # Hasler & Susstrunk metric threshold
    COLOR_ACTIVE_PIXEL_RATIO = float(os.getenv("COLOR_ACTIVE_PIXEL_RATIO", "0.03"))          # Min fraction of pixels with S > 25
    COLOR_FRAME_SAMPLE_COUNT = int(os.getenv("COLOR_FRAME_SAMPLE_COUNT", "5"))                # Frames to sample from video
    COLOR_MIN_COLOR_FRAME_RATIO = float(os.getenv("COLOR_MIN_COLOR_FRAME_RATIO", "0.60"))     # Ratio of sampled frames that must be color
    COLOR_UNCERTAIN_LOW = float(os.getenv("COLOR_UNCERTAIN_LOW", "6.5"))                      # Lower bound for uncertain color classification
    COLOR_UNCERTAIN_HIGH = float(os.getenv("COLOR_UNCERTAIN_HIGH", "11.5"))                    # Upper bound for uncertain color classification

    # Upload configuration
    MAX_CONTENT_LENGTH = 100 * 1024 * 1024  # 100 MB max upload size
    ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "mp4", "avi", "mov", "mkv", "wmv", "webm", "m4v"}

    # Timetable Nearest-Clip Matching & Fallback Configuration
    TIMETABLE_MATCHING_TOLERANCE_MINUTES = int(os.getenv("TIMETABLE_MATCHING_TOLERANCE_MINUTES", "3"))
    TIMETABLE_FALLBACK_STEP_MINUTES = int(os.getenv("TIMETABLE_FALLBACK_STEP_MINUTES", "1"))        # Configurable 1-minute incremental fallback step
    TIMETABLE_MAX_FALLBACK_MINUTES = int(os.getenv("TIMETABLE_MAX_FALLBACK_MINUTES", "30"))        # Maximum permitted search window (e.g. 30 min)

    # Privacy & Retention
    ENABLE_DATA_RETENTION_CLEANUP = os.getenv("ENABLE_DATA_RETENTION_CLEANUP", "True").lower() == "true"
    RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "30"))
    ANONYMIZE_FACE_CROPS = os.getenv("ANONYMIZE_FACE_CROPS", "False").lower() == "true"
    DEMO_MODE = False  # Strictly forbidden: no demo or simulated data

    # Remote Image Server & GitHub Cloud Sync Configuration
    REMOTE_IMAGE_SERVER_URL = os.getenv("REMOTE_IMAGE_SERVER_URL", "http://143.244.140.108:8060").rstrip("/")
    GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
    GITHUB_REPO = os.getenv("GITHUB_REPO", "Gaurav99940/SmartBus_AI")
    GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
    SMARTBUS_DATA_DIR = BASE_DIR / "smartbus-data"
    SMARTBUS_SYSTEM_ROLE = os.getenv("SMARTBUS_SYSTEM_ROLE", "local").lower()  # 'local' (runs AI) or 'live_server' (lightweight display only)
