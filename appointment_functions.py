import os
import requests
from datetime import datetime, timezone
import gspread
from google.oauth2.service_account import Credentials

PATIENT_API = "https://mdrvoiceai.webmdr.net/api/patients"
APPOINTMENT_API = os.getenv("APPOINTMENT_API_URL", "http://localhost:3000/api/appointments")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

TIME_WINDOWS = {
    "morning":   "09:00-11:00",
    "lunch":     "11:00-12:00",
    "afternoon": "12:00-15:00",
    "evening":   "15:00-17:00",
}

# Start/end bounds sent to the availability API for each window keyword
_WINDOW_BOUNDS = {
    "morning":   ("09:00", "11:00"),
    "lunch":     ("11:00", "12:00"),
    "afternoon": ("12:00", "15:00"),
    "evening":   ("15:00", "17:00"),
}


def _get_worksheet(tab_name: str):
    creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip().rstrip("/")
    return client.open_by_key(sheet_id).worksheet(tab_name)


def _parse_time(time_str: str) -> str:
    """Convert window keyword to range, or pass through HH:MM."""
    return TIME_WINDOWS.get(time_str.lower().strip(), time_str)


# ---------------------------------------------------------------------------
# Rubric control functions
# ---------------------------------------------------------------------------

def register_mobile(mobile: str) -> dict:
    """Called when agent successfully captures the patient's mobile number."""
    return {"status": "captured", "mobile": mobile}


def mobile_attempt_failed() -> dict:
    """Called each time agent fails to understand the mobile number."""
    return {"status": "recorded"}


def set_intent(intent: str) -> dict:
    """Called once the patient's intent is clear: 'booking' or 'note'."""
    if intent not in ("booking", "note"):
        return {"error": "invalid_intent", "message": "intent must be 'booking' or 'note'"}
    return {"status": "ok", "intent": intent}


def set_patient_type(patient_type: str) -> dict:
    """Called once patient type is known: 'new' or 'returning'."""
    if patient_type not in ("new", "returning"):
        return {"error": "invalid_patient_type", "message": "patient_type must be 'new' or 'returning'"}
    return {"status": "ok", "patient_type": patient_type}


def register_new_patient(name: str, dob: str) -> dict:
    """Called after spelling out and confirming name and DOB for a new patient."""
    try:
        datetime.strptime(dob, "%Y-%m-%d")
    except ValueError:
        return {"error": "invalid_dob", "message": "Date of birth must be YYYY-MM-DD format."}
    return {"status": "ok", "name": name, "dob": dob}


# ---------------------------------------------------------------------------
# Patient lookup
# ---------------------------------------------------------------------------

def fetch_patient(mobile_number: str) -> dict:
    """
    Fetch patient details from SQL DB by mobile number.
    Single attempt with a short timeout — the agent handles retries via prompt
    so audio keeps flowing to Deepgram between attempts.
    """
    try:
        resp = requests.get(f"{PATIENT_API}/{mobile_number}", timeout=10)
        print(f"[API] fetch_patient {mobile_number} → {resp.status_code}")
        if resp.status_code == 200:
            raw = resp.json()
            print(f"[API] Response: {raw}")
            # API wraps patient record under a 'data' key
            patient = raw.get("data") if isinstance(raw.get("data"), dict) else raw
            name = (
                patient.get("name")
                or f"{patient.get('fname', '')} {patient.get('lname', '')}".strip()
                or patient.get("full_name")
                or patient.get("patient_name")
            )
            dob = (
                patient.get("dob")
                or patient.get("date_of_birth")
                or patient.get("dateOfBirth")
                or patient.get("DOB")
            )
            return {"found": True, "name": name or None, "dob": dob or None}
        elif resp.status_code == 404:
            return {"found": False}
        else:
            return {"error": f"API returned {resp.status_code}"}
    except requests.Timeout:
        print("[API] Timeout")
        return {"error": "timeout", "retry_suggested": True}
    except Exception as e:
        print(f"[API] Error: {e}")
        return {"error": str(e), "retry_suggested": True}


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def check_availability(date: str = None, time: str = None) -> dict:
    """
    Check appointment availability via the scheduling API.
    - No args     → next 3 available slots from now
    - date only   → next 3 available slots on that date
    - date + time → checks specific slot; if taken returns next 3 alternatives
    time accepts HH:MM (24h) or a window keyword: morning / afternoon / evening / lunch
    """
    try:
        params = {}
        if date:
            params["date"] = date
        if time:
            bounds = _WINDOW_BOUNDS.get(time.lower().strip())
            if bounds:
                params["windowStart"], params["windowEnd"] = bounds
            else:
                params["time"] = time  # specific HH:MM
        resp = requests.get(f"{APPOINTMENT_API}/availability", params=params, timeout=5)
        print(f"[API] check_availability {params} → {resp.status_code}")
        if resp.status_code == 200:
            raw = resp.json()
            # Normalize the API response so the LLM never sees the ambiguous
            # {'available': False, 'next3': [...]} shape, which causes it to say
            # "no availability" and then list slots in the same breath.
            slots = raw.get("next3", [])
            if raw.get("available"):
                return {"status": "slot_available", "slots": slots}
            elif slots:
                return {"status": "slot_unavailable_alternatives_offered", "alternatives": slots}
            else:
                return {"status": "no_availability"}
        return {"error": f"API returned {resp.status_code}"}
    except requests.Timeout:
        return {"error": "timeout", "retry_suggested": True}
    except Exception as e:
        print(f"[API] check_availability error: {e}")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Booking & notes
# ---------------------------------------------------------------------------

def book_appointment(mobile: str, name: str, dob: str,
                     date: str, time: str, patient_type: str) -> dict:
    """
    Write a confirmed appointment to the Appointments sheet.
    date: YYYY-MM-DD
    time: HH:MM (24h) or window keyword (morning/afternoon/evening/lunch)
    patient_type: 'new' or 'returning'
    """
    try:
        parsed_time = _parse_time(time)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        ws = _get_worksheet(os.getenv("GOOGLE_APPOINTMENTS_TAB", "Appointments"))
        ws.append_row([name, timestamp, dob, date, parsed_time, patient_type])
        print(f"[SHEETS] Appointment booked: {name} on {date} at {parsed_time}")
        return {"status": "booked", "name": name, "date": date, "time": parsed_time}
    except Exception as e:
        print(f"[SHEETS] Error: {e}")
        return {"error": str(e)}


def leave_note(mobile: str, name: str, note: str) -> dict:
    """Write a free-text note to the Notes sheet."""
    try:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        ws = _get_worksheet(os.getenv("GOOGLE_NOTES_TAB", "Notes"))
        ws.append_row([name, mobile, timestamp, note])
        print(f"[SHEETS] Note saved for {name}")
        return {"status": "saved", "name": name, "timestamp": timestamp}
    except Exception as e:
        print(f"[SHEETS] Error: {e}")
        return {"error": str(e)}


FUNCTION_MAP = {
    "register_mobile":       register_mobile,
    "mobile_attempt_failed": mobile_attempt_failed,
    "set_intent":            set_intent,
    "set_patient_type":      set_patient_type,
    "register_new_patient":  register_new_patient,
    "fetch_patient":         fetch_patient,
    "check_availability":    check_availability,
    "book_appointment":      book_appointment,
    "leave_note":            leave_note,
}
