import asyncio
import base64
import json
import websockets
import os
from dotenv import load_dotenv
from pharmacy_functions import FUNCTION_MAP

load_dotenv()


def sts_connect():
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise Exception("DEEPGRAM_API_KEY not found")

    print("[STS] Connecting to Deepgram...")
    sts_ws = websockets.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        subprotocols=["token", api_key]
    )
    return sts_ws


def load_config():
    with open("config.json", "r") as f:
        config = json.load(f)
        print("[CONFIG] Loaded config")
        return config


async def handle_barge_in(decoded, twilio_ws, streamsid):
    if decoded.get("type") == "UserStartedSpeaking":
        print("[BARGE-IN] User started speaking")



def execute_function_call(func_name, arguments):
    print(f"[FUNC] Executing {func_name} with {arguments}")
    if func_name in FUNCTION_MAP:
        result = FUNCTION_MAP[func_name](**arguments)
        print(f"[FUNC] Result: {result}")
        return result
    else:
        result = {"error": f"Unknown function: {func_name}"}
        print("[FUNC] Error:", result)
        return result



def create_function_call_response(func_id, func_name, result):
    return {
        "type": "FunctionCallResponse",
        "id": func_id,
        "name": func_name,
        "content": json.dumps(result)
    }


async def handle_function_call_request(decoded, sts_ws):
    try:
        for function_call in decoded.get("functions", []):
            func_name = function_call["name"]
            func_id = function_call["id"]
            arguments = json.loads(function_call["arguments"])

            print(f"[FUNC] Request: {func_name}, args: {arguments}")

            result = execute_function_call(func_name, arguments)

            function_result = create_function_call_response(func_id, func_name, result)
            await sts_ws.send(json.dumps(function_result))
            print(f"[FUNC] Sent response")

    except Exception as e:
        print(f"[ERROR] Function call failed: {e}")


async def handle_text_message(decoded, twilio_ws, sts_ws, streamsid):
    print(f"[STS->TEXT] {decoded.get('type')}")
    await handle_barge_in(decoded, twilio_ws, streamsid)

    if decoded.get("type") == "FunctionCallRequest":
        await handle_function_call_request(decoded, sts_ws)


async def sts_sender(sts_ws, audio_queue):
    print("[STS] Sender started")
    while True:
        chunk = await audio_queue.get()
        print(f"[STS] Sending audio chunk: {len(chunk)} bytes")
        await sts_ws.send(chunk)


async def sts_receiver(sts_ws, twilio_ws, streamsid_queue):
    print("[STS] Receiver started")

    streamsid = "webapp"  # replaced Twilio dependency

    async for message in sts_ws:
        if isinstance(message, str):
            print("[STS] Text message received")
            decoded = json.loads(message)
            await handle_text_message(decoded, twilio_ws, sts_ws, streamsid)
            continue

        print(f"[STS] Audio received: {len(message)} bytes")

        # Send raw audio directly back to frontend
        await twilio_ws.send(message)


async def twilio_receiver(twilio_ws, audio_queue, streamsid_queue):
    print("[WS] Receiver started (webapp mode)")

    async for message in twilio_ws:
        try:
            if isinstance(message, bytes):
                print(f"[WS] Received audio: {len(message)} bytes")
                audio_queue.put_nowait(message)
            else:
                print("[WS] Received non-bytes message (ignored)")
        except Exception as e:
            print(f"[ERROR] WS receive failed: {e}")
            break


async def twilio_handler(twilio_ws):
    print("[WS] Client connected")

    audio_queue = asyncio.Queue()
    streamsid_queue = asyncio.Queue()

    async with sts_connect() as sts_ws:
        config_message = load_config()
        await sts_ws.send(json.dumps(config_message))
        print("[STS] Config sent")

        await asyncio.wait(
            [
                asyncio.ensure_future(sts_sender(sts_ws, audio_queue)),
                asyncio.ensure_future(sts_receiver(sts_ws, twilio_ws, streamsid_queue)),
                asyncio.ensure_future(twilio_receiver(twilio_ws, audio_queue, streamsid_queue)),
            ]
        )

        await twilio_ws.close()
        print("[WS] Connection closed")


async def main():
    await websockets.serve(twilio_handler, "localhost", 5000)
    print("[SERVER] Started on ws://localhost:5000")
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
