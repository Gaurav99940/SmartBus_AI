import os
import re
from datetime import datetime, time, date, timedelta
from pathlib import Path
import pandas as pd
from app.config import Config


def parse_flexible_time(val, base_date=None):
    """
    Parses a time string, datetime object, or time object into a full datetime.
    Supports formats like:
      - 08:00:00
      - 8:05 AM / 08:05 PM
      - 2026-10-14 08:00:00
      - 08:00
    """
    if val is None or pd.isna(val) or str(val).strip() == "":
        return None

    if isinstance(val, datetime):
        return val

    if isinstance(val, time):
        b_date = base_date or date.today()
        return datetime.combine(b_date, val)

    val_str = str(val).strip()

    # Try full datetime formats (both 24-hour and 12-hour AM/PM)
    full_formats = [
        "%d-%m-%Y %I:%M:%S %p",
        "%d-%m-%Y %I:%M %p",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%d/%m/%Y %I:%M:%S %p",
        "%d/%m/%Y %I:%M %p",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%Y-%m-%d %I:%M:%S %p",
        "%Y-%m-%d %I:%M %p",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%d-%m-%Y %H:%M:%S.%f",
    ]
    for fmt in full_formats:
        try:
            return datetime.strptime(val_str, fmt)
        except ValueError:
            pass

    # Try dateutil parser if available as flexible fallback
    try:
        from dateutil import parser
        parsed_dt = parser.parse(val_str, dayfirst=True)
        if parsed_dt:
            return parsed_dt
    except Exception:
        pass

    # Try time-only formats
    time_formats = [
        "%H:%M:%S",
        "%H:%M",
        "%I:%M:%S %p",
        "%I:%M %p",
        "%I:%M:%S%p",
        "%I:%M%p",
    ]
    b_date = base_date or date.today()
    for fmt in time_formats:
        try:
            t_obj = datetime.strptime(val_str, fmt).time()
            return datetime.combine(b_date, t_obj)
        except ValueError:
            pass

    return None


def detect_columns(df_columns):
    """
    Infers column mapping from timetable header names.
    """
    cols = [str(c).strip() for c in df_columns]
    mapping = {
        "stop_number": None,
        "stop_name": None,
        "arrival_time": None,
        "departure_time": None,
        "in_time": None,
        "out_time": None,
        "service_date": None
    }

    for c in cols:
        clow = c.lower()
        if not mapping["stop_number"] and ("stop_number" in clow or "stop_no" in clow or "stop no" in clow or "stop #" in clow or clow == "seq" or clow == "order" or clow == "stop"):
            mapping["stop_number"] = c
        elif not mapping["stop_name"] and ("stop_name" in clow or "stop name" in clow or "station" in clow or "location" in clow or clow == "name"):
            mapping["stop_name"] = c
        elif not mapping["arrival_time"] and ("arrival" in clow or "arr_time" in clow or "arr" == clow):
            mapping["arrival_time"] = c
        elif not mapping["departure_time"] and ("departure" in clow or "dep_time" in clow or "dep" == clow):
            mapping["departure_time"] = c
        elif not mapping["in_time"] and ("in_time" in clow or "in time" in clow or clow == "in" or "enter" in clow):
            mapping["in_time"] = c
        elif not mapping["out_time"] and ("out_time" in clow or "out time" in clow or clow == "out" or "exit" in clow):
            mapping["out_time"] = c
        elif not mapping["service_date"] and ("date" in clow or "service_date" in clow):
            mapping["service_date"] = c

    # Fallback for stop_name if stop_name not matched yet
    if not mapping["stop_name"]:
        for c in cols:
            clow = c.lower()
            if "name" in clow or "stop" in clow and c != mapping.get("stop_number"):
                mapping["stop_name"] = c
                break

    # Fallbacks: if no stop_number or stop_name found, try first columns
    if not mapping["stop_number"] and len(cols) > 0:
        mapping["stop_number"] = cols[0]
    if not mapping["stop_name"] and len(cols) > 1:
        mapping["stop_name"] = cols[1] if cols[1] != mapping.get("stop_number") else (cols[0] if len(cols) == 1 else cols[1])

    # Map IN/OUT to arrival/departure if arrival/departure not found
    if not mapping["arrival_time"] and mapping["in_time"]:
        mapping["arrival_time"] = mapping["in_time"]
    if not mapping["departure_time"] and mapping["out_time"]:
        mapping["departure_time"] = mapping["out_time"]

    return mapping


def parse_timetable_file(file_path, column_mapping=None, service_date=None, timezone="UTC", tolerance_minutes=3, time_window_mode="ARRIVAL_DEPARTURE"):
    """
    Parses Excel (.xlsx, .xls) or CSV (.csv) file into a standardized list of stops.
    Returns:
      {
        "success": bool,
        "filename": str,
        "total_stops": int,
        "columns": list,
        "column_mapping": dict,
        "stops": list,
        "validation_errors": list,
        "warnings": list
      }
    """
    path = Path(file_path)
    if not path.exists():
        return {"success": False, "error": f"File does not exist: {file_path}", "validation_errors": ["File not found"]}

    ext = path.suffix.lower()
    df = None
    try:
        if ext in (".xlsx", ".xls"):
            df = pd.read_excel(str(path))
        elif ext == ".csv":
            # Try utf-8 then latin-1
            try:
                df = pd.read_csv(str(path), encoding="utf-8")
            except Exception:
                df = pd.read_csv(str(path), encoding="latin-1")
        else:
            return {"success": False, "error": f"Unsupported timetable format '{ext}'. Must be .xlsx, .xls, or .csv", "validation_errors": ["Unsupported format"]}
    except Exception as e:
        return {"success": False, "error": f"Failed to parse file: {str(e)}", "validation_errors": [str(e)]}

    if df is None or df.empty:
        return {"success": False, "error": "The timetable file is empty.", "validation_errors": ["Spreadsheet contains no rows"]}

    # Clean column names
    df.columns = [str(c).strip() for c in df.columns]
    all_columns = list(df.columns)

    # Use provided mapping or auto-detect
    mapping = column_mapping or detect_columns(all_columns)

    # Validate mapping
    validation_errors = []
    warnings = []

    stop_col = mapping.get("stop_name")
    arr_col = mapping.get("arrival_time") or mapping.get("in_time")
    dep_col = mapping.get("departure_time") or mapping.get("out_time")

    if not stop_col or stop_col not in df.columns:
        validation_errors.append(f"Missing required Stop Name column (configured as '{stop_col}')")

    if not arr_col and not dep_col:
        validation_errors.append("At least one time column (Arrival / IN or Departure / OUT) must be present and mapped.")

    if validation_errors:
        return {
            "success": False,
            "filename": path.name,
            "columns": all_columns,
            "column_mapping": mapping,
            "validation_errors": validation_errors,
            "stops": []
        }

    # Resolve default service date
    base_date = None
    if service_date:
        if isinstance(service_date, str):
            try:
                base_date = datetime.strptime(service_date.strip(), "%Y-%m-%d").date()
            except ValueError:
                base_date = date.today()
        elif isinstance(service_date, (date, datetime)):
            base_date = service_date if isinstance(service_date, date) else service_date.date()
    else:
        base_date = date.today()

    parsed_stops = []
    tol_delta = timedelta(minutes=int(tolerance_minutes))

    for idx, row in df.iterrows():
        # Stop number
        stop_num = idx + 1
        num_col = mapping.get("stop_number")
        if num_col and num_col in row and pd.notna(row[num_col]):
            try:
                stop_num = int(float(str(row[num_col]).strip()))
            except Exception:
                pass

        # Stop name
        stop_name = str(row[stop_col]).strip() if (stop_col and pd.notna(row[stop_col])) else f"Stop {stop_num}"
        if not stop_name or stop_name == "nan":
            continue

        # Row-level service date if present
        row_base_date = base_date
        date_col = mapping.get("service_date")
        if date_col and date_col in row and pd.notna(row[date_col]):
            try:
                row_base_date = parse_flexible_time(row[date_col]).date()
            except Exception:
                pass

        # Arrival & Departure times
        arr_val = row.get(arr_col) if arr_col else None
        dep_val = row.get(dep_col) if dep_col else None

        arr_dt = parse_flexible_time(arr_val, row_base_date)
        dep_dt = parse_flexible_time(dep_val, row_base_date)

        if not arr_dt and not dep_dt:
            warnings.append(f"Row {idx + 1} ({stop_name}) has invalid or missing arrival/departure times. Skipped.")
            continue

        if not arr_dt:
            arr_dt = dep_dt - timedelta(minutes=2)
        if not dep_dt:
            dep_dt = arr_dt + timedelta(minutes=2)

        if dep_dt < arr_dt:
            # Maybe crossed midnight or departure before arrival
            dep_dt = dep_dt + timedelta(days=1)

        # Calculate analysis window based on time_window_mode and tolerance
        # Modes:
        # A. 'ARRIVAL_DEPARTURE': strictly between arrival and departure (with buffer tolerance)
        # B. 'WINDOW_WITH_BUFFER': extends before arrival and after departure by tolerance_minutes
        # C. 'POINT_FRAMES': window around arrival & departure
        w_start = arr_dt - tol_delta
        w_end = dep_dt + tol_delta

        parsed_stops.append({
            "stop_number": stop_num,
            "stop_name": stop_name,
            "scheduled_arrival": arr_dt,
            "scheduled_departure": dep_dt,
            "actual_arrival": arr_dt,
            "actual_departure": dep_dt,
            "window_start": w_start,
            "window_end": w_end
        })

    if not parsed_stops:
        validation_errors.append("No valid stops could be parsed from the file.")
        return {
            "success": False,
            "filename": path.name,
            "columns": all_columns,
            "column_mapping": mapping,
            "validation_errors": validation_errors,
            "stops": []
        }

    # If not explicitly specified by user, infer the service_date from parsed stop timestamps
    inferred_service_date = None
    for s in parsed_stops:
        dt = s.get("scheduled_arrival") or s.get("scheduled_departure")
        if dt and isinstance(dt, datetime):
            inferred_service_date = dt.date()
            break

    effective_service_date = base_date if service_date else (inferred_service_date or base_date)

    return {
        "success": True,
        "filename": path.name,
        "total_stops": len(parsed_stops),
        "columns": all_columns,
        "column_mapping": mapping,
        "service_date": effective_service_date.strftime("%Y-%m-%d"),
        "timezone": timezone,
        "tolerance_minutes": tolerance_minutes,
        "time_window_mode": time_window_mode,
        "validation_errors": validation_errors,
        "warnings": warnings,
        "stops": parsed_stops
    }
