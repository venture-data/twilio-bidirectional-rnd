from twilio.rest import Client
from typing import List, Optional

import asyncio
import base64
import json
from fastapi import WebSocket
from elevenlabs.conversational_ai.conversation import AudioInterface
from starlette.websockets import WebSocketDisconnect, WebSocketState

import audioop
import requests

import tempfile
from aws_handler import AWSHandler

class TwilioService:
    """
    Service class to interact with Twilio API.
    """
    def __init__(self, account_sid: str, auth_token: str):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.client = Client(username=account_sid, password=auth_token)
        

    def delete_recording(self, recording_sid: str):
        """
        Deletes a recording from Twilio.

        Args:
            recording_sid (str): The SID of the recording to delete.
        """
        try:
            print(f"Deleting Twilio recording SID: {recording_sid}")
            self.client.recordings(recording_sid).delete()
            print(f"Twilio recording SID {recording_sid} deleted.")
        except Exception as e:
            print(f"Error deleting recording SID {recording_sid} from Twilio: {e}")
            raise e

    def fetch_call_details(self, call_sid: str) -> dict:
        """
        Fetches call details from Twilio.

        Args:
            call_sid (str): The SID of the call.

        Returns:
            dict: A dictionary containing call details.
        """
        try:
            print(f"Fetching call details for CallSid: {call_sid}")
            call_details = self.client.calls(call_sid).fetch()
            # Extract call details
            call_start_time = call_details.start_time.isoformat()
            call_end_time = call_details.end_time.isoformat() if call_details.end_time else None
            call_duration = float(call_details.duration) / 60 if call_details.duration else 0
            call_to = call_details.to
            return {
                "call_sid": call_sid,
                "start_time": call_start_time,
                "end_time": call_end_time,
                "duration": call_duration,
                "to": call_to
            }
        except Exception as e:
            print(f"Error fetching call details for CallSid {call_sid}: {e}")
            raise e

    def list_recordings(self, call_sid: str) -> List:
        """
        Lists all recordings for a given call SID.

        Args:
            call_sid (str): The SID of the call.

        Returns:
            List: A list of recording objects.
        """
        try:
            print(f"Listing recordings for CallSid: {call_sid}")
            recordings = self.client.recordings.list(call_sid=call_sid)
            return recordings
        except Exception as e:
            print(f"Error listing recordings for CallSid {call_sid}: {e}")
            raise e
        
class RecordingsHandler:
    """
    Handles recording operations: downloading from Twilio, uploading to GCS, and cleanup.
    """
    def __init__(self, twilio_service: TwilioService):
        self.twilio_service = twilio_service
        self.aws_handler = AWSHandler()

    def download_recording(self, recording_sid: str) -> Optional[str]:
        """
        Downloads a recording from Twilio and saves it locally. Returns the local file path.

        Args:
            recording_sid (str): The SID of the recording.

        Returns:
            Optional[str]: The path to the downloaded recording if successful, None otherwise.
        """
        temp_path = None
        try:
            print(f"Processing recording SID: {recording_sid}")
            recording = self.twilio_service.client.recordings(recording_sid).fetch()
            call_sid = recording.call_sid
            
            # Get recording URL and download
            recording_url = f"https://api.twilio.com{recording.uri.replace('.json', '.mp3')}"
            response = requests.get(
                recording_url,
                auth=(self.twilio_service.account_sid, self.twilio_service.auth_token),
                timeout=10
            )
            response.raise_for_status()

            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as temp_file:
                temp_file.write(response.content)
                temp_path = temp_file.name

            print(f"Temporarily stored recording at {temp_path}")

            s3_url = self.aws_handler.upload_recording(
                local_path=temp_path, 
                call_sid=call_sid,
                recording_sid=recording_sid
            )

            # Only delete from Twilio after successful upload
            self.twilio_service.delete_recording(recording_sid)
            print(f"Successfully processed recording {recording_sid}")
            return s3_url
        except Exception as e:
            print(f"Error downloading recording {recording_sid}: {e}")
            return None

# In twilio_service.py

class TwilioAudioInterface(AudioInterface):
    def __init__(self, websocket: WebSocket):
        super().__init__()
        self.websocket = websocket
        self.call_sid = None
        self.stream_sid = None
        self.customParameters = None

        self.loop = asyncio.get_event_loop()
        self.input_callback = None
        self.running = False

    def start(self, input_callback):
        """
        Called by the ElevenLabs Conversation to handle inbound voice from Twilio.
        """
        self.input_callback = input_callback

    def stop(self):
        self.input_callback = None
        self.call_sid = None
        self.stream_sid = None
        self.running = False

    def interrupt(self):
        """
        (Optional) Called when the ElevenLabs agent wants to interrupt user speech.
        """
        asyncio.run_coroutine_threadsafe(self._send_clear_message(), self.loop)

    def output(self, audio: bytes):
        """
        Called by ElevenLabs whenever TTS audio is generated.
        We immediately forward that audio to Twilio over our WebSocket.
        """
        # Stream it straight away (no queue) in a background task:
        asyncio.run_coroutine_threadsafe(
            self._send_audio_chunk(audio),
            self.loop
        )

    async def _send_audio_chunk(self, audio: bytes):
        """
        Sends a single chunk of u-law audio to Twilio over the WS connection.
        """
        # If you want to do 20ms framing, you can break `audio` up first.
        # For a minimal approach, send it as a single chunk:
        if not self.stream_sid:
            return  # Not connected yet

        try:
            payload_b64 = base64.b64encode(audio).decode("utf-8")
            media_message = {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": payload_b64},
            }
            if self.websocket.application_state == WebSocketState.CONNECTED:
                await self.websocket.send_text(json.dumps(media_message))
        except (WebSocketDisconnect, RuntimeError):
            pass

    async def _send_clear_message(self):
        if not self.stream_sid:
            return
        clear_message = {"event": "clear", "streamSid": self.stream_sid}
        if self.websocket.application_state == WebSocketState.CONNECTED:
            await self.websocket.send_text(json.dumps(clear_message))

    async def handle_twilio_message(self, message: dict):
        """
        Called for every inbound JSON from Twilio.
        """
        event_type = message.get("event")

        if event_type == "start":
            # Twilio is telling us they started media streaming.
            self.stream_sid = message["start"]["streamSid"]
            self.call_sid = message["start"]["callSid"]
            self.customParameters = message["start"]["customParameters"]
            self.running = True

        elif event_type == "media" and self.input_callback and self.running:
            # We got inbound audio from Twilio, pass it to the ElevenLabs ASR.
            audio_data = base64.b64decode(message["media"]["payload"])
            self.input_callback(audio_data)
        elif event_type == "stop":
            # Twilio says media streaming ended
            self.running = False