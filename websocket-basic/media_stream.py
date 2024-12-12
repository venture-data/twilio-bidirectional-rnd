import base64
import json
import audioop
import asyncio
from fastapi import WebSocket
from assemblyai_client import AssemblyAIClient
from logger import get_logger

# Initialize logger for this module
logger = get_logger("twilio_inbound_stream")

# Constants
CHUNK_DURATION_MS = 20
SILENCE_THRESHOLD = 500
SILENCE_DURATION_MS = 2000
SILENCE_CHUNKS_REQUIRED = SILENCE_DURATION_MS // CHUNK_DURATION_MS

class MediaStream:
    def __init__(self, websocket: WebSocket, aai_client: AssemblyAIClient):
        self.websocket = websocket
        self.aai_client = aai_client
        self.messages = []
        self.has_seen_media = False
        self.connected = True
        self.silence_count = 0
        self.received_audio = False
        self.processing_finished = False
        self.realtime_started = False
        self.final_transcript = ""
        self.audio_started = False  # Track if we've started the streaming

    async def process_message(self, data: dict):
        if not self.connected or self.processing_finished:
            return

        event = data.get("event")
        logger.debug("Received event from Twilio: %s", data)

        if event == "connected":
            logger.info("From Twilio: Connected event: %s", data)

        elif event == "start":
            logger.info("From Twilio: Start event: %s", data)
            self.aai_client.start_realtime_transcription_session()

        elif event == "media":
            await self.handle_media_event(data)

        elif event == "mark":
            logger.info("From Twilio: Mark event: %s", data)

        elif event == "close":
            logger.info("From Twilio: Close event: %s", data)
            await self.close()

    async def handle_media_event(self, data: dict):
        if not self.has_seen_media:
            logger.info("From Twilio: First media event: %s", data)
            self.has_seen_media = True

        # Decode µ-law payload
        ulaw_payload = base64.b64decode(data["media"]["payload"])
        logger.debug("Decoded ulaw payload size: %d bytes", len(ulaw_payload))

        # Convert µ-law to linear PCM (16-bit)
        linear = audioop.ulaw2lin(ulaw_payload, 2)
        rms = audioop.rms(linear, 2)
        logger.debug("Linear PCM RMS: %d", rms)

        # Add the audio chunk (µ-law format) to the client for AssemblyAI
        self.aai_client.add_audio_chunk(ulaw_payload)

        if not self.audio_started:
            self.audio_started = True
            asyncio.create_task(self.start_streaming_audio())
            logger.debug("Started streaming audio to AssemblyAI.")

        # Silence detection
        if rms < SILENCE_THRESHOLD:
            self.silence_count += 1
        else:
            self.silence_count = 0
            self.received_audio = True

        self.messages.append(data)

        if self.received_audio and self.silence_count >= SILENCE_CHUNKS_REQUIRED:
            logger.info("Detected %dms of silence. Stopping and finalizing transcription.", SILENCE_DURATION_MS)
            self.final_transcript = self.aai_client.stop_realtime_transcription()
            logger.info("Final Realtime Transcripts Received:\n%s", self.final_transcript)
            await self.handle_transcription_and_playback()
            await self.wait_for_audio_playback()
            await self.close()

    async def start_streaming_audio(self):
        self.aai_client.start_streaming_audio()

    async def handle_transcription_and_playback(self):
        if not self.messages:
            logger.warning("No audio messages to process for transcription/playback.")
            return

        try:
            payloads = [base64.b64decode(msg["media"]["payload"]) for msg in self.messages]
            linear_payloads = [audioop.ulaw2lin(p, 2) for p in payloads]
            raw_pcm = b"".join(linear_payloads)

            # Now send the original µ-law audio back to Twilio
            combined_payload = base64.b64encode(b"".join(payloads)).decode("utf-8")
            stream_sid = self.messages[0]["streamSid"]
            message = {
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": combined_payload},
            }
            await self.websocket.send_text(json.dumps(message))
            logger.debug("Sent playback audio back to Twilio.")

            mark_message = {
                "event": "mark",
                "streamSid": stream_sid,
                "mark": {"name": "Playback done"},
            }
            await self.websocket.send_text(json.dumps(mark_message))
            logger.debug("Sent 'Playback done' mark event.")

            self.processing_finished = True
            self.raw_pcm = raw_pcm
            logger.info("Playback audio sent back to Twilio. Will wait before closing.")
        except Exception as e:
            logger.error("Error during transcription or playback: %s", e)
            self.processing_finished = True

    async def wait_for_audio_playback(self):
        if hasattr(self, 'raw_pcm'):
            num_samples = len(self.raw_pcm) // 2
            duration_seconds = num_samples / 8000.0
            logger.debug("Waiting for %f seconds for audio playback to finish.", duration_seconds + 0.5)
            await asyncio.sleep(duration_seconds + 0.5)

    async def close(self):
        if self.connected:
            try:
                await self.websocket.close()
            except Exception as e:
                logger.error("Error closing WebSocket: %s", e)
            finally:
                self.connected = False
        logger.info("Server: Connection closed")
