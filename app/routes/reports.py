from flask import Blueprint, request, Response, jsonify, session
from app.services.export_service import (
    generate_passenger_attendance_csv, generate_passenger_attendance_pdf,
    generate_journey_summary_pdf
)
from app.services.audit_service import log_audit

reports_bp = Blueprint("reports", __name__, url_prefix="/api/reports")


@reports_bp.route("/export.csv", methods=["GET"])
@reports_bp.route("/journey/<int:journey_id>/csv", methods=["GET"])
def export_csv(journey_id=None):
    """
    Exports CSV attendance and passenger tracking data.
    """
    jid = journey_id or request.args.get("journey_id")
    bus_id = request.args.get("bus_id")
    status = request.args.get("status")
    person_id = request.args.get("person_id")

    csv_data = generate_passenger_attendance_csv(
        journey_id=jid,
        bus_id=bus_id,
        status=status,
        person_id=person_id
    )

    log_audit("EXPORT_CSV_REPORT", details={
        "journey_id": jid,
        "bus_id": bus_id,
        "status": status,
        "person_id": person_id
    }, user_id=session.get("user_id"))

    filename = f"bus_attendance_report_{jid or 'all'}.csv"
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@reports_bp.route("/export.pdf", methods=["GET"])
@reports_bp.route("/journey/<int:journey_id>/pdf", methods=["GET"])
def export_pdf(journey_id=None):
    """
    Exports certified executive PDF report with embedded passenger face icons.
    """
    jid = journey_id or request.args.get("journey_id")
    bus_id = request.args.get("bus_id")
    status = request.args.get("status")
    person_id = request.args.get("person_id")

    pdf_data = generate_passenger_attendance_pdf(
        journey_id=jid,
        bus_id=bus_id,
        status=status,
        person_id=person_id
    )

    log_audit("EXPORT_PDF_REPORT", details={
        "journey_id": jid,
        "bus_id": bus_id,
        "status": status,
        "person_id": person_id,
        "with_face_icons": True
    }, user_id=session.get("user_id"))

    filename = f"passenger_manifest_report_{jid or 'all'}.pdf"
    return Response(
        pdf_data,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@reports_bp.route("/journey/<int:journey_id>/summary-pdf", methods=["GET"])
@reports_bp.route("/summary-pdf", methods=["GET"])
def export_journey_summary_pdf(journey_id=None):
    """
    Exports certified executive journey summary PDF with stop-by-stop boarded, continuing, exited, occupancy table.
    """
    jid = journey_id or request.args.get("journey_id")
    pdf_data = generate_journey_summary_pdf(journey_id=jid)

    log_audit("EXPORT_JOURNEY_SUMMARY_PDF", details={"journey_id": jid}, user_id=session.get("user_id"))

    filename = f"journey_summary_report_{jid or 'active'}.pdf"
    return Response(
        pdf_data,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
