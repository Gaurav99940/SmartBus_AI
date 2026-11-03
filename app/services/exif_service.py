import hashlib
from datetime import datetime
from PIL import Image, ExifTags

def compute_file_hash(file_bytes_or_path):
    """
    Computes SHA-256 hash of file content to detect duplicate photo uploads.
    """
    sha = hashlib.sha256()
    if isinstance(file_bytes_or_path, (str, bytes)):
        if isinstance(file_bytes_or_path, str):
            with open(file_bytes_or_path, "rb") as f:
                while chunk := f.read(65536):
                    sha.update(chunk)
        else:
            sha.update(file_bytes_or_path)
    else:
        # File-like object
        pos = file_bytes_or_path.tell()
        while chunk := file_bytes_or_path.read(65536):
            sha.update(chunk)
        file_bytes_or_path.seek(pos)
    return sha.hexdigest()


def _convert_to_degrees(value):
    """
    Helper function to convert GPS coordinates stored in EXIF rational tuple
    (degrees, minutes, seconds) to decimal degrees float.
    """
    try:
        def _to_float(v):
            if hasattr(v, 'numerator') and hasattr(v, 'denominator'):
                return float(v.numerator) / float(v.denominator) if v.denominator != 0 else 0.0
            if isinstance(v, tuple) and len(v) == 2:
                return float(v[0]) / float(v[1]) if v[1] != 0 else 0.0
            return float(v)

        d = _to_float(value[0])
        m = _to_float(value[1])
        s = _to_float(value[2])
        return d + (m / 60.0) + (s / 3600.0)
    except Exception:
        return None


def extract_metadata(image_path):
    """
    Extracts EXIF metadata including capture timestamp and GPS coordinates.
    Returns:
    {
        "has_exif": bool,
        "capture_timestamp": datetime or None,
        "latitude": float or None,
        "longitude": float or None,
        "gps_accuracy": float or None,
        "device_make": str or None,
        "device_model": str or None,
        "warnings": list of str,
        "raw_exif": dict
    }
    """
    result = {
        "has_exif": False,
        "capture_timestamp": None,
        "latitude": None,
        "longitude": None,
        "gps_accuracy": None,
        "device_make": None,
        "device_model": None,
        "warnings": [],
        "raw_exif": {}
    }

    try:
        with Image.open(image_path) as img:
            raw_exif = img._getexif()
            if not raw_exif:
                result["warnings"].append("No EXIF metadata found in the uploaded image. Timestamp and GPS location must be entered manually or obtained from device.")
                return result

            result["has_exif"] = True
            exif_data = {}
            for tag_id, value in raw_exif.items():
                tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
                exif_data[tag_name] = value

            # Camera Make/Model
            result["device_make"] = str(exif_data.get("Make", "")).strip() or None
            result["device_model"] = str(exif_data.get("Model", "")).strip() or None

            # Capture Timestamp
            date_str = (
                exif_data.get("DateTimeOriginal")
                or exif_data.get("DateTimeDigitized")
                or exif_data.get("DateTime")
            )
            if date_str:
                for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
                    try:
                        result["capture_timestamp"] = datetime.strptime(str(date_str).strip(), fmt)
                        break
                    except ValueError:
                        continue
            
            if not result["capture_timestamp"]:
                result["warnings"].append("EXIF present but capture timestamp was missing or unparseable.")

            # GPS Info
            gps_info = exif_data.get("GPSInfo")
            if gps_info:
                gps_tags = {}
                for key, val in gps_info.items():
                    sub_tag = ExifTags.GPSTAGS.get(key, str(key))
                    gps_tags[sub_tag] = val

                lat_val = gps_tags.get("GPSLatitude")
                lat_ref = gps_tags.get("GPSLatitudeRef")
                lon_val = gps_tags.get("GPSLongitude")
                lon_ref = gps_tags.get("GPSLongitudeRef")

                if lat_val and lon_val:
                    lat = _convert_to_degrees(lat_val)
                    lon = _convert_to_degrees(lon_val)

                    if lat is not None and lon is not None:
                        if lat_ref in ("S", "s"):
                            lat = -lat
                        if lon_ref in ("W", "w"):
                            lon = -lon

                        # Bounds validation
                        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                            result["latitude"] = round(lat, 6)
                            result["longitude"] = round(lon, 6)
                        else:
                            result["warnings"].append("EXIF GPS coordinates were out of valid geographic range.")
                
                # GPS accuracy/DOP if available
                dop = gps_tags.get("GPSDOP")
                if dop:
                    try:
                        if hasattr(dop, 'numerator'):
                            result["gps_accuracy"] = float(dop.numerator) / float(dop.denominator)
                        else:
                            result["gps_accuracy"] = float(dop)
                    except Exception:
                        pass

            if result["latitude"] is None or result["longitude"] is None:
                result["warnings"].append("No valid GPS metadata found in photo EXIF. Use manual location entry or device GPS.")

            # Store sanitized subset in raw_exif
            result["raw_exif"] = {
                k: str(v)[:100] for k, v in exif_data.items() 
                if k not in ("MakerNote", "UserComment")
            }

    except Exception as e:
        result["warnings"].append(f"Failed to read image EXIF: {str(e)}")

    return result
