from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request, session
from app.models import Bus, Journey, Upload, PassengerIdentity, PresenceEvent, User, ManualReview
from app.database import db

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/api/dashboard")

@dashboard_bp.route("/stats", methods=["GET"])
def get_stats():
    """
    Returns comprehensive bus monitoring dashboard data.
    Supports filtering by bus_id (e.g. ?bus_id=1 or ?bus_id=all).
    Calculates actual database metrics without hardcoded numbers or fake GPS.
    """
    bus_id_arg = request.args.get("bus_id")
    selected_bus_id = None
    if bus_id_arg and bus_id_arg.isdigit():
        selected_bus_id = int(bus_id_arg)

    now = datetime.utcnow()
    gps_fresh_cutoff = now - timedelta(minutes=10)
    activity_fresh_cutoff = now - timedelta(minutes=30)

    # 1. Total Registered Buses
    all_buses = Bus.query.order_by(Bus.created_at.desc()).all()
    total_registered_buses = len(all_buses)

    # 2. Active Buses (operational active: active status + active journey or recent activity < 30m)
    active_buses_count = sum(1 for b in all_buses if b.is_operational_active())

    # 3. Buses With Recent GPS (location update < 10 mins)
    buses_with_recent_gps = sum(1 for b in all_buses if b.latest_location_time and b.latest_location_time >= gps_fresh_cutoff and b.latest_latitude is not None)

    # Filter scope for passengers and events
    target_bus = None
    target_journey_ids = []

    if selected_bus_id:
        target_bus = Bus.query.get(selected_bus_id)
        if target_bus:
            target_journey_ids = [j.id for j in target_bus.journeys]
    else:
        # All buses
        target_journey_ids = [j.id for j in Journey.query.all()]

    # Passenger queries based on target journey IDs
    p_query = PassengerIdentity.query
    if selected_bus_id and target_bus:
        p_query = p_query.filter(PassengerIdentity.journey_id.in_(target_journey_ids)) if target_journey_ids else p_query.filter(db.false())
    
    passengers = p_query.all() if (not selected_bus_id or target_journey_ids) else []

    # 4. Total Passengers Identified (unique journey-scoped passenger identities)
    total_passengers_identified = len(passengers)

    # 5. Currently Confirmed Present
    currently_confirmed_present = sum(1 for p in passengers if p.status == "PRESENT")

    # 6. Total Confirmed Boardings
    # Distinct confirmed boarding events recorded in PresenceEvent
    ev_query = PresenceEvent.query
    if selected_bus_id and target_journey_ids:
        ev_query = ev_query.filter(PresenceEvent.journey_id.in_(target_journey_ids))
    elif selected_bus_id and not target_journey_ids:
        ev_query = ev_query.filter(db.false())

    confirmed_boardings_count = ev_query.filter_by(event_type="BOARDING_PROVISIONAL").count()

    # 7. Total Confirmed Exits (explicitly confirmed exits)
    confirmed_exits_count = sum(1 for p in passengers if p.status == "EXIT_CONFIRMED")
    # Also include presence events marked EXIT_CONFIRMED
    exit_events_count = ev_query.filter_by(event_type="EXIT_CONFIRMED").count()
    total_confirmed_exits = max(confirmed_exits_count, exit_events_count)

    # 8. Needs Verification (Needs Review or Exit Unconfirmed or Possibly Absent)
    needs_verification_count = sum(1 for p in passengers if p.status in ("NEEDS_REVIEW", "EXIT_UNCONFIRMED", "POSSIBLY_ABSENT"))

    # Active Buses & Journeys Table Data
    active_buses_list = []
    for b in all_buses:
        active_j = next((j for j in b.journeys if j.status == "active"), None)
        latest_upload = None
        if active_j:
            latest_upload = Upload.query.filter_by(journey_id=active_j.id).order_by(Upload.capture_timestamp.desc()).first()
            j_passengers = PassengerIdentity.query.filter_by(journey_id=active_j.id).all()
            pres_cnt = sum(1 for p in j_passengers if p.status == "PRESENT")
            abs_cnt = sum(1 for p in j_passengers if p.status in ("POSSIBLY_ABSENT", "EXIT_UNCONFIRMED"))
        else:
            pres_cnt = 0
            abs_cnt = 0

        op_name = b.operator_name or (b.assigned_operator.full_name or b.assigned_operator.username if b.assigned_operator else "Unassigned")

        active_buses_list.append({
            "id": b.id,
            "registration_number": b.registration_number or b.bus_number,
            "bus_name": b.bus_name or f"Bus {b.bus_number}",
            "route_name": b.route_name or f"{b.starting_point} - {b.destination}",
            "bus_status": b.bus_status,
            "is_operational_active": b.is_operational_active(),
            "operator_name": op_name,
            "journey_id": active_j.journey_code if active_j else "None",
            "journey_status": active_j.status if active_j else "Idle",
            "journey_start_time": active_j.start_time.strftime("%Y-%m-%d %H:%M:%S") if (active_j and active_j.start_time) else "-",
            "latest_gps_update": b.latest_location_time.strftime("%H:%M:%S") if b.latest_location_time else "No GPS",
            "latest_location_freshness": b.get_location_freshness(),
            "last_photo_time": latest_upload.capture_timestamp.strftime("%H:%M:%S") if (latest_upload and latest_upload.capture_timestamp) else "-",
            "confirmed_present_count": pres_cnt,
            "possible_absence_count": abs_cnt
        })

    # Recent Activity Feed (uploads, observations, events, manual reviews, GPS updates)
    recent_activity = []

    # 1. Recent Events
    recent_events = ev_query.order_by(PresenceEvent.event_time.desc()).limit(6).all()
    for e in recent_events:
        j = e.journey
        bus_reg = j.bus.registration_number if (j and j.bus) else "Bus"
        p_id = e.passenger.person_id if e.passenger else "Passenger"
        recent_activity.append({
            "type": "presence_event",
            "title": f"{p_id}: {e.event_type.replace('_', ' ')}",
            "description": e.notes or f"Event recorded on {bus_reg}",
            "bus_info": bus_reg,
            "timestamp": e.event_time.strftime("%Y-%m-%d %H:%M:%S") if e.event_time else "-",
            "badge_class": "badge-present" if "PRESENT" in e.event_type else ("badge-exit-confirmed" if "EXIT" in e.event_type else "badge-possibly-absent")
        })

    # 2. Recent Photo Uploads
    up_query = Upload.query
    if selected_bus_id and target_journey_ids:
        up_query = up_query.filter(Upload.journey_id.in_(target_journey_ids))
    recent_uploads = up_query.order_by(Upload.capture_timestamp.desc()).limit(4).all()
    for u in recent_uploads:
        j = u.journey
        bus_reg = j.bus.registration_number if (j and j.bus) else "Bus"
        angle_str = f" [Angle {u.camera_angle}]" if u.camera_angle else ""
        recent_activity.append({
            "type": "photo_upload",
            "title": f"Photo #{u.id}{angle_str} Processed",
            "description": f"Detected {u.passengers_detected_count} visible passengers. Location: {u.location_source}",
            "bus_info": bus_reg,
            "timestamp": u.capture_timestamp.strftime("%Y-%m-%d %H:%M:%S") if u.capture_timestamp else "-",
            "badge_class": "badge-present" if u.status == "processed" else "badge-needs-review"
        })

    # Sort recent activity chronologically
    recent_activity.sort(key=lambda x: x["timestamp"], reverse=True)
    recent_activity = recent_activity[:10]

    # Map Buses List (registered buses with valid coordinates)
    map_buses = []
    for b in all_buses:
        if b.latest_latitude is not None and b.latest_longitude is not None:
            active_j = next((j for j in b.journeys if j.status == "active"), None)
            j_passengers = PassengerIdentity.query.filter_by(journey_id=active_j.id).all() if active_j else []
            pres_cnt = sum(1 for p in j_passengers if p.status == "PRESENT")
            freshness = b.get_location_freshness()

            map_buses.append({
                "bus_id": b.id,
                "registration_number": b.registration_number or b.bus_number,
                "bus_name": b.bus_name or f"Bus {b.bus_number}",
                "route_name": b.route_name or f"{b.starting_point} - {b.destination}",
                "operator_name": b.operator_name or (b.assigned_operator.full_name or b.assigned_operator.username if b.assigned_operator else "Unassigned"),
                "latitude": b.latest_latitude,
                "longitude": b.latest_longitude,
                "accuracy": b.latest_gps_accuracy,
                "location_source": b.latest_location_source or "DEVICE_API",
                "location_time": b.latest_location_time.strftime("%Y-%m-%d %H:%M:%S") if b.latest_location_time else "-",
                "location_freshness": freshness, # 'Live', 'Stale', 'Offline', 'Manual Location'
                "bus_status": b.bus_status,
                "journey_code": active_j.journey_code if active_j else "None",
                "confirmed_passenger_count": pres_cnt,
                "is_selected": (b.id == selected_bus_id)
            })

    # Selected Bus Details & Passenger Table Data (if a specific bus is selected)
    selected_bus_details = None
    if target_bus:
        active_j = next((j for j in target_bus.journeys if j.status == "active"), None)
        passengers_list = []
        if active_j:
            p_records = PassengerIdentity.query.filter_by(journey_id=active_j.id).order_by(PassengerIdentity.person_id).all()
            for p in p_records:
                passengers_list.append({
                    "id": p.id,
                    "person_id": p.person_id,
                    "current_seat": p.get_seat_number(),
                    "previous_seat": p.previous_seat,
                    "first_seen_time": p.first_seen_time.strftime("%Y-%m-%d %H:%M:%S") if p.first_seen_time else "-",
                    "last_seen_time": p.last_seen_time.strftime("%Y-%m-%d %H:%M:%S") if p.last_seen_time else "-",
                    "status": p.status,
                    "boarding_status": "Confirmed" if p.total_observations > 0 else "Estimated",
                    "exit_status": "Confirmed" if p.status == "EXIT_CONFIRMED" else ("Unconfirmed" if p.status == "EXIT_UNCONFIRMED" else "Not Applicable"),
                    "confidence": "High" if p.status == "PRESENT" else ("Needs Review" if p.status == "NEEDS_REVIEW" else "Medium"),
                    "thumbnail_url": f"/data/{p.thumbnail_path}" if p.thumbnail_path else None
                })

        selected_bus_details = {
            "bus": target_bus.to_dict(),
            "active_journey": active_j.to_dict() if active_j else None,
            "passengers": passengers_list
        }

    return jsonify({
        "selected_bus_id": selected_bus_id,
        "selected_bus_details": selected_bus_details,
        "summary_cards": {
            "total_registered_buses": total_registered_buses,
            "active_buses_count": active_buses_count,
            "buses_with_recent_gps": buses_with_recent_gps,
            "total_passengers_identified": total_passengers_identified,
            "currently_confirmed_present": currently_confirmed_present,
            "total_confirmed_boardings": confirmed_boardings_count,
            "total_confirmed_exits": total_confirmed_exits,
            "needs_verification": needs_verification_count
        },
        "metrics": {
            "total_identified": total_passengers_identified,
            "confirmed_present": currently_confirmed_present,
            "possibly_absent": needs_verification_count,
            "needs_review": needs_verification_count,
            "active_buses": active_buses_count,
            "total_buses": total_registered_buses
        },
        "active_buses": active_buses_list,
        "recent_activity": recent_activity,
        "map_buses": map_buses,
        "has_gps_data": len(map_buses) > 0
    })
