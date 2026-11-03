from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, session
from app.database import db
from app.models import Bus, User, Journey, OperatorBusAssignment, BusLocationLog, GPSLocation, PassengerIdentity, PresenceEvent, Upload
from app.services.audit_service import log_audit

buses_bp = Blueprint("buses", __name__, url_prefix="/api/buses")

@buses_bp.route("", methods=["GET"])
def get_buses():
    """
    List buses. Admin sees all fleet vehicles; Operator strictly sees ONLY their own assigned/registered bus.
    """
    user_id = session.get("user_id")
    role = session.get("role", "operator")

    status_filter = request.args.get("status")
    query = Bus.query

    if status_filter:
        query = query.filter_by(bus_status=status_filter)

    if role == "operator":
        user = User.query.get(user_id) if user_id else None
        if user and user.current_bus_id:
            query = query.filter(Bus.id == user.current_bus_id)
        elif user:
            query = query.filter(Bus.assigned_operator_id == user.id)
        else:
            query = query.filter(Bus.id == -1)

    buses = query.order_by(Bus.created_at.desc()).all()
    results = []
    for b in buses:
        data = b.to_dict()
        # Include current confirmed passenger count if active journey
        active_j = next((j for j in b.journeys if j.status == "active"), None)
        if active_j:
            passengers = PassengerIdentity.query.filter_by(journey_id=active_j.id).all()
            data["confirmed_present_count"] = sum(1 for p in passengers if p.status == "PRESENT")
            data["possibly_absent_count"] = sum(1 for p in passengers if p.status in ("POSSIBLY_ABSENT", "EXIT_UNCONFIRMED"))
            data["total_journey_passengers"] = len(passengers)
        else:
            data["confirmed_present_count"] = 0
            data["possibly_absent_count"] = 0
            data["total_journey_passengers"] = 0
        results.append(data)

    return jsonify(results)


@buses_bp.route("/<int:bus_id>", methods=["GET"])
def get_bus(bus_id):
    """
    Get detailed bus information.
    """
    bus = Bus.query.get_or_404(bus_id)
    data = bus.to_dict()

    # Active journey info
    active_j = Journey.query.filter_by(bus_id=bus.id, status="active").order_by(Journey.start_time.desc()).first()
    if active_j:
        passengers = PassengerIdentity.query.filter_by(journey_id=active_j.id).all()
        data["active_journey"] = active_j.to_dict()
        data["passenger_stats"] = {
            "total_identified": len(passengers),
            "confirmed_present": sum(1 for p in passengers if p.status == "PRESENT"),
            "possibly_absent": sum(1 for p in passengers if p.status == "POSSIBLY_ABSENT"),
            "exit_unconfirmed": sum(1 for p in passengers if p.status == "EXIT_UNCONFIRMED"),
            "exit_confirmed": sum(1 for p in passengers if p.status == "EXIT_CONFIRMED"),
            "needs_review": sum(1 for p in passengers if p.status == "NEEDS_REVIEW")
        }
        # Include passenger items
        data["passengers"] = [p.to_dict() for p in passengers]
    else:
        data["active_journey"] = None
        data["passenger_stats"] = {
            "total_identified": 0, "confirmed_present": 0, "possibly_absent": 0,
            "exit_unconfirmed": 0, "exit_confirmed": 0, "needs_review": 0
        }
        data["passengers"] = []

    return jsonify(data)


@buses_bp.route("", methods=["POST"])
def register_bus():
    """
    Register or select a bus.
    If bus with registration_number exists, allows selecting it rather than creating a duplicate.
    """
    user_id = session.get("user_id")
    data = request.get_json() or {}

    reg_num = data.get("registration_number", "").strip().upper()
    bus_number = data.get("bus_number", "").strip().upper()
    bus_name = data.get("bus_name", "").strip()
    bus_type = data.get("bus_type", "Standard Transit").strip()
    capacity = int(data.get("capacity", 40) or 40)
    route_name = data.get("route_name", "").strip()
    route_number = data.get("route_number", "").strip()
    starting_point = data.get("starting_point", "").strip()
    destination = data.get("destination", "").strip()
    operator_name = data.get("operator_name", "").strip()
    operator_contact = data.get("operator_contact", "").strip()
    bus_status = data.get("bus_status", "Active").strip()

    if not reg_num and not bus_number:
        return jsonify({"error": "Bus Registration Number or Bus ID is required."}), 400

    # Look for existing bus by registration_number or bus_number
    existing_bus = None
    if reg_num:
        existing_bus = Bus.query.filter(
            (Bus.registration_number == reg_num) | (Bus.license_plate == reg_num)
        ).first()

    if not existing_bus and bus_number:
        existing_bus = Bus.query.filter_by(bus_number=bus_number).first()

    if existing_bus:
        # If user explicitly intended to create a brand new one but it exists
        if data.get("select_existing", False) or data.get("assign_only", False):
            # Assign existing bus to current operator
            _assign_operator_to_bus(user_id, existing_bus.id)
            return jsonify({
                "message": f"Assigned to existing bus {existing_bus.registration_number}.",
                "bus": existing_bus.to_dict(),
                "is_existing": True
            }), 200
        else:
            # Update existing bus details and assign
            if bus_name: existing_bus.bus_name = bus_name
            if bus_type: existing_bus.bus_type = bus_type
            if capacity: existing_bus.capacity = capacity
            if route_name: existing_bus.route_name = route_name
            if route_number: existing_bus.route_number = route_number
            if starting_point: existing_bus.starting_point = starting_point
            if destination: existing_bus.destination = destination
            if operator_name: existing_bus.operator_name = operator_name
            if operator_contact: existing_bus.operator_contact = operator_contact
            if bus_status: existing_bus.bus_status = bus_status

            # Update manual route stops if provided
            route_stops_data = data.get("route_stops")
            if route_stops_data and isinstance(route_stops_data, list):
                from app.models import RouteStop
                RouteStop.query.filter_by(bus_id=existing_bus.id).delete()
                for idx, s in enumerate(route_stops_data):
                    s_name = (s.get("stop_name") or s.get("name") or f"Stop #{idx + 1}").strip()
                    s_addr = (s.get("location_address") or s.get("address") or s_name).strip()
                    s_time = s.get("time_window") or s.get("time") or datetime.utcnow().strftime("%d-%m-%Y | %I:%M:%S %p")
                    s_speed = float(s.get("speed_kmh", s.get("speed", 5.0)) or 5.0)
                    s_lat = float(s.get("latitude", s.get("lat", 28.64720 + (idx * 0.0003))))
                    s_lon = float(s.get("longitude", s.get("lon", 77.31890 + (idx * 0.0007))))
                    s_major = bool(s.get("is_major_stop", s.get("major", idx == 0 or idx == len(route_stops_data) - 1)))
                    st = RouteStop(
                        bus_id=existing_bus.id,
                        stop_order=idx + 1,
                        stop_name=s_name,
                        location_address=s_addr,
                        time_window=s_time,
                        speed_kmh=s_speed,
                        latitude=s_lat,
                        longitude=s_lon,
                        is_major_stop=s_major,
                        created_at=datetime.utcnow()
                    )
                    db.session.add(st)

            _assign_operator_to_bus(user_id, existing_bus.id)
            db.session.commit()
            return jsonify({
                "message": f"Bus {existing_bus.registration_number} updated and selected.",
                "bus": existing_bus.to_dict(),
                "is_existing": True
            }), 200

    # Auto-generate bus_number if missing (guarantee uniqueness)
    if not bus_number:
        max_idx = 1
        all_bus_nums = [b.bus_number for b in Bus.query.all() if b.bus_number and b.bus_number.startswith("BUS-")]
        for bn in all_bus_nums:
            try:
                num = int(bn.split("-")[1])
                if num >= max_idx:
                    max_idx = num + 1
            except (IndexError, ValueError):
                pass
        bus_number = f"BUS-{max_idx:03d}"
        # Ensure it does not collide with any existing bus_number
        counter = max_idx
        while Bus.query.filter_by(bus_number=bus_number).first():
            counter += 1
            bus_number = f"BUS-{counter:03d}"

    # Auto-generate route_name if starting_point & destination provided
    if not route_name and starting_point and destination:
        route_name = f"{starting_point} - {destination}"
        if route_number:
            route_name = f"Route {route_number}: {route_name}"

    new_bus = Bus(
        bus_number=bus_number,
        registration_number=reg_num or bus_number,
        license_plate=reg_num or bus_number,
        bus_name=bus_name or f"Transit {bus_number}",
        bus_type=bus_type or "Standard Transit",
        capacity=capacity,
        route_name=route_name or "Standard City Route",
        route_number=route_number,
        starting_point=starting_point or "Civic Terminal",
        destination=destination or "Harbor Point",
        operator_name=operator_name,
        operator_contact=operator_contact,
        bus_status=bus_status or "Active",
        is_active=True,
        assigned_operator_id=user_id,
        created_at=datetime.utcnow()
    )
    db.session.add(new_bus)
    db.session.flush()

    # Save any manually defined route stops for this bus
    route_stops_data = data.get("route_stops")
    if route_stops_data and isinstance(route_stops_data, list):
        from app.models import RouteStop
        for idx, s in enumerate(route_stops_data):
            s_name = (s.get("stop_name") or s.get("name") or f"Stop #{idx + 1}").strip()
            s_addr = (s.get("location_address") or s.get("address") or s_name).strip()
            s_time = s.get("time_window") or s.get("time") or datetime.utcnow().strftime("%d-%m-%Y | %I:%M:%S %p")
            s_speed = float(s.get("speed_kmh", s.get("speed", 5.0)) or 5.0)
            s_lat = float(s.get("latitude", s.get("lat", 28.64720 + (idx * 0.0003))))
            s_lon = float(s.get("longitude", s.get("lon", 77.31890 + (idx * 0.0007))))
            s_major = bool(s.get("is_major_stop", s.get("major", idx == 0 or idx == len(route_stops_data) - 1)))
            st = RouteStop(
                bus_id=new_bus.id,
                stop_order=idx + 1,
                stop_name=s_name,
                location_address=s_addr,
                time_window=s_time,
                speed_kmh=s_speed,
                latitude=s_lat,
                longitude=s_lon,
                is_major_stop=s_major,
                created_at=datetime.utcnow()
            )
            db.session.add(st)

    _assign_operator_to_bus(user_id, new_bus.id)
    db.session.commit()

    try:
        from app.mongodb import sync_document_to_mongo
        sync_document_to_mongo("buses", new_bus.id, new_bus.to_dict())
    except Exception:
        pass

    log_audit("REGISTER_BUS", entity_type="Bus", entity_id=new_bus.id, details={
        "registration_number": new_bus.registration_number,
        "bus_name": new_bus.bus_name,
        "stops_added": len(route_stops_data) if route_stops_data else 0,
        "operator_id": user_id
    }, user_id=user_id)

    return jsonify({
        "message": "Bus registered successfully.",
        "bus": new_bus.to_dict(),
        "is_existing": False
    }), 201


@buses_bp.route("/<int:bus_id>/select", methods=["POST"])
def select_bus(bus_id):
    """
    Select an authorized bus as current operator's active bus.
    """
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    bus = Bus.query.get_or_404(bus_id)
    _assign_operator_to_bus(user_id, bus.id)
    db.session.commit()

    log_audit("SELECT_BUS", entity_type="Bus", entity_id=bus.id, details={"registration_number": bus.registration_number}, user_id=user_id)

    return jsonify({
        "message": f"Switched to bus {bus.registration_number}.",
        "bus": bus.to_dict()
    })


@buses_bp.route("/<int:bus_id>", methods=["DELETE"])
def delete_bus(bus_id):
    """
    Delete a registered bus and its associated route stops, assignments, and journeys.
    """
    user_id = session.get("user_id")

    bus = Bus.query.get(bus_id)
    if not bus:
        return jsonify({"error": f"Bus #{bus_id} not found."}), 404

    reg_num = bus.registration_number or bus.bus_number
    bus_name = bus.bus_name

    from app.models import RouteStop, BusLocationLog, OperatorBusAssignment, PresenceEvent, PassengerObservation, PassengerIdentity, GPSLocation, ManualReview, Upload

    # 1. Clean up route stops
    RouteStop.query.filter_by(bus_id=bus.id).delete()

    # 2. Clean up location logs
    BusLocationLog.query.filter_by(bus_id=bus.id).delete()

    # 3. Clean up operator assignments
    OperatorBusAssignment.query.filter_by(bus_id=bus.id).delete()

    # 4. Clear current_bus_id on any user pointing to this bus
    User.query.filter_by(current_bus_id=bus.id).update({"current_bus_id": None})

    # 5. Clean up journeys and child records
    for j in list(bus.journeys):
        PresenceEvent.query.filter_by(journey_id=j.id).delete()
        PassengerObservation.query.filter_by(journey_id=j.id).delete()
        PassengerIdentity.query.filter_by(journey_id=j.id).delete()
        GPSLocation.query.filter_by(journey_id=j.id).delete()
        ManualReview.query.filter_by(journey_id=j.id).delete()
        Upload.query.filter_by(journey_id=j.id).delete()
        db.session.delete(j)

    # 6. Delete the bus itself
    db.session.delete(bus)
    db.session.commit()

    log_audit("DELETE_BUS", entity_type="Bus", entity_id=bus_id, details={
        "registration_number": reg_num,
        "bus_name": bus_name,
        "deleted_by": user_id
    }, user_id=user_id)

    return jsonify({
        "success": True,
        "message": f"Bus {reg_num} ({bus_name}) was deleted successfully."
    })


@buses_bp.route("/operator/my-buses", methods=["GET"])
def get_operator_buses():
    """
    Returns list of buses assigned to the current operator.
    """
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    user = User.query.get(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    # Fetch assigned buses via OperatorBusAssignment and assigned_operator_id
    assignments = OperatorBusAssignment.query.filter_by(operator_id=user.id).all()
    assigned_bus_ids = set(a.bus_id for a in assignments)

    # Also include buses where assigned_operator_id == user.id
    direct_buses = Bus.query.filter_by(assigned_operator_id=user.id).all()
    for b in direct_buses:
        assigned_bus_ids.add(b.id)

    buses = Bus.query.filter(Bus.id.in_(list(assigned_bus_ids))).all() if assigned_bus_ids else []
    
    # If no buses assigned yet, return empty list
    return jsonify({
        "current_bus_id": user.current_bus_id,
        "buses": [b.to_dict() for b in buses]
    })


@buses_bp.route("/operator/location", methods=["POST"])
def update_operator_location():
    """
    Operator GPS update endpoint:
    Body:
    {
        "latitude": 37.7749,
        "longitude": -122.4194,
        "accuracy": 12.5,
        "location_source": "DEVICE_API" | "MANUAL_ENTRY",
        "bus_id": 1 (optional, uses user's current_bus_id)
    }
    """
    user_id = session.get("user_id")
    data = request.get_json() or {}

    try:
        lat = float(data.get("latitude"))
        lon = float(data.get("longitude"))
    except (TypeError, ValueError):
        return jsonify({"error": "Valid latitude and longitude are required."}), 400

    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return jsonify({"error": "Coordinates out of geographic range."}), 400

    accuracy = data.get("accuracy")
    try:
        accuracy = float(accuracy) if accuracy is not None else None
    except ValueError:
        accuracy = None

    source = data.get("location_source", "DEVICE_API")
    if source not in ("DEVICE_API", "MANUAL_ENTRY"):
        source = "DEVICE_API"

    # Find bus
    bus_id = data.get("bus_id")
    user = User.query.get(user_id) if user_id else None
    if not bus_id and user:
        bus_id = user.current_bus_id

    if not bus_id:
        # Default to first bus if available
        first_bus = Bus.query.first()
        bus_id = first_bus.id if first_bus else None

    if not bus_id:
        return jsonify({"error": "No bus associated with this location update."}), 400

    bus = Bus.query.get(bus_id)
    if not bus:
        return jsonify({"error": f"Bus #{bus_id} not found."}), 404

    now = datetime.utcnow()
    bus.latest_latitude = round(lat, 6)
    bus.latest_longitude = round(lon, 6)
    bus.latest_gps_accuracy = round(accuracy, 1) if accuracy else None
    bus.latest_location_source = source
    bus.latest_location_time = now
    bus.last_activity_time = now

    # Record location log
    active_j = Journey.query.filter_by(bus_id=bus.id, status="active").first()
    log_entry = BusLocationLog(
        bus_id=bus.id,
        operator_id=user_id,
        journey_id=active_j.id if active_j else None,
        latitude=round(lat, 6),
        longitude=round(lon, 6),
        accuracy=round(accuracy, 1) if accuracy else None,
        location_source=source,
        recorded_at=now
    )
    db.session.add(log_entry)

    # Also log to GPSLocation if active journey is present
    if active_j:
        latest_upload = Upload.query.filter_by(journey_id=active_j.id).order_by(Upload.capture_timestamp.desc()).first()
        gps_rec = GPSLocation(
            upload_id=latest_upload.id if latest_upload else 0,
            journey_id=active_j.id,
            latitude=round(lat, 6),
            longitude=round(lon, 6),
            accuracy=accuracy,
            location_source=source,
            recorded_at=now
        )
        db.session.add(gps_rec)

    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Location updated successfully.",
        "location": {
            "latitude": bus.latest_latitude,
            "longitude": bus.latest_longitude,
            "accuracy": bus.latest_gps_accuracy,
            "location_source": bus.latest_location_source,
            "location_time": bus.latest_location_time.strftime("%Y-%m-%d %H:%M:%S"),
            "freshness": bus.get_location_freshness()
        }
    })


def _assign_operator_to_bus(user_id, bus_id):
    """Helper to associate operator with a bus."""
    if not user_id:
        return
    user = User.query.get(user_id)
    if user:
        user.current_bus_id = bus_id
        db.session.add(user)

    # Check assignment record
    existing = OperatorBusAssignment.query.filter_by(operator_id=user_id, bus_id=bus_id).first()
    if not existing:
        assignment = OperatorBusAssignment(
            operator_id=user_id,
            bus_id=bus_id,
            is_primary=True,
            assigned_at=datetime.utcnow()
        )
        db.session.add(assignment)


@buses_bp.route("/operators", methods=["GET"])
def get_operators():
    """
    List all operators with their assigned bus details.
    """
    operators = User.query.filter_by(role="operator").all()
    results = []
    for op in operators:
        bus = Bus.query.get(op.current_bus_id) if op.current_bus_id else None
        
        # Get all assigned buses
        assignments = OperatorBusAssignment.query.filter_by(operator_id=op.id).all()
        assigned_buses = [Bus.query.get(a.bus_id).to_dict() for a in assignments if Bus.query.get(a.bus_id)]

        results.append({
            "id": op.id,
            "username": op.username,
            "full_name": op.full_name or op.username,
            "email": op.email,
            "role": op.role,
            "current_bus_id": op.current_bus_id,
            "current_bus": bus.to_dict() if bus else None,
            "assigned_buses": assigned_buses,
            "assigned_bus_count": len(assigned_buses),
            "consent_accepted": op.consent_accepted,
            "created_at": op.created_at.strftime("%Y-%m-%d %H:%M") if op.created_at else None,
            "last_login": op.last_login.strftime("%Y-%m-%d %H:%M") if op.last_login else None
        })
    return jsonify(results)


@buses_bp.route("/operators/assign", methods=["POST"])
def assign_operator_bus():
    """
    Assign an operator to a bus (Admin action).
    """
    role = session.get("role", "operator")
    data = request.get_json() or {}
    operator_id = data.get("operator_id")
    bus_id = data.get("bus_id")

    if not operator_id or not bus_id:
        return jsonify({"error": "operator_id and bus_id are required."}), 400

    operator = User.query.get(operator_id)
    if not operator:
        return jsonify({"error": "Operator not found."}), 404

    bus = Bus.query.get(bus_id)
    if not bus:
        return jsonify({"error": "Bus not found."}), 404

    _assign_operator_to_bus(operator_id, bus_id)
    bus.assigned_operator_id = operator_id
    bus.operator_name = operator.full_name or operator.username
    db.session.commit()

    log_audit("ASSIGN_OPERATOR", entity_type="Bus", entity_id=bus.id, details={
        "operator_id": operator_id,
        "operator_name": operator.username,
        "bus_number": bus.bus_number
    }, user_id=session.get("user_id"))

    return jsonify({"message": f"Assigned {operator.username} to {bus.registration_number or bus.bus_number}."})

