from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from twilio.rest import Client
from dotenv import load_dotenv
import websockets
import asyncio
import os
import json
from typing import Optional

# Load environment variables
load_dotenv()

# Configuration
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_AUTH_TOKEN = os.getenv('AUTH_TOKEN')
TWILIO_SID = os.getenv('ACCOUNT_SID')
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER")
PORT = int(os.getenv("PORT", 8080))

if not OPENAI_API_KEY:
    raise EnvironmentError("Missing OpenAI API key. Please set it in the .env file.")
if not TWILIO_AUTH_TOKEN or not TWILIO_SID or not TWILIO_NUMBER:
    raise EnvironmentError("Missing Twilio credentials. Please set them in the .env file.")

# FastAPI app
app = FastAPI()

# Jinja2 templates
templates = Jinja2Templates(directory="templates")

# Twilio client
twilio_client = Client(TWILIO_SID, TWILIO_AUTH_TOKEN)

# Constants
SYSTEM_MESSAGE = (
    "You are an English-speaking Customer Support Representative named Alex from Cardinal Plumbing. You've a Arizona Accent. "
    "You are reaching out to a customer named Nikolai regarding an upcoming Plumbing Maintenance due in February. "
    "Greet him politely, confirm his availability, and book his appointment at a suitable date/time. "
    "If he asks questions or prefers a different date, respond helpfully and professionally. "
    "Keep the conversation natural and polite and speak fast. "
    "Make sure to keep an Arizona accent and add some filler words like 'uh', 'um', hmm, 'let me check', 'alright.. hmm', 'like', 'you know', etc. to sound more natural. "
    "Also, although we will keep the output short, but if it is slightly longer, talk naturally, add filler word, take a breath in between. "
    "Don't sound too excited, just talk in a normal, calm tone."
)
VOICE = "verse"
LOG_EVENT_TYPES = [
    "error",
    "response.content.done",
    "rate_limits.updated",
    "response.done",
    "input_audio_buffer.committed",
    "input_audio_buffer.speech_stopped",
    "input_audio_buffer.speech_started",
    "session.created",
]
SHOW_TIMING_MATH = False

# Models
class CallRequest(BaseModel):
    to: str
    twilio_call_url: Optional[str] = "https://handler.twilio.com/twiml/EH0db6372522f950d90f33662d5f3b3881"

# Routes
@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/start-call/")
async def start_call(request: CallRequest):
    to = request.to
    call = twilio_client.calls.create(
        from_=TWILIO_NUMBER,
        to=to,
        url=request.twilio_call_url
    )
    return {"status": "Call initiated", "sid": call.sid}

@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    print("Client connected")

    stream_sid = None
    latest_media_timestamp = 0
    last_assistant_item = None
    mark_queue = []
    response_start_timestamp_twilio = None

    try:
        openai_ws = await websockets.connect(
            "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01",
            additional_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "realtime=v1",
            },
        )

        async def initialize_session():
            print("Initializing session")
            session_update = {
                "type": "session.update",
                "session": {
                    "turn_detection": {"type": "server_vad"},
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "voice": VOICE,
                    "instructions": SYSTEM_MESSAGE,
                    "modalities": ["text", "audio"],
                    "temperature": 0.6,
                },
            }
            await openai_ws.send(json.dumps(session_update))
            await send_initial_conversation_item()

        async def send_initial_conversation_item():
            initial_conversation_item = {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "The upcoming Plumbing Maintenance due in February. "
                                "You'll ask the user about it, keep the conversation to the points, avoid hallucinations. "
                                "Just make up some date and time to book the appointment. For context Today is 14th January 2025, "
                                "To book an appointment, you can say something like 'I can book the appointment for you on 15th February at 10:00 AM'. "
                                "Start with greeting Nikolai, your name is Alex from Cardinal Plumbing. "
                                "Ideally, we want to book the appointment on 10th February at 10:00 AM. "
                                "After greeting, wait for his response before continuing the conversation to keep it natural. "
                                "For context, the working hours are from 9:00 AM to 5:00 PM, Monday to Friday. "
                                "Remember to Speak Fast. Make sure to keep an Arizona accent and add some filler words like 'uh', 'um', hmm, 'let me check', 'alright.. hmm', 'like', 'you know', etc. to sound more natural. "
                                "Don't sound too excited, just talk in a normal tone."
                            ),
                        }
                    ],
                },
            }
            await openai_ws.send(json.dumps(initial_conversation_item))
            await openai_ws.send(json.dumps({"type": "response.create"}))

        async def handle_speech_started_event():
            nonlocal mark_queue, response_start_timestamp_twilio, last_assistant_item
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_time = int(latest_media_timestamp) - int(response_start_timestamp_twilio)
                if SHOW_TIMING_MATH:
                    print(f"Elapsed time for truncation: {elapsed_time}ms")

                if last_assistant_item:
                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": elapsed_time,
                    }
                    await openai_ws.send(json.dumps(truncate_event))

                await websocket.send_json({"event": "clear", "streamSid": stream_sid})

                # Reset
                mark_queue = []
                last_assistant_item = None
                response_start_timestamp_twilio = None

        async def send_mark():
            if stream_sid:
                mark_event = {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": "responsePart"},
                }
                await websocket.send_json(mark_event)
                mark_queue.append("responsePart")

        async def handle_openai_ws():
            nonlocal response_start_timestamp_twilio, last_assistant_item, latest_media_timestamp
            async for message in openai_ws:
                response = json.loads(message)

                if response.get("type") in LOG_EVENT_TYPES:
                    print(f"Received event: {response['type']}", response)

                if response.get("type") == "response.audio.delta" and response.get("delta"):
                    audio_delta = {
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": response["delta"]},
                    }
                    await websocket.send_json(audio_delta)

                    if not response_start_timestamp_twilio:
                        response_start_timestamp_twilio = latest_media_timestamp
                        if SHOW_TIMING_MATH:
                            print(f"Start timestamp: {response_start_timestamp_twilio}ms")

                    if response.get("item_id"):
                        last_assistant_item = response["item_id"]

                    await send_mark()

                if response.get("type") == "input_audio_buffer.speech_started":
                    await handle_speech_started_event()

        await initialize_session()
        asyncio.create_task(handle_openai_ws())

        while True:
            data = await websocket.receive_json()
            if data.get("event") == "media":
                latest_media_timestamp = data["media"]["timestamp"]
                if SHOW_TIMING_MATH:
                    print(f"Media timestamp: {latest_media_timestamp}ms")
                audio_append = {
                    "type": "input_audio_buffer.append",
                    "audio": data["media"]["payload"],
                }
                await openai_ws.send(json.dumps(audio_append))
            elif data.get("event") == "start":
                stream_sid = data["start"]["streamSid"]
                print("Stream started:", stream_sid)
                response_start_timestamp_twilio = None
                latest_media_timestamp = 0
            elif data.get("event") == "mark":
                if mark_queue:
                    mark_queue.pop(0)

    except WebSocketDisconnect:
        print("Client disconnected")
        await openai_ws.close()
    except Exception as e:
        print(f"Error: {e}")
        await openai_ws.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
