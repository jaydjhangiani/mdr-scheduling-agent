import asyncio
import json
import logging
import websockets
import os
import urllib.request
from http import HTTPStatus
from datetime import datetime
from dotenv import load_dotenv
from enum import Enum
from appointment_functions import FUNCTION_MAP

# Suppress harmless noise from Render's load balancer probes:
# - TCP probes that close before sending any HTTP
# - HEAD requests (Render health checks) which websockets rejects before process_request fires
class _SuppressProbeNoise(logging.Filter):
    _NOISE = ("connection closed while reading", "unsupported HTTP method")
    def filter(self, record):
        msg = record.getMessage()
        return not any(s in msg for s in self._NOISE)

for _logger in ("websockets.server", "websockets.asyncio.server"):
    logging.getLogger(_logger).addFilter(_SuppressProbeNoise())

load_dotenv()

# ---------------------------------------------------------------------------
# Map Deepgram event types → frontend event types
# ---------------------------------------------------------------------------
DEEPGRAM_TO_CLIENT = {
    "AgentStartedSpeaking": "agent_speaking_start",
    "AgentAudioDone":       "agent_speaking_end",
}


# ---------------------------------------------------------------------------
# Conversation state machine
# ---------------------------------------------------------------------------

class State(Enum):
    AWAITING_MOBILE       = "awaiting_mobile"
    AWAITING_INTENT       = "awaiting_intent"
    AWAITING_PATIENT_TYPE = "awaiting_patient_type"
    AWAITING_PATIENT_INFO = "awaiting_patient_info"
    AWAITING_AVAILABILITY = "awaiting_availability"
    AWAITING_BOOK         = "awaiting_book"
    AWAITING_NOTE         = "awaiting_note"
    DONE                  = "done"


ALLOWED = {
    State.AWAITING_MOBILE:       {"register_mobile", "mobile_attempt_failed"},
    State.AWAITING_INTENT:       {"set_intent"},
    State.AWAITING_PATIENT_TYPE: {"set_patient_type"},
    State.AWAITING_PATIENT_INFO: {"fetch_patient", "register_new_patient"},
    State.AWAITING_AVAILABILITY: {"check_availability"},
    State.AWAITING_BOOK:         {"check_availability", "book_appointment"},
    State.AWAITING_NOTE:         {"leave_note"},
    State.DONE:                  set(),
}

HOLD_MESSAGES = {
    "fetch_patient":      "One moment while I pull up your records.",
    "check_availability": "Let me check availability for you, one moment.",
}


# ---------------------------------------------------------------------------
# Usage tracking per session
# ---------------------------------------------------------------------------
class UsageTracker:
    def __init__(self):
        self.openai_prompt_tokens = 0
        self.openai_completion_tokens = 0
        self.tts_characters = 0
        self.stt_audio_chunks = 0
        self.conversation_turns = 0

    def add_llm_usage(self, prompt_tokens: int, completion_tokens: int):
        self.openai_prompt_tokens += prompt_tokens
        self.openai_completion_tokens += completion_tokens

    def estimate_tokens(self, text: str) -> int:
        """Rough estimate: ~4 characters per token for English text."""
        return max(1, len(text) // 4)

    def add_tts_text(self, text: str):
        self.tts_characters += len(text)

    def add_audio_chunk(self):
        self.stt_audio_chunks += 1

    def add_turn(self):
        self.conversation_turns += 1

    def summary(self, rubric=None) -> str:
        total = self.openai_prompt_tokens + self.openai_completion_tokens
        # STT: nova-3 charges per minute; 16 kHz 16-bit PCM = 32 000 bytes/s
        # Each chunk from the browser is typically ~100 ms → ~3 200 bytes
        estimated_stt_seconds = (self.stt_audio_chunks * 3200) / 32000
        rubric_line = f"  Rubric: {rubric.status_line()}" if rubric else ""

        lines = [
            "",
            "=" * 50,
            "  SESSION USAGE SUMMARY",
            "=" * 50,
            f"  OpenAI gpt-4o-mini (estimated, ~4 chars/token):",
            f"    Prompt tokens:       {self.openai_prompt_tokens:>8}",
            f"    Completion tokens:   {self.openai_completion_tokens:>8}",
            f"    Total tokens:        {total:>8}",
            f"  Deepgram TTS  (aura-2-thalia-en):",
            f"    Characters sent:     {self.tts_characters:>8}",
            f"  Deepgram STT  (nova-3):",
            f"    Audio chunks sent:   {self.stt_audio_chunks:>8}",
            f"    ~Audio seconds:      {estimated_stt_seconds:>8.1f}",
            f"  Conversation turns:    {self.conversation_turns:>8}",
        ]
        if rubric_line:
            lines.append(rubric_line)
        lines += ["=" * 50, ""]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Session rubric — tracks required info + enforces conversation state
# ---------------------------------------------------------------------------
class SessionRubric:
    MAX_ATTEMPTS = 3

    MAX_FETCH_ATTEMPTS = 2

    def __init__(self):
        self.mobile_number: str | None = None
        self.is_appointment: bool | None = None
        self.appointment_date: str | None = None
        self.appointment_time: str | None = None
        self.is_confirmed: bool = False
        self.mobile_attempts: int = 0
        self.fetch_attempts: int = 0
        self.pending_close: bool = False
        self.state: State = State.AWAITING_MOBILE
        self.patient_type: str | None = None

    def allowed_functions(self) -> set:
        allowed = ALLOWED.get(self.state, set()).copy()
        if self.state == State.AWAITING_PATIENT_INFO:
            if self.patient_type == "new":
                allowed.discard("fetch_patient")
            elif self.patient_type == "returning":
                allowed.discard("register_new_patient")
        # Block fetch_patient once we've hit the retry cap to prevent infinite re-fetching
        if self.fetch_attempts >= self.MAX_FETCH_ATTEMPTS:
            allowed.discard("fetch_patient")
        return allowed

    def advance(self, func_name: str, args: dict, result: dict):
        if "error" in result:
            if func_name == "fetch_patient":
                self.fetch_attempts += 1
                print(f"[RUBRIC] fetch_patient failed ({self.fetch_attempts}/{self.MAX_FETCH_ATTEMPTS})")
                # Unblock register_new_patient so the LLM can proceed as a new patient
                self.patient_type = "new"
                print("[RUBRIC] fetch_patient failed — patient_type flipped to 'new'")
            return
        if func_name == "register_mobile":
            self.state = State.AWAITING_INTENT
        elif func_name == "set_intent":
            self.state = State.AWAITING_PATIENT_TYPE if args.get("intent") == "booking" else State.AWAITING_NOTE
        elif func_name == "set_patient_type":
            self.patient_type = args.get("patient_type")
            self.state = State.AWAITING_PATIENT_INFO
        elif func_name in ("fetch_patient", "register_new_patient"):
            self.state = State.AWAITING_AVAILABILITY
        elif func_name == "check_availability":
            self.state = State.AWAITING_BOOK
        elif func_name in ("book_appointment", "leave_note"):
            self.state = State.DONE
        print(f"[STATE] → {self.state.value}")

    def capture_mobile(self, mobile: str):
        self.mobile_number = mobile
        print(f"[RUBRIC] Mobile captured: {mobile}")

    def record_mobile_failure(self):
        self.mobile_attempts += 1
        print(f"[RUBRIC] Mobile attempt failed ({self.mobile_attempts}/{self.MAX_ATTEMPTS})")
        if self.mobile_attempts >= self.MAX_ATTEMPTS:
            self.pending_close = True
            print("[RUBRIC] Max attempts reached — will close after goodbye audio")

    def confirm_appointment(self, date: str, time: str):
        self.is_appointment = True
        self.appointment_date = date
        self.appointment_time = time
        self.is_confirmed = True
        print(f"[RUBRIC] Appointment confirmed: {date} {time}")

    def confirm_note(self):
        self.is_appointment = False
        self.is_confirmed = True
        print("[RUBRIC] Note confirmed")

    def is_complete(self) -> bool:
        if not self.mobile_number:
            return False
        if not self.is_confirmed:
            return False
        if self.is_appointment and (not self.appointment_date or not self.appointment_time):
            return False
        return True

    def status_line(self) -> str:
        if self.is_complete():
            if self.is_appointment:
                return f"COMPLETE  (appointment: {self.appointment_date} {self.appointment_time})"
            return "COMPLETE  (note saved)"
        missing = []
        if not self.mobile_number:
            missing.append(f"mobile (attempts: {self.mobile_attempts}/{self.MAX_ATTEMPTS})")
        if self.is_appointment and not self.appointment_date:
            missing.append("date")
        if self.is_appointment and not self.appointment_time:
            missing.append("time")
        if not self.is_confirmed:
            missing.append("confirmation")
        return f"INCOMPLETE  (missing: {', '.join(missing)})  state={self.state.value}"


def sts_connect():
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise Exception("DEEPGRAM_API_KEY not found")
    return websockets.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        subprotocols=["token", api_key]
    )


def load_config():
    with open("config.json", "r") as f:
        config = json.load(f)

    # Inject today's date so the agent can resolve relative dates ("next Monday")
    today = datetime.now()
    today_str = f"{today.strftime('%Y-%m-%d')} ({today.strftime('%A')})"
    config["agent"]["think"]["prompt"] = (
        config["agent"]["think"]["prompt"].replace("{TODAY}", today_str)
    )

    openai_key = os.getenv("OPENAI_API_KEY")
    config["agent"]["think"]["endpoint"] = {
        "url": "https://api.openai.com/v1/chat/completions",
        "headers": {"Authorization": f"Bearer {openai_key}"}
    }

    return config


def execute_function_call(func_name, arguments):
    print(f"[FUNC] Executing {func_name} with {arguments}")
    if func_name in FUNCTION_MAP:
        result = FUNCTION_MAP[func_name](**arguments)
        print(f"[FUNC] Result: {result}")
        return result
    result = {"error": f"Unknown function: {func_name}"}
    print(f"[FUNC] Error: {result}")
    return result


def create_function_call_response(func_id, func_name, result):
    return {
        "type": "FunctionCallResponse",
        "id": func_id,
        "name": func_name,
        "content": json.dumps(result)
    }


async def handle_function_call_request(decoded, sts_ws, rubric: SessionRubric):
    func_id = "unknown"
    func_name = "unknown"
    try:
        for function_call in decoded.get("functions", []):
            func_name = function_call["name"]
            func_id = function_call["id"]
            arguments = json.loads(function_call["arguments"])
            print(f"[FUNC] Request: {func_name}, args: {arguments}")

            # State machine guard — block out-of-sequence calls
            if func_name not in rubric.allowed_functions():
                error = {
                    "error": "out_of_sequence",
                    "message": (
                        f"Cannot call {func_name} in state '{rubric.state.value}'. "
                        f"Allowed: {sorted(rubric.allowed_functions())}. "
                        "Follow the steps in order."
                    )
                }
                print(f"[STATE] Blocked {func_name} — state={rubric.state.value}")
                await sts_ws.send(json.dumps(create_function_call_response(func_id, func_name, error)))
                continue

            # Inject hold message before calls with API latency
            if func_name in HOLD_MESSAGES:
                await sts_ws.send(json.dumps({
                    "type": "InjectAgentMessage",
                    "message": HOLD_MESSAGES[func_name]
                }))

            # Run blocking I/O in a thread so the asyncio event loop stays free
            # (audio forwarding continues; Deepgram doesn't generate extra filler speech)
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: execute_function_call(func_name, arguments)
            )

            # Legacy rubric updates
            if func_name == "register_mobile":
                rubric.capture_mobile(arguments.get("mobile", ""))
            elif func_name == "mobile_attempt_failed":
                rubric.record_mobile_failure()
            elif func_name == "book_appointment" and "error" not in result:
                rubric.confirm_appointment(
                    arguments.get("date", ""),
                    arguments.get("time", "")
                )
            elif func_name == "leave_note" and "error" not in result:
                rubric.confirm_note()

            # Advance state machine
            rubric.advance(func_name, arguments, result)

            response = create_function_call_response(func_id, func_name, result)
            await sts_ws.send(json.dumps(response))
            print(f"[FUNC] Result: {result}")
    except Exception as e:
        print(f"[ERROR] Function call failed: {e}")
        await sts_ws.send(json.dumps(create_function_call_response(
            func_id, func_name, {"error": f"Function call failed: {str(e)}"}
        )))


async def handle_text_message(decoded, client_ws, sts_ws,
                              usage: UsageTracker, rubric: SessionRubric) -> bool:
    """Returns True if the server should close the connection after this message."""
    msg_type = decoded.get("type")
    print(f"[STS] Event: {msg_type}")

    # --- Barge-in: user started speaking, flush client audio queue ---
    if msg_type == "UserStartedSpeaking":
        print("[BARGE-IN] User started speaking")
        await client_ws.send(json.dumps({"type": "barge_in"}))

    # --- Agent lifecycle events ---
    elif msg_type in DEEPGRAM_TO_CLIENT:
        client_event = {"type": DEEPGRAM_TO_CLIENT[msg_type]}
        await client_ws.send(json.dumps(client_event))
        print(f"[FWD] {msg_type} → {client_event['type']}")
        # Close after goodbye audio finishes if rubric triggered a forced close
        if msg_type == "AgentAudioDone" and rubric.pending_close:
            print("[RUBRIC] Goodbye audio done — closing connection")
            return True

    # --- Forward conversation text + estimate token usage ---
    elif msg_type == "ConversationText":
        role = decoded.get("role", "")
        content = decoded.get("content", "")
        if role == "assistant":
            usage.add_tts_text(content)
            usage.add_turn()
            usage.add_llm_usage(0, usage.estimate_tokens(content))
            await client_ws.send(json.dumps({
                "type": "agent_response",
                "message": content
            }))
        elif role == "user":
            usage.add_turn()
            usage.add_llm_usage(usage.estimate_tokens(content), 0)
            await client_ws.send(json.dumps({
                "type": "stt_output",
                "transcript": content,
                "latency_ms": 0
            }))

    # --- Known Deepgram housekeeping events — no action needed ---
    elif msg_type in ("Welcome", "SettingsApplied", "History",
                      "FunctionCallResponse", "Error"):
        if msg_type == "Error":
            code = decoded.get("code", "")
            desc = decoded.get("description", "")
            if code == "CLIENT_MESSAGE_TIMEOUT":
                print(f"[STS] Session timed out (normal on disconnect): {desc}")
            else:
                print(f"[ERROR] Deepgram error [{code}]: {desc}")

    # --- Function calls ---
    elif msg_type == "FunctionCallRequest":
        await handle_function_call_request(decoded, sts_ws, rubric)

    # --- Log any other event types so we can discover new Deepgram events ---
    else:
        print(f"[STS] Unhandled event type '{msg_type}': {json.dumps(decoded)[:200]}")

    return False


async def sts_sender(sts_ws, audio_queue):
    print("[STS] Sender started")
    while True:
        chunk = await audio_queue.get()
        await sts_ws.send(chunk)


async def sts_receiver(sts_ws, client_ws, usage: UsageTracker, rubric: SessionRubric):
    print("[STS] Receiver started")
    async for message in sts_ws:
        if isinstance(message, str):
            decoded = json.loads(message)
            should_close = await handle_text_message(decoded, client_ws, sts_ws, usage, rubric)
            if should_close:
                return
        else:
            # Raw PCM audio — forward binary directly to client
            await client_ws.send(message)


async def client_receiver(client_ws, audio_queue, usage: UsageTracker):
    print("[WS] Receiver started")
    async for message in client_ws:
        try:
            if isinstance(message, bytes):
                usage.add_audio_chunk()
                audio_queue.put_nowait(message)
            else:
                print(f"[WS] Non-binary message ignored: {message[:80]}")
        except Exception as e:
            print(f"[ERROR] Client receive failed: {e}")
            break


async def client_handler(client_ws):
    print("[WS] Client connected")
    audio_queue = asyncio.Queue()
    usage = UsageTracker()
    rubric = SessionRubric()

    try:
        async with sts_connect() as sts_ws:
            config_message = load_config()
            await sts_ws.send(json.dumps(config_message))
            print("[STS] Config sent")

            tasks = [
                asyncio.ensure_future(sts_sender(sts_ws, audio_queue)),
                asyncio.ensure_future(sts_receiver(sts_ws, client_ws, usage, rubric)),
                asyncio.ensure_future(client_receiver(client_ws, audio_queue, usage)),
            ]

            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

            for task in tasks:
                task.cancel()

            await client_ws.close()
            print("[WS] Connection closed")
    finally:
        print(usage.summary(rubric))


async def health_check(connection, request):
    """HTTP health endpoint — returns JSON status of all required config."""
    if request.path == "/health":
        checks = {
            "deepgram_api_key":        bool(os.getenv("DEEPGRAM_API_KEY")),
            "openai_api_key":          bool(os.getenv("OPENAI_API_KEY")),
            "google_sheet_id":         bool(os.getenv("GOOGLE_SHEET_ID")),
            "google_credentials":      bool(os.getenv("GOOGLE_CREDENTIALS_JSON")),
            "appointment_api_url":     bool(os.getenv("APPOINTMENT_API_URL")),
        }
        all_ok = all(checks.values())
        body = json.dumps({
            "status": "ok" if all_ok else "degraded",
            "checks": checks,
        }, indent=2) + "\n"
        status = HTTPStatus.OK if all_ok else HTTPStatus.SERVICE_UNAVAILABLE
        return connection.respond(status, body)


async def keep_alive():
    """Ping /health every 14 minutes so Render's free tier doesn't spin down."""
    base_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base_url:
        print("[KEEPALIVE] RENDER_EXTERNAL_URL not set — skipping keep-alive")
        return
    await asyncio.sleep(30)  # let the server finish starting up
    while True:
        await asyncio.sleep(14 * 60)
        try:
            urllib.request.urlopen(f"{base_url}/health", timeout=10)
            print("[KEEPALIVE] Ping sent")
        except Exception as e:
            print(f"[KEEPALIVE] Ping failed: {e}")


async def main():
    port = int(os.getenv("PORT", 5000))
    await websockets.serve(client_handler, "0.0.0.0", port,
                           process_request=health_check)
    print(f"[SERVER] Started on ws://0.0.0.0:{port}")
    asyncio.ensure_future(keep_alive())
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())