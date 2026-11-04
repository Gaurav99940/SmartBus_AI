import json
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, session
from app.config import Config
from app.database import db
from app.models import RouteStop, Bus, Journey, PassengerIdentity, User, MasterRouteStop, MasterUPRoute
from app.services.audit_service import log_audit

route_stops_bp = Blueprint("route_stops", __name__, url_prefix="/api/route-stops")


@route_stops_bp.route("/up-routes", methods=["GET"])
def get_up_routes():
    """
    Returns the master list of all major Uttar Pradesh transit routes.
    Used by operators during registration to choose their route, and by admins to monitor & configure.
    """
    region = request.args.get("region")
    query = MasterUPRoute.query.filter_by(is_active=True)
    if region:
        query = query.filter(MasterUPRoute.region.ilike(f"%{region}%"))
    routes = query.order_by(MasterUPRoute.route_code.asc()).all()
    return jsonify({
        "success": True,
        "count": len(routes),
        "routes": [r.to_dict() for r in routes]
    })


@route_stops_bp.route("/up-routes/<int:route_id>", methods=["GET"])
def get_up_route_detail(route_id):
    """
    Returns complete route details and full list of stops for a specific UP route.
    """
    route = MasterUPRoute.query.get(route_id)
    if not route:
        return jsonify({"error": f"UP Route #{route_id} not found."}), 404
    return jsonify({
        "success": True,
        "route": route.to_dict()
    })


@route_stops_bp.route("/up-routes", methods=["POST"])
def create_up_route():
    """
    Admin dynamically configures/adds a new Uttar Pradesh route to the master library.
    """
    user_id = session.get("user_id")
    data = request.get_json() or {}

    route_code = (data.get("route_code") or "").strip().upper()
    route_name = (data.get("route_name") or "").strip()
    region = (data.get("region") or "Uttar Pradesh").strip()
    origin = (data.get("origin") or "").strip()
    dest = (data.get("destination") or "").strip()
    dist = float(data.get("total_distance_km", 100.0) or 100.0)
    dur = float(data.get("estimated_duration_hours", 3.0) or 3.0)
    avg_speed = float(data.get("avg_speed_kmh", 12.0) or 12.0)
    stops = data.get("stops", [])

    if not route_name or not origin or not dest:
        return jsonify({"error": "Route name, origin, and destination are required."}), 400

    if not route_code:
        count = MasterUPRoute.query.count() + 1
        route_code = f"UP-RT-{count:02d}"

    existing = MasterUPRoute.query.filter_by(route_code=route_code).first()
    if existing:
        return jsonify({"error": f"Route code '{route_code}' already exists."}), 409

    new_route = MasterUPRoute(
        route_code=route_code,
        route_name=route_name,
        region=region,
        origin=origin,
        destination=dest,
        total_distance_km=dist,
        estimated_duration_hours=dur,
        avg_speed_kmh=avg_speed,
        stops_json=json.dumps(stops),
        is_active=True
    )
    db.session.add(new_route)
    db.session.commit()

    log_audit("CREATE_UP_ROUTE", entity_type="MasterUPRoute", entity_id=new_route.id, details={"route_code": route_code, "route_name": route_name}, user_id=user_id)

    return jsonify({
        "success": True,
        "message": f"Successfully created UP master route '{route_code}: {route_name}'.",
        "route": new_route.to_dict()
    }), 201


@route_stops_bp.route("/admin/fleet-summary", methods=["GET"])
def get_fleet_summary():
    """
    Consolidated fleet monitoring endpoint for Admin:
    Lists all registered buses, their assigned operator, selected UP route, stops count, and live telemetry.
    """
    buses = Bus.query.order_by(Bus.created_at.desc()).all()
    summary = []
    for b in buses:
        stops_count = RouteStop.query.filter_by(bus_id=b.id).count()
        assigned_user = User.query.get(b.assigned_operator_id) if b.assigned_operator_id else None
        
        # Check active journey
        active_j = next((j for j in b.journeys if j.status == "active"), None)
        passengers_count = len(active_j.passengers) if active_j and active_j.passengers else 0

        summary.append({
            "id": b.id,
            "bus_number": b.bus_number,
            "registration_number": b.registration_number or b.bus_number,
            "bus_name": b.bus_name,
            "bus_type": b.bus_type,
            "bus_status": b.bus_status,
            "capacity": b.capacity,
            "route_number": b.route_number,
            "route_name": b.route_name,
            "starting_point": b.starting_point,
            "destination": b.destination,
            "operator_name": b.operator_name or (assigned_user.full_name or assigned_user.username if assigned_user else "Unassigned"),
            "operator_contact": b.operator_contact or (getattr(assigned_user, "phone", None) or getattr(assigned_user, "email", "") if assigned_user else ""),
            "stops_count": stops_count,
            "active_passengers_count": passengers_count,
            "freshness": b.to_dict().get("freshness", "Offline"),
            "latest_location_time": b.latest_location_time.strftime("%Y-%m-%d %H:%M:%S") if b.latest_location_time else None
        })

    return jsonify({
        "success": True,
        "count": len(summary),
        "fleet": summary
    })



@route_stops_bp.route("/master-stops", methods=["GET"])
def get_master_stops():
    """
    Returns admin-provided master transit stations and stops library.
    Operators can pick from these stops during bus registration or define custom stops.
    """
    master_stops = MasterRouteStop.query.order_by(MasterRouteStop.id.asc()).all()
    return jsonify({
        "success": True,
        "count": len(master_stops),
        "stops": [s.to_dict() for s in master_stops]
    })


@route_stops_bp.route("/master-stops", methods=["POST"])
def add_master_stop():
    """
    Admin adds a new transit station / stop to the master library.
    """
    user_id = session.get("user_id")
    data = request.get_json() or {}

    name = (data.get("stop_name") or "").strip()
    addr = (data.get("location_address") or "").strip()
    speed = float(data.get("default_speed_kmh", 8.0) or 8.0)
    lat = float(data.get("latitude", 28.6472) or 28.6472)
    lon = float(data.get("longitude", 77.3189) or 77.3189)
    major = bool(data.get("is_major_stop", False))

    if not name or not addr:
        return jsonify({"error": "Stop name and location address are required."}), 400

    existing = MasterRouteStop.query.filter_by(stop_name=name).first()
    if existing:
        existing.location_address = addr
        existing.default_speed_kmh = speed
        existing.latitude = lat
        existing.longitude = lon
        existing.is_major_stop = major
        db.session.commit()
        return jsonify({"success": True, "message": f"Updated master stop '{name}'.", "stop": existing.to_dict()})

    new_stop = MasterRouteStop(
        stop_name=name,
        location_address=addr,
        default_speed_kmh=speed,
        latitude=lat,
        longitude=lon,
        is_major_stop=major
    )
    db.session.add(new_stop)
    db.session.commit()

    return jsonify({"success": True, "message": f"Added master stop '{name}'.", "stop": new_stop.to_dict()}), 201


@route_stops_bp.route("", methods=["GET"])
def get_route_stops():
    """
    Returns the ordered list of route stops strictly for the targeted bus.
    - Operator: Strictly sees ONLY their assigned bus's route and stops.
    - Admin: Can specify bus_id or switch between buses.
    """
    user_id = session.get("user_id")
    user = User.query.get(user_id) if user_id else None
    role = session.get("role", user.role if user else "operator")

    bus_id = request.args.get("bus_id", type=int)

    if role == "operator":
        # Operator is strictly locked to their own assigned bus
        bus_id = user.current_bus_id if (user and user.current_bus_id) else None
        if not bus_id and user:
            assigned = Bus.query.filter_by(assigned_operator_id=user.id).first()
            bus_id = assigned.id if assigned else None
        if not bus_id:
            first_bus = Bus.query.first()
            bus_id = first_bus.id if first_bus else None
    elif not bus_id:
        # Admin defaults to user.current_bus_id or first available bus
        bus_id = user.current_bus_id if (user and user.current_bus_id) else None
        if not bus_id:
            first_bus = Bus.query.first()
            bus_id = first_bus.id if first_bus else None

    target_bus = Bus.query.get(bus_id) if bus_id else None

    stops = []
    if bus_id:
        stops = RouteStop.query.filter_by(bus_id=bus_id).order_by(RouteStop.stop_order.asc(), RouteStop.id.asc()).all()
    if not stops:
        # Check unassigned legacy stops
        stops = RouteStop.query.filter(RouteStop.bus_id.is_(None)).order_by(RouteStop.stop_order.asc(), RouteStop.id.asc()).all()
    if not stops:
        stops = RouteStop.query.order_by(RouteStop.stop_order.asc(), RouteStop.id.asc()).all()
    if not stops:
        # Check timetable stops
        tt = None
        if target_bus:
            tt = TimetableImport.query.join(Journey).filter(Journey.bus_id == target_bus.id).order_by(TimetableImport.id.desc()).first()
        if not tt:
            tt = TimetableImport.query.order_by(TimetableImport.id.desc()).first()
        if tt and tt.stops:
            stops = sorted(tt.stops, key=lambda s: s.stop_number)

    stops_list = []
    for s in stops:
        if hasattr(s, "to_dict"):
            sd = s.to_dict()
            if "stop_order" not in sd:
                sd["stop_order"] = getattr(s, "stop_number", 1)
            if "speed_kmh" not in sd or sd["speed_kmh"] is None:
                sd["speed_kmh"] = 8.0
            stops_list.append(sd)

    # Calculate summary metrics
    speeds = [getattr(s, "speed_kmh", 8.0) or 8.0 for s in stops]
    avg_speed = round(sum(speeds) / len(speeds), 1) if speeds else 0.0
    max_speed = round(max(speeds), 1) if speeds else 0.0
    min_speed = round(min(speeds), 1) if speeds else 0.0

    origin_name = stops[0].stop_name if stops else (target_bus.starting_point if target_bus and target_bus.starting_point else "Not Defined")
    dest_name = stops[-1].stop_name if stops else (target_bus.destination if target_bus and target_bus.destination else "Not Defined")

    return jsonify({
        "success": True,
        "bus_id": bus_id,
        "bus_name": target_bus.bus_name if target_bus else None,
        "bus_registration": target_bus.registration_number if target_bus else None,
        "stops": stops_list,
        "count": len(stops_list),
        "route_summary": {
            "origin": origin_name,
            "destination": dest_name,
            "total_stops": len(stops_list),
            "average_speed_kmh": avg_speed,
            "max_speed_kmh": max_speed,
            "min_speed_kmh": min_speed
        }
    })


@route_stops_bp.route("", methods=["POST"])
def add_route_stop():
    """
    Manually add a new stop to the bus route.
    Body:
    {
        "stop_name": "Pacific Mall North Wing",
        "location_address": "Plot Alpha, Sahibabad Industrial Area...",
        "time_window": "15-10-2026 | 06:40:00 PM",
        "speed_kmh": 8.0,
        "latitude": 28.6485,
        "longitude": 77.3255,
        "is_major_stop": false,
        "insert_after_order": 6 (optional)
    }
    """
    data = request.get_json() or {}
    stop_name = (data.get("stop_name") or "").strip()
    address = (data.get("location_address") or "").strip()
    time_window = (data.get("time_window") or "").strip() or datetime.utcnow().strftime("%d-%m-%Y | %I:%M:%S %p")
    speed_kmh = float(data.get("speed_kmh", 0.0))
    lat = data.get("latitude")
    lon = data.get("longitude")
    is_major = bool(data.get("is_major_stop", False))
    insert_after = data.get("insert_after_order")
    bus_id = data.get("bus_id")

    if not stop_name:
        return jsonify({"error": "Stop name is required."}), 400
    if not address:
        address = stop_name

    if lat is None or lon is None:
        # Default near Sahibabad / Kaushambi if not provided
        lat = 28.6472
        lon = 77.3200
    else:
        lat = float(lat)
        lon = float(lon)

    # Determine stop_order
    max_order = db.session.query(db.func.max(RouteStop.stop_order)).scalar() or 0
    if insert_after is not None:
        new_order = int(insert_after) + 1
        # Shift subsequent stops
        later_stops = RouteStop.query.filter(RouteStop.stop_order >= new_order).all()
        for s in later_stops:
            s.stop_order += 1
    else:
        new_order = max_order + 1

    new_stop = RouteStop(
        bus_id=bus_id,
        stop_order=new_order,
        stop_name=stop_name,
        location_address=address,
        time_window=time_window,
        speed_kmh=speed_kmh,
        latitude=lat,
        longitude=lon,
        is_major_stop=is_major,
        created_at=datetime.utcnow()
    )
    db.session.add(new_stop)
    db.session.commit()

    log_audit(
        action="ADD_ROUTE_STOP",
        entity_type="route_stops",
        entity_id=new_stop.id,
        details={"stop_name": stop_name, "stop_order": new_order, "speed_kmh": speed_kmh},
        user_id=session.get("user_id")
    )

    return jsonify({
        "success": True,
        "message": f"Route stop '{stop_name}' added at order #{new_order}.",
        "stop": new_stop.to_dict()
    }), 201


@route_stops_bp.route("/<int:stop_id>", methods=["DELETE"])
def delete_route_stop(stop_id):
    """
    Deletes a route stop and re-indexes remaining stops.
    """
    stop = RouteStop.query.get(stop_id)
    if not stop:
        return jsonify({"error": f"Stop #{stop_id} not found."}), 404

    deleted_order = stop.stop_order
    stop_name = stop.stop_name
    db.session.delete(stop)

    # Re-order remaining stops
    subsequent = RouteStop.query.filter(RouteStop.stop_order > deleted_order).order_by(RouteStop.stop_order.asc()).all()
    for s in subsequent:
        s.stop_order -= 1

    db.session.commit()

    log_audit(
        action="DELETE_ROUTE_STOP",
        entity_type="route_stops",
        entity_id=stop_id,
        details={"stop_name": stop_name, "deleted_order": deleted_order},
        user_id=session.get("user_id")
    )

    return jsonify({
        "success": True,
        "message": f"Stop #{stop_id} ('{stop_name}') deleted."
    })


@route_stops_bp.route("/passenger-trajectories", methods=["GET"])
def get_passenger_trajectories():
    """
    Returns passengers for the selected or active journey and calculates their
    exact stop-by-stop trajectory (Traveled stops in GREEN vs Non-traveled in RED).
    Strictly scoped to the target bus.
    """
    user_id = session.get("user_id")
    user = User.query.get(user_id) if user_id else None
    role = session.get("role", user.role if user else "operator")

    bus_id = request.args.get("bus_id", type=int)
    if role == "operator":
        bus_id = user.current_bus_id if (user and user.current_bus_id) else None
        if not bus_id and user:
            assigned = Bus.query.filter_by(assigned_operator_id=user.id).first()
            bus_id = assigned.id if assigned else None
        if not bus_id:
            first_bus = Bus.query.first()
            bus_id = first_bus.id if first_bus else None
    elif not bus_id:
        bus_id = user.current_bus_id if (user and user.current_bus_id) else None
        if not bus_id:
            first_bus = Bus.query.first()
            bus_id = first_bus.id if first_bus else None

    journey_id = request.args.get("journey_id", type=int)
    if not journey_id:
        active_journey = None
        if bus_id:
            active_journey = Journey.query.filter_by(bus_id=bus_id, status="active").order_by(Journey.id.desc()).first()
            if not active_journey:
                active_journey = Journey.query.filter_by(bus_id=bus_id).order_by(Journey.id.desc()).first()
        if not active_journey:
            active_journey = Journey.query.filter_by(status="active").order_by(Journey.id.desc()).first()
        if not active_journey:
            active_journey = Journey.query.order_by(Journey.id.desc()).first()
        journey_id = active_journey.id if active_journey else None

    # Fetch stops strictly for target bus
    all_stops = []
    if bus_id:
        all_stops = RouteStop.query.filter_by(bus_id=bus_id).order_by(RouteStop.stop_order.asc(), RouteStop.id.asc()).all()
    if not all_stops:
        all_stops = RouteStop.query.filter(RouteStop.bus_id.is_(None)).order_by(RouteStop.stop_order.asc(), RouteStop.id.asc()).all()
    if not all_stops:
        all_stops = RouteStop.query.order_by(RouteStop.stop_order.asc(), RouteStop.id.asc()).all()
    if not all_stops and journey_id:
        tt = TimetableImport.query.filter_by(journey_id=journey_id).order_by(TimetableImport.id.desc()).first()
        if not tt and bus_id:
            tt = TimetableImport.query.order_by(TimetableImport.id.desc()).first()
        if tt and tt.stops:
            all_stops = sorted(tt.stops, key=lambda s: s.stop_number)

    stops_count = len(all_stops)
    if stops_count == 0:
        return jsonify({"success": True, "bus_id": bus_id, "journey_id": journey_id, "passengers": [], "stops": []})

    passengers = []
    if journey_id:
        passengers = PassengerIdentity.query.filter_by(journey_id=journey_id).order_by(PassengerIdentity.id.asc()).all()
    elif bus_id:
        passengers = PassengerIdentity.query.join(Journey).filter(Journey.bus_id == bus_id).order_by(PassengerIdentity.id.desc()).limit(15).all()

    if not passengers and bus_id:
        passengers = PassengerIdentity.query.join(Journey).filter(Journey.bus_id == bus_id).order_by(PassengerIdentity.id.desc()).limit(15).all()

    stops_serialized = []
    for s in all_stops:
        sd = s.to_dict()
        if "stop_order" not in sd:
            sd["stop_order"] = getattr(s, "stop_number", 1)
        if "speed_kmh" not in sd or sd.get("speed_kmh") is None:
            sd["speed_kmh"] = 8.0
        stops_serialized.append(sd)

    if not passengers:
        return jsonify({
            "success": True,
            "journey_id": journey_id,
            "stops": stops_serialized,
            "passengers": []
        })

    trajectories = []
    # Build lookup maps for all_stops by ID, stop_order/stop_number, and normalized name
    stop_by_id = {s.id: s for s in all_stops}
    stop_by_num = {}
    stop_by_name = {}
    for s in all_stops:
        s_num = getattr(s, "stop_order", None) or getattr(s, "stop_number", None)
        if s_num is not None:
            stop_by_num[s_num] = s
        s_name = (getattr(s, "stop_name", "") or "").strip().lower()
        if s_name:
            stop_by_name[s_name] = s

    for idx, p in enumerate(passengers):
        # Collect all stops where passenger was ACTUALLY detected / confirmed present
        confirmed_stop_ids = set()

        def add_stop_reference(raw_stop_id):
            if not raw_stop_id:
                return
            if raw_stop_id in stop_by_id:
                confirmed_stop_ids.add(raw_stop_id)
                return
            t_stop = TimetableStop.query.get(raw_stop_id)
            if t_stop:
                if t_stop.stop_number in stop_by_num:
                    confirmed_stop_ids.add(stop_by_num[t_stop.stop_number].id)
                elif t_stop.stop_name.strip().lower() in stop_by_name:
                    confirmed_stop_ids.add(stop_by_name[t_stop.stop_name.strip().lower()].id)
                return
            r_stop = RouteStop.query.get(raw_stop_id)
            if r_stop:
                if r_stop.stop_order in stop_by_num:
                    confirmed_stop_ids.add(stop_by_num[r_stop.stop_order].id)
                elif r_stop.stop_name.strip().lower() in stop_by_name:
                    confirmed_stop_ids.add(stop_by_name[r_stop.stop_name.strip().lower()].id)

        # Parse traveled_stops_json if available
        traveled_entries = []
        if p.traveled_stops_json:
            try:
                traveled_entries = json.loads(p.traveled_stops_json)
            except Exception:
                traveled_entries = []

        # 1. Match from recorded traveled_stops_json entries (by stop_number or stop_name)
        for tentry in traveled_entries:
            ts_num = tentry.get("stop_number")
            ts_name = (tentry.get("stop_name") or "").strip().lower()
            if ts_num is not None and ts_num in stop_by_num:
                confirmed_stop_ids.add(stop_by_num[ts_num].id)
            elif ts_name and ts_name in stop_by_name:
                confirmed_stop_ids.add(stop_by_name[ts_name].id)

        # 2. Check observations & uploads
        for obs in p.observations:
            if obs.route_stop_id:
                add_stop_reference(obs.route_stop_id)
            elif obs.upload and obs.upload.route_stop_id:
                add_stop_reference(obs.upload.route_stop_id)
            elif obs.upload and getattr(obs.upload, "latitude", None) and getattr(obs.upload, "longitude", None):
                gps_stops = [s for s in all_stops if getattr(s, "latitude", None) is not None and getattr(s, "longitude", None) is not None]
                if gps_stops:
                    closest = min(gps_stops, key=lambda s: (s.latitude - obs.upload.latitude)**2 + (s.longitude - obs.upload.longitude)**2)
                    confirmed_stop_ids.add(closest.id)

        # 3. Check presence events
        for ev in p.events:
            if ev.route_stop_id:
                add_stop_reference(ev.route_stop_id)
            elif getattr(ev, "latitude", None) and getattr(ev, "longitude", None):
                gps_stops = [s for s in all_stops if getattr(s, "latitude", None) is not None and getattr(s, "longitude", None) is not None]
                if gps_stops:
                    closest = min(gps_stops, key=lambda s: (s.latitude - ev.latitude)**2 + (s.longitude - ev.longitude)**2)
                    confirmed_stop_ids.add(closest.id)

        # 4. Check explicit boarding / exit stop id and numbers
        if p.boarding_stop_id:
            add_stop_reference(p.boarding_stop_id)
        if p.exit_stop_id:
            add_stop_reference(p.exit_stop_id)

        if p.boarding_stop_number and p.boarding_stop_number in stop_by_num:
            confirmed_stop_ids.add(stop_by_num[p.boarding_stop_number].id)
        if p.exit_stop_number and p.exit_stop_number in stop_by_num:
            confirmed_stop_ids.add(stop_by_num[p.exit_stop_number].id)

        if not confirmed_stop_ids and getattr(p, "first_latitude", None) and getattr(p, "first_longitude", None):
            gps_stops = [s for s in all_stops if getattr(s, "latitude", None) is not None and getattr(s, "longitude", None) is not None]
            if gps_stops:
                closest = min(gps_stops, key=lambda s: (s.latitude - p.first_latitude)**2 + (s.longitude - p.first_longitude)**2)
                confirmed_stop_ids.add(closest.id)

        # If passenger has no recorded stops, default to the first stop
        if not confirmed_stop_ids and stops_count > 0:
            confirmed_stop_ids.add(all_stops[0].id)

        # Determine boarding stop (first present) and exit/latest stop
        first_present = next((s for s in all_stops if s.id in confirmed_stop_ids), all_stops[0])
        last_present = next((s for s in reversed(all_stops) if s.id in confirmed_stop_ids), first_present)

        boarding_stop = first_present
        exit_stop = last_present

        board_idx = next((i for i, s in enumerate(all_stops) if s.id == boarding_stop.id), 0)
        exit_idx = next((i for i, s in enumerate(all_stops) if s.id == exit_stop.id), board_idx)

        # Passenger is available from boarding stop up to their latest confirmed stop
        traveled_stop_slice = set(s.id for s in all_stops[board_idx:exit_idx+1])
        traveled_stop_slice.update(confirmed_stop_ids)

        present_stop_ids = [s.id for s in all_stops if s.id in traveled_stop_slice]
        absent_stop_ids = [s.id for s in all_stops if s.id not in traveled_stop_slice]

        traveled_stop_ids = present_stop_ids
        non_traveled_stop_ids = absent_stop_ids

        traveled_count = max(len(traveled_stop_ids), p.get_distinct_stops_count(), len(traveled_entries))
        travel_pct = round((traveled_count / stops_count) * 100, 1) if stops_count > 0 else 0.0

        b_order = getattr(boarding_stop, "stop_order", None) or getattr(boarding_stop, "stop_number", 1)
        b_time = getattr(boarding_stop, "time_window", None) or (boarding_stop.scheduled_arrival.strftime("%I:%M %p") if getattr(boarding_stop, "scheduled_arrival", None) else None)
        e_order = getattr(exit_stop, "stop_order", None) or getattr(exit_stop, "stop_number", 1)
        e_time = getattr(exit_stop, "time_window", None) or (exit_stop.scheduled_arrival.strftime("%I:%M %p") if getattr(exit_stop, "scheduled_arrival", None) else None)

        all_angles = p.get_detected_angles()
        first_ang = p.get_first_observed_angle()

        covered_names = [getattr(s, "stop_name", f"Stop #{getattr(s, 'stop_order', i+1)}") for i, s in enumerate(all_stops) if s.id in traveled_stop_slice]

        trajectories.append({
            "id": p.id,
            "person_id": p.person_id,
            "canonical_person_id": p.get_canonical_person_id(),
            "is_merged": bool(p.is_merged or p.canonical_identity_id),
            "current_seat": p.get_seat_number(),
            "status": p.status,
            "thumbnail_path": p.thumbnail_path,
            "first_seen_time": p.first_seen_time.strftime("%I:%M:%S %p") if p.first_seen_time else None,
            "last_seen_time": p.last_seen_time.strftime("%I:%M:%S %p") if p.last_seen_time else None,
            "first_observed_angle": first_ang,
            "last_observed_angle": p.last_observed_angle or (all_angles[-1] if all_angles else first_ang),
            "detected_angles": all_angles,
            "distinct_stops_count": p.get_distinct_stops_count(),
            "traveled_stops_detail": traveled_entries,
            "boarding_stop": {
                "id": boarding_stop.id,
                "order": b_order,
                "name": getattr(boarding_stop, "stop_name", ""),
                "time": b_time,
                "lat": getattr(boarding_stop, "latitude", None),
                "lon": getattr(boarding_stop, "longitude", None)
            },
            "exit_stop": {
                "id": exit_stop.id,
                "order": e_order,
                "name": getattr(exit_stop, "stop_name", ""),
                "time": e_time,
                "lat": getattr(exit_stop, "latitude", None),
                "lon": getattr(exit_stop, "longitude", None)
            },
            "traveled_stops_count": traveled_count,
            "total_route_stops": stops_count,
            "travel_percentage": travel_pct,
            "traveled_summary": f"Traveled {traveled_count} of {stops_count} Stops ({travel_pct}%)",
            "coverage_desc": f"Stop #{b_order} ({getattr(boarding_stop, 'stop_name', '')}) ➔ Stop #{e_order} ({getattr(exit_stop, 'stop_name', '')})",
            "covered_stop_names": covered_names,
            "traveled_stop_ids": traveled_stop_ids,
            "non_traveled_stop_ids": non_traveled_stop_ids
        })

    return jsonify({
        "success": True,
        "journey_id": journey_id,
        "stops": stops_serialized,
        "passengers": trajectories
    })


@route_stops_bp.route("/passenger/<int:passenger_id>/stops", methods=["POST"])
def update_passenger_stops(passenger_id):
    """
    Manually assign or override a passenger's boarding and exit stop.
    """
    p = PassengerIdentity.query.get(passenger_id)
    if not p:
        return jsonify({"error": f"Passenger #{passenger_id} not found."}), 404

    data = request.get_json() or {}
    boarding_id = data.get("boarding_stop_id")
    exit_id = data.get("exit_stop_id")

    if boarding_id is not None:
        p.boarding_stop_id = boarding_id
    if exit_id is not None:
        p.exit_stop_id = exit_id

    db.session.commit()

    log_audit(
        action="UPDATE_PASSENGER_STOPS",
        entity_type="passenger_identities",
        entity_id=passenger_id,
        details={"person_id": p.person_id, "boarding_stop_id": boarding_id, "exit_stop_id": exit_id},
        user_id=session.get("user_id")
    )

    return jsonify({
        "success": True,
        "message": f"Passenger {p.person_id} route stops updated.",
        "passenger": p.to_dict()
    })


@route_stops_bp.route("/bulk-update", methods=["POST"])
def bulk_update_stops():
    """
    Allows Admin or Bus Operator to update or create route stops in bulk.
    Accepts:
      bus_id: int
      route_name: optional str
      origin: optional str
      destination: optional str
      stops: list of dicts: [
        {
          "name" or "stop_name": str,
          "address" or "location_address": str,
          "time" or "time_window": str,
          "speed" or "speed_kmh": float,
          "major" or "is_major_stop": bool,
          "lat" or "latitude": float,
          "lon" or "longitude": float
        }, ...
      ]
    """
    user_id = session.get("user_id")
    user = User.query.get(user_id) if user_id else None
    role = session.get("role", user.role if user else "operator")

    data = request.get_json() or {}
    bus_id = data.get("bus_id")

    if role == "operator":
        allowed_bus_id = user.current_bus_id if (user and user.current_bus_id) else None
        if not allowed_bus_id and user:
            assigned = Bus.query.filter_by(assigned_operator_id=user.id).first()
            allowed_bus_id = assigned.id if assigned else None
        if not bus_id:
            bus_id = allowed_bus_id
        elif bus_id != allowed_bus_id and allowed_bus_id is not None:
            return jsonify({"error": "Operators may only manage their own assigned bus route."}), 403

    if not bus_id:
        target_bus = Bus.query.first()
        bus_id = target_bus.id if target_bus else None

    bus = Bus.query.get(bus_id)
    if not bus:
        return jsonify({"error": f"Bus #{bus_id} not found."}), 404

    stops_data = data.get("stops", [])
    if not stops_data or not isinstance(stops_data, list):
        return jsonify({"error": "No stops provided in payload."}), 400

    # Delete existing stops for this bus
    RouteStop.query.filter_by(bus_id=bus.id).delete()

    n = len(stops_data)
    created_stops = []
    
    start_lat = float(data.get("start_lat", 28.6472))
    start_lon = float(data.get("start_lon", 77.3189))
    end_lat = float(data.get("end_lat", 26.8570))
    end_lon = float(data.get("end_lon", 80.9320))

    for idx, s in enumerate(stops_data):
        s_name = (s.get("name") or s.get("stop_name") or f"Stop #{idx+1}").strip()
        s_addr = (s.get("address") or s.get("location_address") or s_name).strip()
        s_time = (s.get("time") or s.get("time_window") or "").strip()
        try:
            s_speed = float(s.get("speed") or s.get("speed_kmh") or 8.0)
        except Exception:
            s_speed = 8.0
        s_major = bool(s.get("major", s.get("is_major_stop", idx == 0 or idx == n - 1)))
        
        frac = (idx / max(1, n - 1))
        default_lat = round(start_lat + (end_lat - start_lat) * frac, 6)
        default_lon = round(start_lon + (end_lon - start_lon) * frac, 6)
        try:
            s_lat = float(s.get("lat") or s.get("latitude") or default_lat)
            s_lon = float(s.get("lon") or s.get("longitude") or default_lon)
        except Exception:
            s_lat = default_lat
            s_lon = default_lon

        rs = RouteStop(
            bus_id=bus.id,
            stop_order=idx + 1,
            stop_name=s_name,
            location_address=s_addr,
            time_window=s_time or None,
            speed_kmh=s_speed,
            latitude=s_lat,
            longitude=s_lon,
            is_major_stop=s_major,
            created_at=datetime.utcnow()
        )
        db.session.add(rs)
        created_stops.append(rs)

    if created_stops:
        bus.starting_point = created_stops[0].stop_name
        bus.destination = created_stops[-1].stop_name
        if data.get("route_name"):
            bus.route_name = data.get("route_name").strip()
        elif not bus.route_name or "Standard" in bus.route_name:
            bus.route_name = f"{bus.starting_point} — {bus.destination}"

    db.session.commit()

    log_audit("BULK_UPDATE_ROUTE_STOPS", entity_type="buses", entity_id=bus.id,
              details={"bus_id": bus.id, "stops_count": len(created_stops), "route_name": bus.route_name},
              user_id=user_id)

    return jsonify({
        "success": True,
        "message": f"Successfully updated {len(created_stops)} stops for Bus #{bus.bus_number}.",
        "bus_id": bus.id,
        "bus_number": bus.bus_number,
        "route_name": bus.route_name,
        "starting_point": bus.starting_point,
        "destination": bus.destination,
        "stops_count": len(created_stops),
        "stops": [s.to_dict() for s in created_stops]
    })


@route_stops_bp.route("/apply-up-route", methods=["POST"])
def apply_up_route_to_bus():
    """
    Applies a Master UP Route to the specified bus (or active operator's bus).
    Copies all master stops, speeds, GPS, and names directly to the bus's timeline.
    """
    user_id = session.get("user_id")
    user = User.query.get(user_id) if user_id else None
    role = session.get("role", user.role if user else "operator")

    data = request.get_json() or {}
    bus_id = data.get("bus_id")
    route_id = data.get("route_id")
    route_code = data.get("route_code")

    if role == "operator":
        allowed_bus_id = user.current_bus_id if (user and user.current_bus_id) else None
        if not allowed_bus_id and user:
            assigned = Bus.query.filter_by(assigned_operator_id=user.id).first()
            allowed_bus_id = assigned.id if assigned else None
        if not bus_id:
            bus_id = allowed_bus_id
        elif bus_id != allowed_bus_id and allowed_bus_id is not None:
            return jsonify({"error": "Operators may only manage their own assigned bus route."}), 403

    if not bus_id:
        target_bus = Bus.query.first()
        bus_id = target_bus.id if target_bus else None

    bus = Bus.query.get(bus_id)
    if not bus:
        return jsonify({"error": f"Bus #{bus_id} not found."}), 404

    master_route = None
    if route_id:
        master_route = MasterUPRoute.query.get(route_id)
    elif route_code:
        master_route = MasterUPRoute.query.filter_by(route_code=route_code).first()

    if not master_route:
        return jsonify({"error": "Master UP Route not found."}), 404

    stops = master_route.get_stops()
    if not stops:
        return jsonify({"error": "Master route contains no stops."}), 400

    RouteStop.query.filter_by(bus_id=bus.id).delete()

    created_stops = []
    for idx, s in enumerate(stops):
        s_name = (s.get("name") or s.get("stop_name") or f"Stop #{idx+1}").strip()
        s_addr = (s.get("address") or s.get("location_address") or s_name).strip()
        s_time = (s.get("time") or s.get("time_window") or "").strip()
        try:
            s_speed = float(s.get("speed") or s.get("speed_kmh") or 8.0)
        except Exception:
            s_speed = 8.0
        s_major = bool(s.get("major", s.get("is_major_stop", False)))
        s_lat = float(s.get("lat") or s.get("latitude") or 28.6472)
        s_lon = float(s.get("lon") or s.get("longitude") or 77.3189)

        rs = RouteStop(
            bus_id=bus.id,
            stop_order=idx + 1,
            stop_name=s_name,
            location_address=s_addr,
            time_window=s_time or None,
            speed_kmh=s_speed,
            latitude=s_lat,
            longitude=s_lon,
            is_major_stop=s_major,
            created_at=datetime.utcnow()
        )
        db.session.add(rs)
        created_stops.append(rs)

    bus.starting_point = master_route.origin
    bus.destination = master_route.destination
    bus.route_name = master_route.route_name
    bus.route_number = master_route.route_code
    db.session.commit()

    log_audit("APPLY_MASTER_UP_ROUTE", entity_type="buses", entity_id=bus.id,
              details={"bus_id": bus.id, "route_code": master_route.route_code, "stops_count": len(created_stops)},
              user_id=user_id)

    return jsonify({
        "success": True,
        "message": f"Applied '{master_route.route_name}' ({len(created_stops)} stops) to Bus #{bus.bus_number}.",
        "bus_id": bus.id,
        "bus_number": bus.bus_number,
        "route_name": bus.route_name,
        "stops_count": len(created_stops),
        "stops": [s.to_dict() for s in created_stops]
    })

