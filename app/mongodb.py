import os
import certifi
from datetime import datetime
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
from app.config import Config

_mongo_client = None
_mongo_db = None
_connection_status = {
    "connected": False,
    "last_checked": None,
    "error": None,
    "ping_ms": None
}


def get_mongo_uri():
    """Returns the MongoDB Atlas connection URI from config or env."""
    return os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or getattr(Config, "MONGO_URI", None) or ""


def is_valid_mongo_uri(uri):
    """Checks whether the URI is configured and not a placeholder."""
    if not uri or not isinstance(uri, str):
        return False
    u = uri.strip()
    if not (u.startswith("mongodb://") or u.startswith("mongodb+srv://")):
        return False
    # Check for unreplaced placeholders
    if "<username>" in u or "<password>" in u or "username:password" in u:
        return False
    return True


def mask_mongo_uri(uri):
    """Masks credentials for safe display in logs and UI."""
    if not uri:
        return ""
    try:
        if "@" in uri:
            prefix, rest = uri.split("@", 1)
            scheme = prefix.split("://")[0] + "://" if "://" in prefix else "mongodb+srv://"
            return f"{scheme}******:******@{rest}"
        return uri
    except Exception:
        return "mongodb+srv://******:******@..."


def get_mongo_client(timeout_ms=4000, force_reconnect=False):
    """
    Initializes and caches the PyMongo client with SSL/TLS and timeout safeguards.
    Uses certifi.where() for reliable Atlas connectivity on Windows.
    """
    global _mongo_client, _connection_status
    uri = get_mongo_uri()

    if not is_valid_mongo_uri(uri):
        _connection_status["connected"] = False
        _connection_status["error"] = "MONGO_URI is not set or contains placeholders (<username>:<password>)."
        _connection_status["last_checked"] = datetime.utcnow().isoformat()
        return None

    if _mongo_client is not None and not force_reconnect:
        return _mongo_client

    try:
        client = MongoClient(
            uri,
            tlsCAFile=certifi.where(),
            serverSelectionTimeoutMS=timeout_ms,
            connectTimeoutMS=timeout_ms,
            socketTimeoutMS=timeout_ms,
            appname="SmartBus-AI-Tracker"
        )
        # Test connection
        start_t = datetime.utcnow()
        client.admin.command("ping")
        elapsed = (datetime.utcnow() - start_t).total_seconds() * 1000

        _mongo_client = client
        _connection_status["connected"] = True
        _connection_status["error"] = None
        _connection_status["ping_ms"] = round(elapsed, 1)
        _connection_status["last_checked"] = datetime.utcnow().isoformat()

        # Ensure database and indexes
        init_mongo_indexes(client)
        print(f"[MongoDB Atlas] Successfully connected to cluster! Ping: {elapsed:.1f}ms")
        return _mongo_client

    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        _connection_status["connected"] = False
        _connection_status["error"] = f"Failed to connect to MongoDB Atlas: {str(e)}"
        _connection_status["last_checked"] = datetime.utcnow().isoformat()
        _mongo_client = None
        print(f"[MongoDB Atlas Warning] Connection error: {e}")
        return None
    except Exception as e:
        _connection_status["connected"] = False
        _connection_status["error"] = str(e)
        _connection_status["last_checked"] = datetime.utcnow().isoformat()
        _mongo_client = None
        print(f"[MongoDB Atlas Warning] Unexpected error: {e}")
        return None


def get_mongo_db():
    """Returns the MongoDB database instance."""
    client = get_mongo_client()
    if client is None:
        return None
    db_name = os.getenv("MONGO_DB_NAME", getattr(Config, "MONGO_DB_NAME", "bus_tracker"))
    return client[db_name]


def get_mongo_collection(collection_name):
    """Returns a specific collection from MongoDB Atlas or None if not connected."""
    db = get_mongo_db()
    if db is None:
        return None
    return db[collection_name]


def init_mongo_indexes(client=None):
    """Sets up indexes on MongoDB Atlas collections for performance."""
    if client is None:
        client = _mongo_client
    if client is None:
        return

    try:
        db_name = os.getenv("MONGO_DB_NAME", getattr(Config, "MONGO_DB_NAME", "bus_tracker"))
        db = client[db_name]

        # 1. Users
        db.users.create_index("username", unique=True)
        db.users.create_index("email")

        # 2. Buses
        db.buses.create_index("registration_number", unique=True, sparse=True)
        db.buses.create_index("bus_number")

        # 3. Journeys
        db.journeys.create_index("journey_code", unique=True, sparse=True)
        db.journeys.create_index("bus_id")
        db.journeys.create_index("status")

        # 4. Uploads
        db.uploads.create_index("journey_id")
        db.uploads.create_index("batch_id")
        db.uploads.create_index("camera_angle")

        # 5. Passenger Identities
        db.passenger_identities.create_index([("journey_id", 1), ("person_id", 1)], unique=True, sparse=True)
        db.passenger_identities.create_index("status")

        # 6. Passenger Observations
        db.passenger_observations.create_index([("journey_id", 1), ("passenger_identity_id", 1)])
        db.passenger_observations.create_index("upload_id")

        # 7. Presence Events
        db.presence_events.create_index([("journey_id", 1), ("passenger_identity_id", 1)])
        db.presence_events.create_index("event_time")

        # 8. GPS Locations
        db.gps_locations.create_index("journey_id")
        db.gps_locations.create_index("upload_id")

        # 9. Route Stops
        db.route_stops.create_index([("bus_id", 1), ("stop_order", 1)])

        # 10. Camera Angle Configs
        db.camera_angle_configs.create_index([("camera_angle", 1), ("bus_id", 1)], unique=True, sparse=True)

        # 11. Audit Logs
        db.audit_logs.create_index("created_at")
        db.audit_logs.create_index("action")

    except Exception as e:
        print(f"[MongoDB Atlas Warning] Index creation: {e}")


def sync_document_to_mongo(collection_name, doc_id, data_dict):
    """
    Upserts a document into MongoDB Atlas.
    Safe: fails silently with warning if MongoDB Atlas is not yet reachable.
    """
    col = get_mongo_collection(collection_name)
    if col is None:
        return False
    try:
        clean_doc = dict(data_dict)
        clean_doc["_id"] = str(doc_id)
        clean_doc["synced_at"] = datetime.utcnow()
        col.replace_one({"_id": str(doc_id)}, clean_doc, upsert=True)
        return True
    except Exception as e:
        print(f"[MongoDB Atlas Sync Warning] {collection_name} #{doc_id}: {e}")
        return False


def delete_document_from_mongo(collection_name, doc_id):
    """Removes a document from MongoDB Atlas."""
    col = get_mongo_collection(collection_name)
    if col is None:
        return False
    try:
        col.delete_one({"_id": str(doc_id)})
        return True
    except Exception as e:
        print(f"[MongoDB Atlas Delete Warning] {collection_name} #{doc_id}: {e}")
        return False


def delete_many_from_mongo(collection_name, filter_dict):
    """Deletes multiple documents matching filter from MongoDB Atlas."""
    col = get_mongo_collection(collection_name)
    if col is None:
        return 0
    try:
        res = col.delete_many(filter_dict)
        return res.deleted_count
    except Exception as e:
        print(f"[MongoDB Atlas DeleteMany Warning] {collection_name}: {e}")
        return 0


def get_mongo_status():
    """Returns rich status dictionary of MongoDB Atlas connection and collection statistics."""
    global _connection_status
    uri = get_mongo_uri()
    is_cfg = is_valid_mongo_uri(uri)
    masked = mask_mongo_uri(uri)
    db_name = os.getenv("MONGO_DB_NAME", getattr(Config, "MONGO_DB_NAME", "bus_tracker"))

    # Test fresh connection
    client = get_mongo_client(timeout_ms=3000)
    connected = (client is not None)

    collection_counts = {}
    if connected:
        try:
            db = client[db_name]
            for c_name in [
                "users", "buses", "journeys", "uploads", "passenger_identities",
                "passenger_observations", "presence_events", "gps_locations",
                "camera_angle_configs", "route_stops", "audit_logs"
            ]:
                collection_counts[c_name] = db[c_name].count_documents({})
        except Exception:
            pass

    return {
        "is_configured": is_cfg,
        "connected": connected,
        "masked_uri": masked if is_cfg else "Not Configured (Add MONGO_URI in .env)",
        "database_name": db_name,
        "ping_ms": _connection_status.get("ping_ms"),
        "last_checked": _connection_status.get("last_checked"),
        "error": _connection_status.get("error") if not connected else None,
        "collections": collection_counts,
        "total_documents": sum(collection_counts.values()) if collection_counts else 0
    }
