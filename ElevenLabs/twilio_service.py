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

class TwilioAudioInterface(AudioInterface):
    def __init__(self, websocket: WebSocket):
        super().__init__()
        self.websocket = websocket
        self.input_callback = None
        self.stream_sid = None
        self.call_sid = None
        self.customParameters = None
        self.loop = asyncio.get_event_loop()
        self.background_noise = None
        self.background_pos = 0

    def load_background_noise(self, file_path: str):
        """Load background noise from a file."""
        with open(file_path, 'rb') as f:
            self.background_noise = f.read()
        print(f"Loaded background noise: {len(self.background_noise)} bytes")

    def mix_with_background(self, audio_bytes: bytes) -> bytes:
        """Mix AI audio with background noise."""
        if not self.background_noise:
            return audio_bytes
        
        bg_ulaw = self._get_background_ulaw(len(audio_bytes))
        if not bg_ulaw:
            return audio_bytes
        
        # Convert to PCM and mix
        ai_pcm = audioop.ulaw2lin(audio_bytes, 2)
        bg_pcm = audioop.ulaw2lin(bg_ulaw, 2)
        mixed_pcm = audioop.add(ai_pcm, bg_pcm, 2)
        mixed_ulaw = audioop.lin2ulaw(mixed_pcm, 2)
        return mixed_ulaw

    def _get_background_ulaw(self, length: int) -> bytes:
        """Retrieve background bytes, looping as needed."""
        bg_ulaw = bytearray()
        remaining = length
        while remaining > 0:
            end_pos = self.background_pos + remaining
            if end_pos <= len(self.background_noise):
                bg_ulaw.extend(self.background_noise[self.background_pos:end_pos])
                self.background_pos = end_pos
                remaining = 0
            else:
                bg_ulaw.extend(self.background_noise[self.background_pos:])
                remaining -= (len(self.background_noise) - self.background_pos)
                self.background_pos = 0
        return bytes(bg_ulaw)

    def output(self, audio: bytes):
        """Send mixed audio to Twilio."""
        mixed_audio = self.mix_with_background(audio)
        asyncio.run_coroutine_threadsafe(self.send_audio_to_twilio(mixed_audio), self.loop)

    def start(self, input_callback):
        self.input_callback = input_callback

    def stop(self):
        self.input_callback = None
        self.stream_sid = None
        self.call_sid = None

    def output(self, audio: bytes):
        asyncio.run_coroutine_threadsafe(self.send_audio_to_twilio(audio), self.loop)

    def interrupt(self):
        asyncio.run_coroutine_threadsafe(self.send_clear_message_to_twilio(), self.loop)

    async def send_audio_to_twilio(self, audio: bytes):
        if self.stream_sid:
            audio_payload = base64.b64encode(audio).decode("utf-8")
            audio_delta = {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": audio_payload},
            }
            try:
                if self.websocket.application_state == WebSocketState.CONNECTED:
                    await self.websocket.send_text(json.dumps(audio_delta))
            except (WebSocketDisconnect, RuntimeError):
                pass

    async def send_clear_message_to_twilio(self):
        if self.stream_sid:
            clear_message = {"event": "clear", "streamSid": self.stream_sid}
            try:
                if self.websocket.application_state == WebSocketState.CONNECTED:
                    await self.websocket.send_text(json.dumps(clear_message))
            except (WebSocketDisconnect, RuntimeError):
                pass

    async def handle_twilio_message(self, data):
        event_type = data.get("event")

        if event_type == "start":
            print(f"Full Data: {data["start"]}")

            self.stream_sid = data["start"]["streamSid"]
            print(f"streamsid: {data["start"]["streamSid"]}")

            self.customParameters = data["start"]["customParameters"]

            self.call_sid = data["start"]["callSid"]
            print(f"callsid: {data["start"]["callSid"]}")

        elif event_type == "media" and self.input_callback:
            audio_data = base64.b64decode(data["media"]["payload"])
            self.input_callback(audio_data)