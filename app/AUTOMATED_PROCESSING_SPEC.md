# Automated Multi-Angle Video Processing & Timetable Stop Matching Specification

## Architecture Overview
The new feature enables automated stop-wise video and image processing for multi-angle cameras (Angles A, B, C) synchronized with an uploaded Excel/CSV timetable.

### Key Components:
1. **Source Folders & Local File Handling** (`media_service.py`):
   - Secure local directory picker and scanning.
   - Preserves source media as strictly read-only.
   - Computes metadata: file size, modification time, duration, frame timestamps, EXIF capture time.
   - Generates and persists a media manifest.

2. **Video & Image Upload Compatibility** (`media_service.py` & `person_pipeline.py`):
   - Dropzones accept both images (JPG, PNG, WEBP) and videos (MP4, AVI, MOV, MKV).
   - Video preview with thumbnail and duration.
   - Incremental frame extraction at stop timestamp windows without loading entire videos into RAM.

3. **Timetable Import & Stop Window Matching** (`timetable_service.py`):
   - Parses Excel (.xlsx, .xls) and CSV (.csv).
   - Flexible column mapping (Arrival/Departure, IN/OUT, Stop Number/Name, Service Date, Time Zone).
   - Configurable matching tolerance around stop windows (e.g. +/- 3 minutes).
   - Matches Angle A, B, C clips based on recording timestamps and stop time windows.

4. **Automated Stop-Wise Processing Queue & State Machine** (`batch_processor.py`):
   - Job queue supporting: Start, Pause, Resume, Cancel, Retry Failed.
   - Sequential stop processing:
     1. Stop time window lookup
     2. Clip & frame synchronization across Angles A, B, C
     3. Cross-angle passenger detection using existing AI pipeline
     4. Stop coverage classification: Fully Covered, Partially Covered, Not Covered, Needs Review, Failed
     5. Database persistence of stop results, observations, and audit log.

5. **Stop Coverage & Reports Export** (`export_service.py`):
   - Detailed stop-wise report with metrics per stop.
   - Export to CSV and Excel (.xlsx).
   - Processing manifest export.
