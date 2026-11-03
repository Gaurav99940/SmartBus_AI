from datetime import datetime
from flask import Blueprint, request, jsonify, session, render_template, redirect, url_for
from app.database import db
from app.models import User, Bus, OperatorBusAssignment
from app.services.audit_service import log_audit

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")

@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400

    user = User.query.filter_by(username=username).first()
    if not user or not user.check_password(password):
        return jsonify({"error": "Invalid username or password"}), 401

    session["user_id"] = user.id
    session["username"] = user.username
    session["role"] = user.role
    user.last_login = datetime.utcnow()

    assigned_bus = None
    has_bus = False

    if user.role == "operator":
        # Resolve current bus
        if user.current_bus_id:
            assigned_bus = Bus.query.get(user.current_bus_id)
        if not assigned_bus:
            # Check assignments
            assign = OperatorBusAssignment.query.filter_by(operator_id=user.id).first()
            if assign:
                assigned_bus = Bus.query.get(assign.bus_id)
            if not assigned_bus:
                # Check direct bus
                assigned_bus = Bus.query.filter_by(assigned_operator_id=user.id).first()

        if assigned_bus:
            user.current_bus_id = assigned_bus.id
            session["current_bus_id"] = assigned_bus.id
            has_bus = True

    db.session.commit()

    log_audit("USER_LOGIN", entity_type="User", entity_id=user.id, details={"username": user.username, "role": user.role}, user_id=user.id)

    # Redirect target
    if user.role == "operator" and not has_bus:
        redirect_url = "/buses?setup=1"
    else:
        redirect_url = "/dashboard"

    return jsonify({
        "message": "Login successful",
        "user": user.to_dict(),
        "has_assigned_bus": has_bus,
        "assigned_bus": assigned_bus.to_dict() if assigned_bus else None,
        "redirect_url": redirect_url
    })


@auth_bp.route("/register", methods=["POST"])
def register():
    """
    Register a new bus operator or user account.
    Validates credentials, creates user record, initializes session, and redirects to bus setup.
    """
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    full_name = data.get("full_name", "").strip()
    email = data.get("email", "").strip()
    phone = data.get("phone", "").strip()
    role = data.get("role", "operator").strip().lower()

    if not username:
        return jsonify({"error": "Username is required."}), 400
    if len(username) < 3:
        return jsonify({"error": "Username must be at least 3 characters long."}), 400
    if not password or len(password) < 4:
        return jsonify({"error": "Password must be at least 4 characters long."}), 400

    # Ensure valid role
    if role not in ("operator", "admin"):
        role = "operator"

    # Default fallback email if not provided
    if not email:
        email = f"{username.lower()}@smartbus.local"

    # Check for existing username or email
    if User.query.filter_by(username=username).first():
        return jsonify({"error": f"Username '{username}' is already taken. Please choose another."}), 409

    if User.query.filter_by(email=email).first():
        # Fallback with timestamp to avoid collision
        email = f"{username.lower()}_{int(datetime.utcnow().timestamp())}@smartbus.local"

    new_user = User(
        username=username,
        email=email,
        full_name=full_name or username,
        role=role,
        consent_accepted=True,
        created_at=datetime.utcnow()
    )
    new_user.set_password(password)
    db.session.add(new_user)
    db.session.commit()

    # Automatically log in the newly registered user
    session["user_id"] = new_user.id
    session["username"] = new_user.username
    session["role"] = new_user.role

    log_audit(
        "USER_REGISTER",
        entity_type="User",
        entity_id=new_user.id,
        details={"username": new_user.username, "role": new_user.role, "email": new_user.email},
        user_id=new_user.id
    )

    # Operators go to bus setup; admins go to dashboard
    redirect_url = "/buses?setup=1" if role == "operator" else "/dashboard"

    return jsonify({
        "success": True,
        "message": f"Account for '{new_user.username}' successfully registered!",
        "user": new_user.to_dict(),
        "redirect_url": redirect_url
    }), 201



@auth_bp.route("/logout", methods=["POST", "GET"])
def logout():
    user_id = session.get("user_id")
    if user_id:
        log_audit("USER_LOGOUT", entity_type="User", entity_id=user_id, user_id=user_id)
    session.clear()
    if request.method == "GET":
        return redirect(url_for("page_login"))
    return jsonify({"message": "Logged out successfully"})


@auth_bp.route("/me", methods=["GET"])
def me():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"authenticated": False}), 401
    user = User.query.get(user_id)
    if not user:
        session.clear()
        return jsonify({"authenticated": False}), 401

    assigned_bus = None
    if user.current_bus_id:
        b = Bus.query.get(user.current_bus_id)
        if b:
            assigned_bus = b.to_dict()

    return jsonify({
        "authenticated": True,
        "user": user.to_dict(),
        "assigned_bus": assigned_bus,
        "has_assigned_bus": assigned_bus is not None
    })


@auth_bp.route("/consent", methods=["POST"])
def accept_consent():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401
    user = User.query.get(user_id)
    if user:
        user.consent_accepted = True
        db.session.commit()
        log_audit("ACCEPT_BIOMETRIC_CONSENT", entity_type="User", entity_id=user.id, user_id=user.id)
    return jsonify({"message": "Consent accepted successfully"})
