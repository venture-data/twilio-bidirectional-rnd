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

import websockets
import asyncio
from starlette.websockets import WebSocketDisconnect

from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client

from elevenlabs.conversational_ai.conversation import Conversation, ConversationConfig

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
    PromptAgentOverrideConfig
)

from twilio_service import TwilioAudioInterface, TwilioService, RecordingsHandler
from utils import parse_time_to_utc_plus_5

from dotenv import load_dotenv

import httpx

load_dotenv()

# Jinja2 templates
templates = Jinja2Templates(directory="templates")

TWILIO_ACCOUNT_SID = os.getenv("ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("AUTH_TOKEN")
AGENT_ID = os.getenv("AGENT_ID")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

SYSTEM_MESSAGE = (
    "You are a support agent (talk in Urdu) named Haider representing a data and AI services company. "
    "Your goal is to land clients by promoting services like chatbots, fraud detection, customer segmentation, and sales forecasting. "
    "Be friendly/enthusiastic, use filler words (hmm, ah, etc.), and keep responses to 3-4 sentences. "
    "Never repeat the user's own words back to them. "
    "Use the knowledge base to provide more information about the company when asked Remember to talk in Urdu. "
    "The user will probably speak in Hindi but always respond in Urdu. "
)
VOICE = "alloy" # verse, alloy, 
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

app = FastAPI()

eleven_labs_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

# Initialize Twilio client
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
twilio_service = TwilioService(
    account_sid=TWILIO_ACCOUNT_SID,
    auth_token=TWILIO_AUTH_TOKEN
)

recordings_handler = RecordingsHandler(
    twilio_service=twilio_service
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
    connect.stream(url=f"wss://{request.url.hostname}/elevenlabs/media-stream")
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

class OutBoundRequest(BaseModel):
    to: str
    name: Optional[str] = "Ammar"
    language: Optional[str] = "english"
    agent_id: Optional[str] = os.getenv("AGENT_ID")
    from_: Optional[str] = "+17753177891" # +15512967933 +12185857512 +17753177891
    twilio_call_url: Optional[str] = "https://bidirectional-me-547752509861.me-central1.run.app/twilio/twiml"
    recording_callback_url: Optional[str] = "https://bidirectional-me-547752509861.me-central1.run.app/twilio/recording-call-back"
    status_callback_url: Optional[str] = "https://bidirectional-me-547752509861.me-central1.run.app/twilio/call-status"

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
        
    if request.language == 'urdu':
        twiml_url = f"{twiml_url}&{urlencode({'agent_provider': 'openai'})}"

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
    name = request.query_params.get("name", "DefaultName")
    agent_id = request.query_params.get("agent_id", os.getenv("AGENT_ID"))
    agent_provider = request.query_params.get("agent_provider", os.getenv("agent_provider"))
    print(f"Making an outgoing call to: {name}")
    if agent_provider == 'openai':
        twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
            <Response>
                <Connect>
                    <Stream url="wss://bidirectional-me-547752509861.me-central1.run.app/openai/media-stream">
                        <Parameter name="name" value="{name}" />
                        <Parameter name="agent_id" value="{agent_id}" />
                    </Stream>
                </Connect>
            </Response>"""
    else:
        twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
            <Response>
                <Connect>
                    <Stream url="wss://bidirectional-me-547752509861.me-central1.run.app/elevenlabs/media-stream">
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

@app.websocket("/openai/media-stream")
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
                                "You're a bot who understands and talks in Urdu mainly. "
                                "You're on a call with Ammar. "
                                "You are a support agent named Haider. "
                                "You represent a data and AI services company and are tasked to land clients to use your services.  "
                                "You are very friendly and enthusiastic and really want to help the customer. "
                                "Your main task is to land clients, tell that your company deals in AI and data services such as chatbots, fraud detections, customer segmentation, sales forecasting etc, if asked then tell the services in detail and how the client company can benefit from it. "
                                "try to Answer in about 1- 2 sentences. Keep answers concise and like a natural conversations. Do add some filler wirds like: hmm, umm, let me check, let me think, ah, etc. "
                                "Remember to keep answers to the point and don't repeat back the users response.    "
                                "Hi Ammar, I'm Haider from Venture Data. We're a Data and AI company. How are you today?"
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

@app.websocket("/elevenlabs/media-stream")
async def handle_media_stream(websocket: WebSocket):
    await websocket.accept()
    print("WebSocket connection opened")

    audio_interface = TwilioAudioInterface(websocket)
    audio_interface.load_background_noise("call-center-youtube-01.ulaw")

    local_call_sid = None
    conversation = None
    conversation_logs = []

    try:
        async for message in websocket.iter_text():
            if not message:
                continue
            
            data = json.loads(message)
            event_type = data.get("event")
            
            await audio_interface.handle_twilio_message(data)
            
            if event_type == "start":
                local_call_sid = data["start"]["callSid"]
                await audio_interface.start_background_stream()  # Start streaming
                
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
                                    "prompt": "You are a support agent named Haider. "
                                        "You represent a data and AI services company and are tasked to land clients to use your services.  "
                                        "You are very friendly and enthusiastic and really want to help the customer. "
                                        "Your main task is to land clients, tell that your company deals in AI and data services such as chatbots, fraud detections, customer segmentation, sales forecasting etc, if asked then tell the services in detail and how the client company can benefit from it. "
                                        "try to Answer in about 1- 2 sentences. Keep answers concise and like a natural conversations. Do add some filler wirds like: hmm, umm, let me check, let me think, ah, etc. "
                                        "Remember to keep answerst to the point and don't repeat back the users response."
                                },
                                "first_message": f"Hi {name}, I'm Haider from Venture Data. We're a Data and AI company. How are you today?",
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
            await audio_interface.stop_background_stream()
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
    Endpoint to create an agent in ElevenLabs and upload knowledge base document.
    """
    # First create agent without knowledge base
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
            ],
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
    if not match:
        raise HTTPException(status_code=500, detail="Failed to extract agent_id")
    
    agent_id = match.group(1)
    print(f"Created agent with ID: {agent_id}")

    try:
        files = {
            'file': ('About Us Content.docx', open('About Us Content.docx', 'rb'), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        }
        headers = {
            'xi-api-key': ELEVENLABS_API_KEY
        }
        
        upload_url = f"https://api.elevenlabs.io/v1/convai/agents/{agent_id}/add-to-knowledge-base"
        async with httpx.AsyncClient() as client:
            response = await client.post(upload_url, headers=headers, files=files)
            response.raise_for_status()
            kb_data = response.json()
            kb_id = kb_data.get('id')
            print(f"Successfully uploaded knowledge base document. KB ID: {kb_id}")

            patch_url = f"https://api.elevenlabs.io/v1/convai/agents/{agent_id}"
            patch_data = {
                "conversation_config": {
                    "agent": {
                        "prompt": {
                            "knowledge_base": [
                                {
                                    "type": "file",
                                    "name": "About Us Content.docx",
                                    "id": kb_id
                                }
                            ]
                        }
                    }
                }
            }
            patch_response = await client.patch(patch_url, headers=headers, json=patch_data)
            patch_response.raise_for_status()
            print(f"Successfully updated agent with knowledge base")

    except Exception as e:
        print(f"Error handling knowledge base: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to handle knowledge base: {str(e)}")

    return CreateAgentResponse(agent_id=agent_id)