from twilio.rest import Client
from typing import List, Optional
import logging

import asyncio
import base64
import json
from fastapi import WebSocket
from elevenlabs.conversational_ai.conversation import AudioInterface
from starlette.websockets import WebSocketDisconnect, WebSocketState

import os
import requests

logger = logging.getLogger(__name__)


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
            logger.info(f"Deleting Twilio recording SID: {recording_sid}")
            self.client.recordings(recording_sid).delete()
            logger.info(f"Twilio recording SID {recording_sid} deleted.")
        except Exception as e:
            logger.error(f"Error deleting recording SID {recording_sid} from Twilio: {e}")
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
            logger.info(f"Fetching call details for CallSid: {call_sid}")
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
            logger.error(f"Error fetching call details for CallSid {call_sid}: {e}")
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
            logger.info(f"Listing recordings for CallSid: {call_sid}")
            recordings = self.client.recordings.list(call_sid=call_sid)
            return recordings
        except Exception as e:
            logger.error(f"Error listing recordings for CallSid {call_sid}: {e}")
            raise e
        
class RecordingsHandler:
    """
    Handles recording operations: downloading from Twilio, uploading to GCS, and cleanup.
    """
    def __init__(self, twilio_service: TwilioService, recordings_dir: str):
        self.twilio_service = twilio_service
        self.recordings_dir = recordings_dir

    def download_recording(self, recording_sid: str) -> Optional[str]:
        """
        Downloads a recording from Twilio and saves it locally. Returns the local file path.

        Args:
            recording_sid (str): The SID of the recording.

        Returns:
            Optional[str]: The path to the downloaded recording if successful, None otherwise.
        """
        try:
            logger.info(f"Downloading recording SID: {recording_sid}")
            recording = self.twilio_service.client.recordings(recording_sid).fetch()
            # Get the recording URL (MP3)
            recording_url = f"https://api.twilio.com{recording.uri.replace('.json', '.mp3')}"
            # Download the recording using stored credentials
            response = requests.get(
                recording_url,
                auth=(self.twilio_service.account_sid, self.twilio_service.auth_token)
            )
            if response.status_code == 200:
                recording_path = os.path.join(self.recordings_dir, f"{recording_sid}.mp3")
                with open(recording_path, "wb") as audio_file:
                    audio_file.write(response.content)
                logger.info(f"Recording {recording_sid} downloaded to {recording_path}")
                return recording_path
            else:
                logger.error(f"Failed to download recording: {response.status_code}, {response.text}")
                return None
        except Exception as e:
            logger.error(f"Error downloading recording {recording_sid}: {e}")
            return None

class TwilioAudioInterface(AudioInterface):
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.input_callback = None
        self.stream_sid = None
        self.call_sid = None
        self.customParameters = None
        self.loop = asyncio.get_event_loop()

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