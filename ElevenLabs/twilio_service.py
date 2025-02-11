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
        self.input_callback = None
        self.stream_sid = None
        self.call_sid = None
        self.customParameters = None
        self.loop = asyncio.get_event_loop()

        self.background_noise = None
        self.background_pos = 0
        self.background_task = None
        self.running = False

        self.ai_audio_queue = asyncio.Queue()
        self.chunk_size = 160  # 20ms of audio at 8KHz
        self.background_volume = 0.5

    def load_background_noise(self, file_path: Optional[str]):
        """Load background noise from a file. Pass None to disable (use silence)."""
        self.background_noise = None
        if file_path is None:
            print("Background noise disabled (will send silence).")
            return
        try:
            with open(file_path, 'rb') as f:
                self.background_noise = f.read()
            print(f"Loaded background noise: {len(self.background_noise)} bytes")
        except Exception as e:
            print(f"Error loading background noise: {str(e)}")

    async def start_background_stream(self):
        """Always start the background (or silence) streaming loop."""
        self.running = True
        self.background_task = asyncio.create_task(self._stream_background())

    async def stop_background_stream(self):
        """Stop background streaming."""
        self.running = False
        if self.background_task:
            await self.background_task

    async def _stream_background(self):
        """Continuous streaming loop: either background noise or silence, plus AI mixing."""
        while self.running and self.stream_sid:
            try:
                # 1) Get background chunk or silence
                if self.background_noise:
                    bg_chunk = self._get_background_chunk(self.chunk_size)
                    # Optionally adjust background volume
                    bg_chunk = self._adjust_volume(bg_chunk, self.background_volume)
                else:
                    # Generate 160 bytes of mu-law silence (0xFF is silence in G.711 u-law)
                    bg_chunk = b'\xFF' * self.chunk_size

                # 2) Check for AI audio to mix
                ai_chunk = await self._get_ai_chunk()

                # 3) Mix or just use background if no AI chunk
                if ai_chunk:
                    mixed_audio = self.mix_chunks(bg_chunk, ai_chunk)
                else:
                    mixed_audio = bg_chunk

                # 4) Send the final chunk to Twilio
                await self.send_audio_to_twilio(mixed_audio)

                # Sleep ~20ms to maintain real-time pacing
                await asyncio.sleep(0.02)
            except Exception as e:
                print(f"Error in background stream: {str(e)}")
                break

    def _get_background_chunk(self, length: int) -> bytes:
        """Get next background chunk (loop if needed)."""
        chunk = bytearray()
        remaining = length
        while remaining > 0:
            end = self.background_pos + remaining
            if end <= len(self.background_noise):
                chunk.extend(self.background_noise[self.background_pos:end])
                self.background_pos = end
                remaining = 0
            else:
                chunk.extend(self.background_noise[self.background_pos:])
                remaining -= len(self.background_noise) - self.background_pos
                self.background_pos = 0
        return bytes(chunk)

    async def _get_ai_chunk(self) -> Optional[bytes]:
        """Get AI audio chunk if available, or None if not."""
        try:
            return self.ai_audio_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def mix_chunks(self, bg_chunk: bytes, ai_chunk: bytes) -> bytes:
        """Mix background and AI audio chunks using audioop."""
        try:
            bg_pcm = audioop.ulaw2lin(bg_chunk, 2)
            ai_pcm = audioop.ulaw2lin(ai_chunk, 2)
            mixed_pcm = audioop.add(bg_pcm, ai_pcm, 2)
            return audioop.lin2ulaw(mixed_pcm, 2)
        except Exception as e:
            print(f"Error mixing audio chunks: {str(e)}")
            return bg_chunk  # Fallback to background/silence only

    def _adjust_volume(self, audio_chunk: bytes, volume: float) -> bytes:
        """Adjust the volume of an audio chunk."""
        if volume == 1.0:
            return audio_chunk
        try:
            pcm = audioop.ulaw2lin(audio_chunk, 2)
            adjusted_pcm = audioop.mul(pcm, 2, volume)
            return audioop.lin2ulaw(adjusted_pcm, 2)
        except Exception as e:
            print(f"Error adjusting volume: {str(e)}")
            return audio_chunk

    def output(self, audio: bytes):
        """Receive AI audio from ElevenLabs and queue it for mixing."""
        # If .start_background_stream() never ran, self.running is False
        if not self.running:
            return
        # Break AI audio into smaller chunks so it fits our 20ms cycle
        for i in range(0, len(audio), self.chunk_size):
            chunk = audio[i : i + self.chunk_size]
            self.loop.call_soon_threadsafe(self.ai_audio_queue.put_nowait, chunk)

    def start(self, input_callback):
        self.input_callback = input_callback

    def stop(self):
        self.input_callback = None
        self.stream_sid = None
        self.call_sid = None

    def interrupt(self):
        # Clears the audio buffer in Twilio
        asyncio.run_coroutine_threadsafe(self.send_clear_message_to_twilio(), self.loop)

    async def send_audio_to_twilio(self, audio: bytes):
        """Actually send the final (mixed) mu-law audio chunk to Twilio."""
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
        """Sends a 'clear' event to Twilio to flush buffers if needed."""
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
            self.stream_sid = data["start"]["streamSid"]
            self.customParameters = data["start"]["customParameters"]
            self.call_sid = data["start"]["callSid"]
            # Always start background stream, even if it's just silence
            await self.start_background_stream()

        elif event_type == "media" and self.input_callback:
            audio_data = base64.b64decode(data["media"]["payload"])
            self.input_callback(audio_data)