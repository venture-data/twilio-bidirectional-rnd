import os
import json
import traceback
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, Response
from fastapi.responses import HTMLResponse
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client
from elevenlabs import ElevenLabs
from elevenlabs.conversational_ai.conversation import Conversation, ConversationConfig
from twilio_audio_interface import TwilioAudioInterface
from starlette.websockets import WebSocketDisconnect
from fastapi.templating import Jinja2Templates
from urllib.parse import urlencode

load_dotenv()

# Jinja2 templates
templates = Jinja2Templates(directory="templates")

TWILIO_ACCOUNT_SID = os.getenv("ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("AUTH_TOKEN")
AGENT_ID = os.getenv("AGENT_ID")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

app = FastAPI()

# Initialize Twilio client
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


class OutBoundRequest(BaseModel):
    to: str
    name: str
    from_: Optional[str] = "+17753177891" # +15512967933 +12185857512 +17753177891
    twilio_call_url: Optional[str] = "https://deadly-adapted-joey.ngrok-free.app/twilio/twiml"

# local https://handler.twilio.com/twiml/EH27222b10726db3571bf103a8c4b222b5
# US https://handler.twilio.com/twiml/EHcbb679b885a518afb1af0ae52dfcc870
# ME https://handler.twilio.com/twiml/EH0e5171711df88a1c641f721ac0ae7049

@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/twilio/inbound_call")
async def handle_incoming_call(request: Request):
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "Unknown")
    from_number = form_data.get("From", "Unknown")
    print(f"Incoming call: CallSid={call_sid}, From={from_number}")

    response = VoiceResponse()
    connect = Connect()
    connect.stream(url=f"wss://{request.url.hostname}/media-stream-eleven")
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.post("/twilio/outbound_call")
async def initiate_outbound_call(request: OutBoundRequest):
    """
    Endpoint to initiate an outbound call via Twilio.
    Expects JSON payload: { "to": "+1234567890", "from_" [OPTIONAL]: "+1098765432", "twiml_url" [OPTIONAL]: "https://....", "name": "custom_name" }
    """
    to_number = request.to
    from_number = request.from_
    twiml_url = request.twilio_call_url
    name = request.name

    if not to_number or not from_number:
        return {"error": "Missing 'to' or 'from' phone number"}

    if not twiml_url:
        return {"error": "Missing 'twiml_url'"}

    if name:
        twiml_url = f"{twiml_url}?{urlencode({'name': name})}"

    call = twilio_client.calls.create(
        to=to_number,
        from_=from_number,
        url=twiml_url
    )

    return {"status": "initiated", "call_sid": call.sid}

@app.post("/twilio/twiml")
async def incoming_call(request: Request):
    # Extract the 'name' from query parameters
    name = request.query_params.get("name", "DefaultName")
    print(f"Making an outgoing call to: {name}")
    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Connect>
                <Stream url="wss://deadly-adapted-joey.ngrok-free.app/media-stream-eleven">
                    <Parameter name="name" value="{name}" />
                </Stream>
            </Connect>
        </Response>"""
    return Response(content=twiml_response, media_type="application/xml")

@app.websocket("/media-stream-eleven")
async def handle_media_stream(websocket: WebSocket):
    await websocket.accept()
    print("WebSocket connection opened")

    audio_interface = TwilioAudioInterface(websocket)
    eleven_labs_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

    local_call_sid = None
    conversation = None  # Initialize conversation as None

    try:
        async for message in websocket.iter_text():
            if not message:
                continue
            
            data = json.loads(message)
            event_type = data.get("event")
            
            # Handle the message to update audio_interface's state
            await audio_interface.handle_twilio_message(data)
            
            if event_type == "start":
                local_call_sid = data["start"]["callSid"]
                # Extract the name after processing the start event
                name = audio_interface.customParameters.get("name", "DefaultName")
                
                # Initialize and start the conversation here
                conversation = Conversation(
                    client=eleven_labs_client,
                    config=ConversationConfig(
                        conversation_config_override={
                            "agent": {
                                "prompt": {
                                    "prompt": "The customer's x account balance is $900. They are x in LA."
                                },
                                "first_message": f"Hi {name}, how can I help you today?",
                            }
                        }
                    ),
                    agent_id=os.getenv("AGENT_ID"),
                    requires_auth=True,
                    audio_interface=audio_interface,
                    callback_agent_response=lambda text: print(f"Agent: {text}"),
                    callback_user_transcript=lambda text: print(f"User: {text}"),
                )
                conversation.start_session()
                print("Conversation started")

    except WebSocketDisconnect:
        print("WebSocket disconnected")
    except Exception:
        print("Error occurred in WebSocket handler:")
        traceback.print_exc()
    finally:
        try:
            if conversation is not None:
                conversation.end_session()
                print(f"Call SID: {local_call_sid}")
                if local_call_sid:
                    twilio_client.calls(local_call_sid).update(status="completed")
                conversation.wait_for_session_end()
                print("Conversation ended")
        except Exception:
            print("Error ending conversation session:")
            traceback.print_exc()

# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)
