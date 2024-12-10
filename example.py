from fastapi import FastAPI, WebSocket, Request, WebSocketDisconnect, BackgroundTasks
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
import asyncio
import os
from dotenv import load_dotenv
import assemblyai as aai
from openai import OpenAI
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse
import base64
import json

load_dotenv()

app = FastAPI()

# Configuration
TWILIO_AUTH_TOKEN = os.getenv('AUTH_TOKEN')
TWILIO_SID = os.getenv('ACCOUNT_SID')
ASSEMBLY_AI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER")

# Initialize clients
twilio_client = Client(TWILIO_SID, TWILIO_AUTH_TOKEN)
openai_client = OpenAI(api_key=OPENAI_API_KEY)
aai.settings.api_key = ASSEMBLY_AI_API_KEY


# AssemblyAI Callbacks
def on_open(session_opened: aai.RealtimeSessionOpened):
    print("Session opened. Session ID:", session_opened.session_id)


def on_data(transcript: aai.RealtimeTranscript):
    """
    Process the transcription result from AssemblyAI.
    """
    if not transcript.text:
        return

    if isinstance(transcript, aai.RealtimeFinalTranscript):
        print("\nFinal transcript:", transcript.text)
    else:
        print("\rLive transcript:", transcript.text, end="")


def on_error(error: aai.RealtimeError):
    print("An error occurred:", error)


def on_close():
    print("Session closed.")


class CallRequest(BaseModel):
    to: str

@app.post("/start-call/")
async def start_call(request: CallRequest):
    to = request.to
    """
    Start an outbound call using Twilio.
    """
    twilio_call_url = "https://deadly-adapted-joey.ngrok-free.app/twilio-webhook/"  # Use your ngrok URL.

    call = twilio_client.calls.create(
        from_=TWILIO_NUMBER,
        to=to,
        url=twilio_call_url
    )
    return {"status": "Call initiated", "sid": call.sid}

@app.post("/twilio-webhook/")
async def twilio_webhook(request: Request):
    """
    Handle incoming Twilio webhook events, greet the caller, and initiate Media Streams.
    """
    data = await request.form()
    call_sid = data.get("CallSid", "")
    print(f'call sid: {call_sid}')

    response = VoiceResponse()

    # Greet the caller
    response.say("Hello, how can I help you?", voice="alice")

    # Start the media stream with `call_sid` as a query parameter
    response.start().stream(name="media", url=f"wss://deadly-adapted-joey.ngrok-free.app/ws/transcribe?call_sid={call_sid}")

    # Keep the call alive
    response.pause(length=10)

    return PlainTextResponse(str(response))


@app.websocket("/ws/transcribe/")
async def websocket_endpoint(websocket: WebSocket, background_tasks: BackgroundTasks):
    """
    WebSocket endpoint for receiving real-time audio, transcribing it,
    and responding using ChatGPT.
    """
    # Parse `call_sid` from WebSocket query parameters
    query_params = websocket.scope.get("query_string", b"").decode()
    call_sid = None

    for param in query_params.split("&"):
        if param.startswith("call_sid="):
            call_sid = param.split("=")[1]
            break

    if not call_sid:
        print("Missing or invalid call_sid. Rejecting connection.")
        await websocket.close(code=403)
        return

    print(f"WebSocket connection accepted for Call SID: {call_sid}")
    await websocket.accept()

    # Initialize the AssemblyAI RealtimeTranscriber
    transcriber = aai.RealtimeTranscriber(
        sample_rate=16_000,
        on_open=on_open,
        on_data=on_data,
        on_error=on_error,
        on_close=on_close,
    )

    await asyncio.to_thread(transcriber.connect)

    transcription_buffer = ""
    last_transcription_time = None

    try:
        while True:
            try:
                # Receive message from WebSocket
                message = await websocket.receive()

                if "text" in message:
                    msg_data = message["text"]

                    # Decode Twilio Media Streams JSON payload
                    media_data = json.loads(msg_data).get("media", {}).get("payload", "")
                    audio_data = base64.b64decode(media_data)

                    # Stream audio to AssemblyAI
                    await asyncio.to_thread(transcriber.stream, audio_data)

                # Process transcription updates
                if hasattr(transcriber, "text") and transcriber.text:
                    transcription_buffer += transcriber.text
                    last_transcription_time = asyncio.get_event_loop().time()

                # After 2 seconds of inactivity, process the transcription
                if last_transcription_time and asyncio.get_event_loop().time() - last_transcription_time > 2:
                    if transcription_buffer.strip():
                        # Send transcription to ChatGPT
                        chatgpt_response = await generate_response(transcription_buffer)

                        # Convert ChatGPT response to speech
                        audio_url = await synthesize_speech(chatgpt_response)

                        # Dynamically update Twilio playback
                        background_tasks.add_task(update_twilio_playback, call_sid, audio_url)

                        # Clear transcription buffer
                        transcription_buffer = ""

            except WebSocketDisconnect:
                print("WebSocket disconnected by client.")
                break

    except Exception as e:
        print(f"Unexpected error: {e}")
    finally:
        # Close the transcriber
        await asyncio.to_thread(transcriber.close)
        print("Session closed.")

async def update_twilio_playback(call_sid: str, audio_url: str):
    """
    Update Twilio's current call with a new TwiML to play the synthesized speech.
    """
    twiml_response = f"""
    <?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Play>{audio_url}</Play>
    </Response>
    """
    try:
        twilio_client.calls(call_sid).update(twiml=twiml_response)
        print(f"Updated playback for Call SID: {call_sid}")
    except Exception as e:
        print(f"Error updating Twilio playback: {e}")


@app.post("/generate-response/")
async def generate_response(prompt: str):
    """
    Generate a response using OpenAI's GPT model based on the given prompt.
    """
    response = openai_client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt},
        ],
    )
    return {"response": response.choices[0].message.content}


async def synthesize_speech(text: str) -> str:
    """
    Use AssemblyAI to convert OpenAI response text to speech.
    """
    async with aai.TextToSpeechClient() as tts_client:
        response = await tts_client.create(
            text=text,
            voice="en_us_jenny"
        )
        return response.audio_url
