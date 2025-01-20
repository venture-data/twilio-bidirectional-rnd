import os
import json
import traceback
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client
from elevenlabs import ElevenLabs
from elevenlabs.conversational_ai.conversation import Conversation
from twilio_audio_interface import TwilioAudioInterface
from starlette.websockets import WebSocketDisconnect

load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("AUTH_TOKEN")
AGENT_ID = os.getenv("AGENT_ID")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

app = FastAPI()

# Initialize Twilio client
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


class OutBoundRequest(BaseModel):
    to: str
    from_: Optional[str] = "+17753177891"
    twiml_url: Optional[str] = "https://handler.twilio.com/twiml/EH27222b10726db3571bf103a8c4b222b5"


@app.get("/")
async def root():
    return {"message": "Twilio-ElevenLabs Integration Server (Outbound Example)"}


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
    Expects JSON payload: { "to": "+1234567890", "from_" [OPTIONAL]: "+1098765432", "twiml_url" [OPTIONAL]: "https://...." }
    """
    to_number = request.to
    from_number = request.from_

    if not to_number or not from_number:
        return {"error": "Missing 'to' or 'from' phone number"}

    # Make the outbound call
    call = twilio_client.calls.create(
        to=to_number,
        from_=from_number,
        url=request.twiml_url
    )

    return {"status": "initiated", "call_sid": call.sid}


# @app.post("/twilio/outbound_call_response")
# async def outbound_call_response(request: Request):
#     """
#     Twilio will hit this endpoint after the call is answered.
#     We respond with TwiML to connect to our WebSocket.
#     """
#     response = VoiceResponse()
#     connect = Connect()
#     connect.stream(url=f"wss://{request.url.hostname}/media-stream-eleven")
#     response.append(connect)
#     return HTMLResponse(content=str(response), media_type="application/xml")


@app.websocket("/media-stream-eleven")
async def handle_media_stream(websocket: WebSocket):
    await websocket.accept()
    print("WebSocket connection opened")

    audio_interface = TwilioAudioInterface(websocket)
    eleven_labs_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)
    
    local_call_sid = None

    try:
        conversation = Conversation(
            client=eleven_labs_client,
            agent_id=AGENT_ID,
            requires_auth=True,  # Security > Enable authentication
            audio_interface=audio_interface,
            callback_agent_response=lambda text: print(f"Agent: {text}"),
            callback_user_transcript=lambda text: print(f"User: {text}"),
        )

        conversation.start_session()
        print("Conversation started")

        async for message in websocket.iter_text():
            if not message:
                continue
            
            data = json.loads(message)
            
            if data.get("event") == "start":
                local_call_sid = data["start"]["callSid"]
            await audio_interface.handle_twilio_message(json.loads(message))

    except WebSocketDisconnect:
        print("WebSocket disconnected")
    except Exception:
        print("Error occurred in WebSocket handler:")
        traceback.print_exc()
    finally:
        try:
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
