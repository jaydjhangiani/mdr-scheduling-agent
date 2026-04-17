# MDRhythm Receptionist Voice Agent

---

## What Is This?

The MDRhythm Pharmacy Voice Agent is an AI-powered phone receptionist. Patients call in, speak naturally, and the agent handles the entire interaction — no hold music, no menus, no human staff needed for routine bookings.

It does two things:

1. **Book an appointment** — collects patient details, checks real-time availability, and confirms a slot
2. **Leave a note** — captures a free-text message for the pharmacy team to follow up on

---

## Who Is It For?

- **Patients** — call at any time and get an appointment booked in under 2 minutes
- **recpetionist staff** — wake up to a Google Sheet with confirmed appointments and notes, no manual data entry
- **Operations** — reduces inbound call handling load and eliminates booking errors from human transcription

---

## Patient Experience Walkthrough

Here is what a typical booking call sounds like from the patient's perspective.

**1. Greeting**
> "Hello, thank you for calling. To get started, could I please have your mobile number?"

The agent asks for the patient's mobile number and reads it back digit by digit to confirm.

**2. Intent**
> "Are you calling to book an appointment or would you like to leave a note for our team?"

**3. New or returning patient**
> "Are you a new or returning patient?"

For **returning patients** — the agent looks them up in the database by mobile number and asks for their date of birth to verify identity. If matched, it greets them by name.

For **new patients** — the agent asks them to spell their full name letter by letter, reads it back to confirm, then collects their date of birth.

**4. Date and time**
> "What date and time works best for you?"

The patient can say things like:
- "Next Monday morning"
- "April 15th at 3pm"
- "This week, afternoon"

The agent checks real-time availability and reads back the open slots if the requested time is taken.

**5. Confirmation and booking**
> "Just to confirm — I have Nari Hira, Monday April 13th at 9am. Shall I go ahead and book that?"

Only after the patient says yes does the appointment get written to the system.

**Example from a real call:**
```
Agent:  "Let me check availability for you, one moment."
         → checks API → slot at 9:00, 9:30, 10:00 available

Agent:  "I have openings at 9am, 9:30am, and 10am on April 13th. Which works for you?"
Patient: "9am please."
Agent:  "Perfect. I have Nari Hira booked for Monday April 13th at 9am. Is there anything else I can help you with?"
```

---

## What Happens in the Background

- Patient details and appointment are written to a **Google Sheet** in real time
- The scheduling system checks **live availability** — no double bookings
- Every session generates a usage log including conversation turns, tokens used, and whether the booking completed successfully
- If a patient cannot be understood after 3 attempts, the agent politely ends the call and logs the failed session

---

## Key Capabilities

| Capability | Detail |
|---|---|
| Natural language date/time | Understands "next Monday", "afternoon", "3pm", "April 15th" |
| Live availability check | Queries scheduling API before confirming any slot |
| Patient lookup | Identifies returning patients by mobile + DOB verification |
| Barge-in | Patient can interrupt the agent at any point — it stops and listens immediately |
| Hold messages | Agent speaks a hold message while fetching data so there is no silent pause |
| Google Sheets logging | Appointments and notes written automatically, no staff input needed |
| Graceful failure | If systems are unavailable, agent offers to take a note and have the team call back |

---

## Technical Overview

---

## Architecture

```
Browser (mic/speaker)
    ↕ WebSocket (binary PCM audio + JSON events)
main.py (Python WebSocket server, port 5000)
    ↕ WebSocket (wss://agent.deepgram.com)
Deepgram Voice Agent
    ├── STT: nova-3
    ├── LLM: gpt-4o-mini (temp 0.2)
    └── TTS: aura-2-thalia-en
```

Function calls from the LLM are intercepted by `main.py` before execution. The server enforces a strict state machine — invalid calls are rejected with a correction message the agent reads and self-corrects from.

---

## Conversation State Machine

```
AWAITING_MOBILE
    → register_mobile / mobile_attempt_failed

AWAITING_INTENT
    → set_intent("booking" | "note")

AWAITING_PATIENT_TYPE          [booking only]
    → set_patient_type("new" | "returning")

AWAITING_PATIENT_INFO          [booking only]
    → fetch_patient             [returning only — blocked for new]
    → register_new_patient      [new only — blocked for returning]

AWAITING_AVAILABILITY          [booking only]
    → check_availability

AWAITING_BOOK                  [booking only]
    → check_availability        [re-check allowed]
    → book_appointment

AWAITING_NOTE                  [note only]
    → leave_note

DONE
```

If the LLM calls a function outside the allowed set for the current state, the server immediately returns:

```json
{
  "error": "out_of_sequence",
  "message": "Cannot call fetch_patient in state 'awaiting_patient_info'. Allowed: ['register_new_patient']. Follow the steps in order."
}
```

The agent reads this and self-corrects in the next turn — no human intervention needed.

---

## What This Fixed (Real Transcript Examples)

### Bug 1 — fetch_patient called for a new patient

**Before** (no guardrails): Agent asked "new or returning?" Patient said "new." Agent called `fetch_patient` anyway.

**After**: `allowed_functions()` discards `fetch_patient` once `set_patient_type("new")` is called. The call is blocked server-side before it reaches the DB.

---

### Bug 2 — Date of birth used as appointment date

**Before**: After collecting DOB `1936-09-21` for a new patient, the agent immediately called:
```
check_availability {'date': '1936-09-21'} → 200
```
The availability API returned slots for 1936 — a 90-year-old date.

**After**: Two layers of protection:
1. `check_availability` rejects any date in the past with: `"That looks like a date of birth. Please ask the patient for a future appointment date."`
2. Prompt explicitly states: *"The appointment date is a future date they want to visit and is never their date of birth."*

---

### Bug 3 — Window keyword sent raw to availability API (400 error)

**Before**:
```
check_availability {'date': '2026-04-13', 'time': 'afternoon'} → 400
```
The API doesn't accept keyword strings.

**After**: `check_availability()` converts window keywords before the HTTP call:
```python
"afternoon" → windowStart=12:00, windowEnd=15:00
```
```
check_availability {'date': '2026-04-13', 'windowStart': '12:00', 'windowEnd': '15:00'} → 200
```

---

## Barge-In

Barge-in lets the patient interrupt the agent mid-sentence. It operates entirely at the audio layer — independent of the state machine.

**How it works:**

1. Deepgram detects the patient started speaking → sends `UserStartedSpeaking`
2. `main.py` forwards `{"type": "barge_in"}` to the browser
3. The browser flushes its audio playback queue immediately — agent stops mid-word
4. Deepgram begins transcribing the patient's new speech

```
[STS] Event: UserStartedSpeaking
[BARGE-IN] User started speaking          ← server detects interrupt
                                           ← browser drops queued audio
[STS] Event: ConversationText             ← patient's words transcribed
```

**What barge-in does NOT affect:**
- The state machine — if a function call already completed before the interrupt, the state has already advanced. The patient cannot "undo" a completed function call by barging in.
- The LLM context — Deepgram manages conversation history internally. Barge-in only affects the audio pipeline.

**From the transcript** — patient interrupted twice while agent was reading back available slots. State remained at `AWAITING_BOOK` correctly, and the agent was able to call `book_appointment` after the patient confirmed:

```
[BARGE-IN] User started speaking
...
[FUNC] Request: book_appointment, args: {'date': '2026-04-13', 'time': '09:00', ...}
[RUBRIC] Appointment confirmed: 2026-04-13 09:00
```

---

## Hold Messages

For function calls with API latency (`fetch_patient`, `check_availability`), the server injects a spoken hold message **before** executing the call — guaranteed regardless of what the LLM decides to say:

```python
HOLD_MESSAGES = {
    "fetch_patient":      "One moment while I pull up your records.",
    "check_availability": "Let me check availability for you, one moment.",
}
```

This keeps the conversation natural during the ~1–2 second API round-trip. The message is sent as `InjectAgentMessage` to Deepgram, which speaks it immediately while the function executes in parallel.

---

## Function Reference

| Function | State required | Description |
|---|---|---|
| `register_mobile` | AWAITING_MOBILE | Mobile number confirmed |
| `mobile_attempt_failed` | AWAITING_MOBILE | Failed transcription attempt |
| `set_intent` | AWAITING_INTENT | "booking" or "note" |
| `set_patient_type` | AWAITING_PATIENT_TYPE | "new" or "returning" |
| `register_new_patient` | AWAITING_PATIENT_INFO (new) | Name + DOB for new patients |
| `fetch_patient` | AWAITING_PATIENT_INFO (returning) | DB lookup by mobile |
| `check_availability` | AWAITING_AVAILABILITY / AWAITING_BOOK | Check slots via scheduling API |
| `book_appointment` | AWAITING_BOOK | Write confirmed slot to Google Sheets |
| `leave_note` | AWAITING_NOTE | Write note to Google Sheets |

---

## Environment Variables

| Variable | Description |
|---|---|
| `DEEPGRAM_API_KEY` | Deepgram API key |
| `OPENAI_API_KEY` | OpenAI key (gpt-4o-mini) |
| `APPOINTMENT_API_URL` | Base URL for scheduling API (default: `http://localhost:3000/api/appointments`) |
| `GOOGLE_SHEET_ID` | Google Sheets ID for appointments and notes |
| `GOOGLE_CREDENTIALS_PATH` | Path to service account JSON |
| `GOOGLE_APPOINTMENTS_TAB` | Sheet tab name for appointments (default: `Appointments`) |
| `GOOGLE_NOTES_TAB` | Sheet tab name for notes (default: `Notes`) |
