from flask import Blueprint, jsonify, request
from app.models import GPSLocation, Upload, PassengerIdentity, Journey

map_bp = Blueprint("map", __name__, url_prefix="/api/map")

@map_bp.route("/points", methods=["GET"])
def get_map_points():
    journey_id = request.args.get("journey_id")
    
    query = GPSLocation.query.join(Upload)
    if journey_id:
        query = query.filter(GPSLocation.journey_id == journey_id)

    locations = query.order_by(GPSLocation.recorded_at.asc()).all()

    points = []
    for loc in locations:
        upload = loc.upload
        journey = upload.journey if upload else None
        bus = journey.bus if journey else None
        
        # Valid GPS only
        if loc.latitude is not None and loc.longitude is not None:
            points.append({
                "id": loc.id,
                "upload_id": loc.upload_id,
                "journey_code": journey.journey_code if journey else "N/A",
                "bus_number": bus.bus_number if bus else "N/A",
                "latitude": loc.latitude,
                "longitude": loc.longitude,
                "accuracy": loc.accuracy,
                "location_source": loc.location_source,
                "recorded_at": loc.recorded_at.strftime("%Y-%m-%d %H:%M:%S") if loc.recorded_at else None,
                "passengers_detected": upload.passengers_detected_count if upload else 0,
                "annotated_url": f"/data/{upload.annotated_path}" if upload and upload.annotated_path else None
            })

    # Also retrieve passenger boarding & last-confirmed locations
    p_query = PassengerIdentity.query
    if journey_id:
        p_query = p_query.filter_by(journey_id=journey_id)
    passengers = p_query.all()

    passenger_markers = []
    for p in passengers:
        if p.first_latitude is not None and p.first_longitude is not None:
            passenger_markers.append({
                "person_id": p.person_id,
                "status": p.status,
                "first_seen": {
                    "latitude": p.first_latitude,
                    "longitude": p.first_longitude,
                    "time": p.first_seen_time.strftime("%Y-%m-%d %H:%M:%S") if p.first_seen_time else None
                },
                "last_seen": {
                    "latitude": p.last_latitude,
                    "longitude": p.last_longitude,
                    "time": p.last_seen_time.strftime("%Y-%m-%d %H:%M:%S") if p.last_seen_time else None
                } if (p.last_latitude is not None and p.last_longitude is not None) else None
            })

    return jsonify({
        "total_points": len(points),
        "has_gps_data": len(points) > 0,
        "route_points": points,
        "passenger_markers": passenger_markers
    })
