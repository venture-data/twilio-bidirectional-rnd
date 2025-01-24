# Standard libraries
import os
import json
import traceback

from typing import Optional
from pydantic import BaseModel
from urllib.parse import urlencode

from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi import FastAPI, Request, WebSocket, Response, HTTPException

from starlette.websockets import WebSocketDisconnect

from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client

from elevenlabs import ElevenLabs
from elevenlabs.conversational_ai.conversation import Conversation, ConversationConfig

from twilio_service import TwilioAudioInterface, TwilioService, RecordingsHandler
from utils import parse_time_to_utc_plus_5

import logging
from dotenv import load_dotenv

# Initialize logging
logging.basicConfig(level=logging.DEBUG,  # Set to DEBUG for detailed logs
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

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
twilio_service = TwilioService(
    account_sid=TWILIO_ACCOUNT_SID,
    auth_token=TWILIO_AUTH_TOKEN
)

recordings_handler = RecordingsHandler(
    twilio_service=twilio_service,
    recordings_dir="recordings"
)

processed_recordings = set()

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

@app.post("/recording-complete")
async def handle_recording_complete(request: Request):
    """
    Endpoint to handle 'action' callbacks for recording completion.
    """
    form_data = await request.form()
    call_sid = form_data.get("CallSid")

    if not call_sid:
        logger.warning("CallSid is missing in the form data.")
        raise HTTPException(status_code=400, detail="Missing CallSid")

    try:
        logger.info(f"Processing CallSid: {call_sid}")
        
        # Fetch recordings for the call
        logger.debug("Fetching recordings for the call.")
        recordings = twilio_service.list_recordings(call_sid=call_sid)
        logger.debug(f"Recordings fetched: {recordings}")

        if not recordings:
            logger.error("No recordings found for the provided CallSid.")
            raise HTTPException(status_code=404, detail="No recordings found for the provided CallSid")
        
        # Filter finalized recordings with duration > 0
        finalized_recordings = [r for r in recordings if r.duration and int(r.duration) > 0]
        logger.debug(f"Finalized recordings: {[{'sid': r.sid, 'duration': r.duration} for r in finalized_recordings]}")

        if not finalized_recordings:
            logger.error("No finalized recordings found for the provided CallSid.")
            raise HTTPException(status_code=404, detail="No finalized recordings found for the provided CallSid")
        
        # Find the Longest recording
        longest_recording = max(finalized_recordings, key=lambda r: int(r.duration))
        logger.debug(f"Longest recording SID: {longest_recording.sid}, Duration: {longest_recording.duration}")
        
        # Delete other recordings
        for recording in recordings:
            if recording.sid != longest_recording.sid:
                logger.debug(f"Deleting recording SID: {recording.sid}")
                twilio_service.delete_recording(recording.sid)
        
        # Check if already processed
        if longest_recording.sid in processed_recordings:
            logger.info(f"Recording SID {longest_recording.sid} already processed. No action taken.")
            return {"message": f"Recording SID {longest_recording.sid} already processed. No action taken."}
        
        # Download the Longest recording
        recording_path = recordings_handler.download_recording(longest_recording.sid)
        logger.debug(f"Recording downloaded to: {recording_path}")
        
        if not recording_path:
            logger.error("Failed to download recording.")
            raise HTTPException(status_code=500, detail="Failed to download recording.")
        
        # Upload to GCS and delete local file and Twilio recording
        recording_public_url = recordings_handler.upload_and_cleanup(recording_path, longest_recording.sid)
        logger.debug(f"Recording public URL: {recording_public_url}")
        
        if not recording_public_url:
            logger.error("Failed to upload recording to GCS.")
            raise HTTPException(status_code=500, detail="Failed to upload recording to GCS.")
        
        # Fetch call details
        call_details_dict = twilio_service.fetch_call_details(call_sid)
        logger.debug(f"Call details fetched: {call_details_dict}")
        
        # Convert times to UTC+5
        call_details_dict["start_time"] = parse_time_to_utc_plus_5(call_details_dict.get("start_time"))
        call_details_dict["end_time"] = parse_time_to_utc_plus_5(call_details_dict.get("end_time"))
        
        # Add recording SID
        call_details_dict["recording_sid"] = longest_recording.sid
          
        # Mark as processed
        processed_recordings.add(longest_recording.sid)
        logger.debug(f"Marked recording SID {longest_recording.sid} as processed.")
        
        
        logger.info(f"Successfully processed CallSid {call_sid}.")
        return {"message": f"Successfully processed CallSid {call_sid}."}
    
    except HTTPException as e:
        logger.error(f"HTTPException occurred: {e.detail}")
        raise e
    except Exception as e:
        logger.exception(f"Unexpected error processing CallSid {call_sid}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process recordings: {str(e)}")

@app.post("/call-status")
async def call_status_callback(request: Request):
    """
    Endpoint to handle Twilio call status callbacks and save details into the database.
    """
    form_data = await request.form()
    call_sid = form_data.get("CallSid")
    call_status = form_data.get("CallStatus")
    from_number = form_data.get("From")
    to_number = form_data.get("To")

    print(f"Call SID: {call_sid}, Status: {call_status}, From: {from_number}, To: {to_number}")

    return {"message": "Call status received and saved successfully."}

@app.websocket("/media-stream-eleven")
async def handle_media_stream(websocket: WebSocket):
    await websocket.accept()
    print("WebSocket connection opened")

    audio_interface = TwilioAudioInterface(websocket)
    eleven_labs_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

    local_call_sid = None
    conversation = None  # Initialize conversation as None
    conversation_logs = []  # List to store conversation logs

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
                                    "prompt": "The customer's account balance is $900. They are Top G in LA."
                                    " always make haste in ending conversations with customers, if he/she indicates to do so."
                                    " Don't ask follow up questions if the customer is not interested in continuing the conversation."
                                },
                                "first_message": f"Hi {name}, how can I help you today?",
                            }
                        }
                    ),
                    agent_id=os.getenv("AGENT_ID"),
                    requires_auth=True,
                    audio_interface=audio_interface,
                    callback_agent_response=lambda text: conversation_logs.append(("Agent", text)),
                    callback_user_transcript=lambda text: conversation_logs.append(("User", text)),
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

                # Print the conversation logs after the session has ended
                print("\nConversation Logs:")
                for speaker, text in conversation_logs:
                    print(f"{speaker}: {text}")

        except Exception:
            print("Error ending conversation session:")
            traceback.print_exc()

# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)
