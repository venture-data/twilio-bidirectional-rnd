import os
import re
import json
import traceback

from enum import Enum
from typing import Optional
from pydantic import BaseModel
from urllib.parse import urlencode

from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi import FastAPI, Request, WebSocket, Response, HTTPException

from starlette.websockets import WebSocketDisconnect

from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client

from elevenlabs.conversational_ai.conversation import Conversation, ConversationConfig, ClientTools
from elevenlabs import (
    ElevenLabs,
    ConversationalConfig,
    AgentConfig,
    PromptAgent,
    AsrConversationalConfig,
    ConversationConfig as cc,
    TurnConfig,
    TtsConversationalConfig,
    AgentPlatformSettings,
    ConversationInitiationClientDataConfig,
    ConversationConfigClientOverrideConfig,
    AgentConfigOverrideConfig,
    PromptAgentOverrideConfig,
    ClientToolConfig
)

from twilio_service import TwilioAudioInterface, TwilioService, RecordingsHandler
from utils import parse_time_to_utc_plus_5

from dotenv import load_dotenv

load_dotenv()

# Jinja2 templates
templates = Jinja2Templates(directory="templates")

TWILIO_ACCOUNT_SID = os.getenv("ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("AUTH_TOKEN")
AGENT_ID = os.getenv("AGENT_ID")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

app = FastAPI()

eleven_labs_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

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

class OutBoundRequest(BaseModel):
    to: str
    name: str
    agent_id: Optional[str] = os.getenv("AGENT_ID")
    from_: Optional[str] = "+17753177891" # +15512967933 +12185857512 +17753177891
    twilio_call_url: Optional[str] = "https://deadly-adapted-joey.ngrok-free.app/twilio/twiml"
    recording_callback_url: Optional[str] = "https://deadly-adapted-joey.ngrok-free.app/twilio/recording-call-back"
    status_callback_url: Optional[str] = "https://deadly-adapted-joey.ngrok-free.app/twilio/call-status"

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
    agent_id = request.agent_id

    if not to_number or not from_number:
        return {"error": "Missing 'to' or 'from' phone number"}

    if not twiml_url:
        return {"error": "Missing 'twiml_url'"}

    if name:
        twiml_url = f"{twiml_url}?{urlencode({'name': name})}"
    if agent_id:
        twiml_url = f"{twiml_url}&{urlencode({'agent_id': agent_id})}"

    call = twilio_client.calls.create(
        record=True,
        to=to_number,
        from_=from_number,
        url=twiml_url,
        recording_status_callback=request.recording_callback_url,
        recording_status_callback_event=['completed'],
        status_callback=request.status_callback_url,
        status_callback_event=[
            "queued", "ringing", "in-progress",
            "canceled", "completed", "failed",
            "busy", "no-answer"
        ],
    )

    return {"status": "initiated", "call_sid": call.sid}

@app.post("/twilio/twiml")
async def incoming_call(request: Request):
    # Extract the 'name' from query parameters
    name = request.query_params.get("name", "DefaultName")
    agent_id = request.query_params.get("agent_id", os.getenv("AGENT_ID"))
    print(f"Making an outgoing call to: {name}")
    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Connect>
                <Stream url="wss://deadly-adapted-joey.ngrok-free.app/media-stream-eleven">
                    <Parameter name="name" value="{name}" />
                    <Parameter name="agent_id" value="{agent_id}" />
                </Stream>
            </Connect>
        </Response>"""
    return Response(content=twiml_response, media_type="application/xml")

@app.post("/twilio/recording-call-back")
async def handle_recording_complete(request: Request):
    """
    Endpoint to handle 'action' callbacks for recording completion.
    """
    form_data = await request.form()
    call_sid = form_data.get("CallSid")

    if not call_sid:
        print("CallSid is missing in the form data.")
        raise HTTPException(status_code=400, detail="Missing CallSid")

    try:
        print(f"Processing CallSid: {call_sid}")
        
        # Fetch recordings for the call
        print("Fetching recordings for the call.")
        recordings = twilio_service.list_recordings(call_sid=call_sid)
        print(f"Recordings fetched: {recordings}")

        if not recordings:
            print("No recordings found for the provided CallSid.")
            raise HTTPException(status_code=404, detail="No recordings found for the provided CallSid")
        
        # Filter finalized recordings with duration > 0
        finalized_recordings = [r for r in recordings if r.duration and int(r.duration) > 0]
        print(f"Finalized recordings: {[{'sid': r.sid, 'duration': r.duration} for r in finalized_recordings]}")

        if not finalized_recordings:
            print("No finalized recordings found for the provided CallSid.")
            raise HTTPException(status_code=404, detail="No finalized recordings found for the provided CallSid")
        
        # Find the Longest recording
        longest_recording = max(finalized_recordings, key=lambda r: int(r.duration))
        print(f"Longest recording SID: {longest_recording.sid}, Duration: {longest_recording.duration}")
        
        # Delete other recordings
        for recording in recordings:
            if recording.sid != longest_recording.sid:
                print(f"Deleting recording SID: {recording.sid}")
                twilio_service.delete_recording(recording.sid)
        
        # Check if already processed
        if longest_recording.sid in processed_recordings:
            print(f"Recording SID {longest_recording.sid} already processed. No action taken.")
            return {"message": f"Recording SID {longest_recording.sid} already processed. No action taken."}
        
        # Download the Longest recording
        recording_path = recordings_handler.download_recording(longest_recording.sid)
        print(f"Recording downloaded to: {recording_path}")
        
        if not recording_path:
            print("Failed to download recording.")
            raise HTTPException(status_code=500, detail="Failed to download recording.")
        
        # Fetch call details
        call_details_dict = twilio_service.fetch_call_details(call_sid)
        print(f"Call details fetched: {call_details_dict}")
        
        # Convert times to UTC+5
        call_details_dict["start_time"] = parse_time_to_utc_plus_5(call_details_dict.get("start_time"))
        call_details_dict["end_time"] = parse_time_to_utc_plus_5(call_details_dict.get("end_time"))
        
        # Add recording SID
        call_details_dict["recording_sid"] = longest_recording.sid
          
        # Mark as processed
        processed_recordings.add(longest_recording.sid)
        print(f"Marked recording SID {longest_recording.sid} as processed.")
        
        print(f"Successfully processed CallSid {call_sid}.")
        return {"message": f"Successfully processed CallSid {call_sid}."}
    
    except HTTPException as e:
        print(f"HTTPException occurred: {e.detail}")
        raise e
    except Exception as e:
        print(f"Unexpected error processing CallSid {call_sid}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process recordings: {str(e)}")

@app.post("/twilio/call-status")
async def call_status_callback(request: Request):
    """
    Endpoint to handle Twilio call status callbacks and save details into the database.
    """
    print("Received call status callback.")

    form_data = await request.form()
    call_sid = form_data.get("CallSid")
    call_status = form_data.get("CallStatus")
    from_number = form_data.get("From")
    to_number = form_data.get("To")

    print(f"STATUS: Call SID: {call_sid}, Status: {call_status}, From: {from_number}, To: {to_number}")

    return {"message": "Call status received and saved successfully."}

@app.websocket("/media-stream-eleven")
async def handle_media_stream(websocket: WebSocket):
    await websocket.accept()
    print("WebSocket connection opened")

    audio_interface = TwilioAudioInterface(websocket)

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
                agent_id = audio_interface.customParameters.get("agent_id", os.getenv("AGENT_ID"))
                
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
                    agent_id=agent_id,
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


class LLMOptions(str, Enum):
    """Allowed LLM options"""
    GPT4O_MINI = "gpt-4o-mini"
    GPT4O = "gpt-4o"
    GPT4 = "gpt-4"
    GPT4_TURBO = "gpt-4-turbo"
    GPT35_TURBO = "gpt-3.5-turbo"
    GEMINI15_PRO = "gemini-1.5-pro"
    GEMINI15_FLASH = "gemini-1.5-flash"
    GEMINI10_PRO = "gemini-1.0-pro"
    CLAUDE35_SONNET = "claude-3-5-sonnet"
    CLAUDE3_HAIKU = "claude-3-haiku"
    GROK_BETA = "grok-beta"

class CreateAgentRequest(BaseModel):
    name: str
    first_message: str
    system_prompt: str
    llm: Optional[LLMOptions] = LLMOptions.GPT4O_MINI
    voice_id: Optional[str] = "UgBBYS2sOqTuMpoF3BR0"
    language: Optional[str] = "en"
    # model_id: Optional[str] = "eleven_turbo_v2_5"
    stability: Optional[float] = 0.5
    similarity_boost: Optional[float] = 0.8

class CreateAgentResponse(BaseModel):
    agent_id: str


@app.post("/elevenlabs/create_agent", response_model=CreateAgentResponse)
async def create_agent(request: CreateAgentRequest):
    """
    Endpoint to create an agent in ElevenLabs.
    """
    # client_tool = ClientTools() 
    # client_tool.register()

    agent_config = AgentConfig(
        language=request.language,
        prompt=PromptAgent(
            prompt=request.system_prompt,
            llm=request.llm, 
            temperature=0.5,
            tools=[
                {
                    "type": "system",
                    "description": "Gives agent the ability to end the call with the user.",
                    "name": "end_call",
                }
            ]
        ),
        first_message=request.first_message
    )

    asr_config = AsrConversationalConfig(
        user_input_audio_format="ulaw_8000",
    )

    conversation_config = cc(
        client_events=['user_transcript', 'agent_response', 'interruption', 'audio']
    )

    turn_config = TurnConfig(
        turn_timeout=4,
        mode='silence'
    )

    tts_config = TtsConversationalConfig(
        # model_id=request.model_id,
        voice_id=request.voice_id,
        agent_output_audio_format='ulaw_8000',
        stability=request.stability,
        similarity_boost=request.similarity_boost
    )

    agend_coinfig = ConversationalConfig(
        agent=agent_config,
        asr=asr_config,
        conversation=conversation_config,
        turn=turn_config,
        tts=tts_config
    )

    platform_settings = AgentPlatformSettings(
        overrides=ConversationInitiationClientDataConfig(
            conversation_config_override=ConversationConfigClientOverrideConfig(
                agent=AgentConfigOverrideConfig(
                    prompt=PromptAgentOverrideConfig(
                        prompt=True
                    ),
                    first_message=True,
                    language=True
                )
            )
        )
    )

    agent_id = eleven_labs_client.conversational_ai.create_agent(
                    conversation_config=agend_coinfig,
                    name=request.name,
                    platform_settings=platform_settings
    )

    match = re.search(r"agent_id='([^']+)'", str(agent_id))
    if match:
        agent_id = match.group(1)
        print(f"Extracted agent_id: {agent_id}")
    else: 
        print("No agent_id found")
    
    return CreateAgentResponse(agent_id=agent_id)