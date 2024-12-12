import base64
import json
import audioop
import asyncio
from fastapi import WebSocket
from assemblyai_client import AssemblyAIClient

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

        if event == "connected":
            print(f"From Twilio: Connected event: {data}")

        elif event == "start":
            print(f"From Twilio: Start event: {data}")
            # Start the realtime transcription session at the start of the call
            self.aai_client.start_realtime_transcription_session()
            # We don't start streaming immediately. We'll start once we get the first media chunk.

        elif event == "media":
            await self.handle_media_event(data)

        elif event == "mark":
            print(f"From Twilio: Mark event: {data}")

        elif event == "close":
            print(f"From Twilio: Close event: {data}")
            # If Twilio closes early and we haven't processed, just close.
            await self.close()

    async def handle_media_event(self, data: dict):
        if not self.has_seen_media:
            print(f"From Twilio: First media event: {data}")
            self.has_seen_media = True

        # Decode µ-law payload to linear PCM (16-bit)
        ulaw_payload = base64.b64decode(data["media"]["payload"])
        linear = audioop.ulaw2lin(ulaw_payload, 2)

        # Add the audio chunk to the client (instead of send_audio_chunk)
        self.aai_client.add_audio_chunk(linear)

        # If we haven't started streaming to AssemblyAI yet, do so now
        if not self.audio_started:
            self.audio_started = True
            # Start streaming audio
            # This call will block until we run out of audio chunks or we close the connection
            # Consider running this in the background if blocking is an issue.
            asyncio.create_task(self.start_streaming_audio())

        # Detect silence via RMS
        rms = audioop.rms(linear, 2)
        if rms < SILENCE_THRESHOLD:
            self.silence_count += 1
        else:
            self.silence_count = 0
            self.received_audio = True

        # Store the original message for offline transcription playback
        self.messages.append(data)

        # If we have received some audio and now have 2s of silence, we consider that end of speech.
        if self.received_audio and self.silence_count >= SILENCE_CHUNKS_REQUIRED:
            print(f"Detected {SILENCE_DURATION_MS}ms of silence. Stopping and finalizing transcription.")

            # Stop sending new audio chunks by simply not adding any more.
            # Once the streaming generator finishes, we can stop the realtime transcription:
            self.final_transcript = self.aai_client.stop_realtime_transcription()
            print("Final Realtime Transcripts Received:\n", self.final_transcript)

            # Now handle the transcription and playback
            await self.handle_transcription_and_playback()

            # After transcription and playback, wait for the audio duration before closing the connection.
            await self.wait_for_audio_playback()

            # Now close the connection so Twilio proceeds to the final <Say>
            await self.close()

    async def start_streaming_audio(self):
        # This will start streaming audio chunks until they run out
        self.aai_client.start_streaming_audio()

    async def handle_transcription_and_playback(self):
        if not self.messages:
            print("No audio messages to process.")
            return

        try:
            # Extract µ-law audio for playback
            payloads = [base64.b64decode(msg["media"]["payload"]) for msg in self.messages]
            linear_payloads = [audioop.ulaw2lin(p, 2) for p in payloads]
            raw_pcm = b"".join(linear_payloads)

            # Now send the original µ-law audio back to Twilio to playback
            combined_payload = base64.b64encode(b"".join(payloads)).decode("utf-8")
            stream_sid = self.messages[0]["streamSid"]
            message = {
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": combined_payload},
            }
            await self.websocket.send_text(json.dumps(message))

            # Send a mark event to indicate playback done
            mark_message = {
                "event": "mark",
                "streamSid": stream_sid,
                "mark": {"name": "Playback done"},
            }
            await self.websocket.send_text(json.dumps(mark_message))

            self.processing_finished = True
            self.raw_pcm = raw_pcm
            print("Playback audio sent back to Twilio. Will wait before closing.")
        except Exception as e:
            print("Error during transcription or playback:", e)
            self.processing_finished = True

    async def wait_for_audio_playback(self):
        if hasattr(self, 'raw_pcm'):
            num_samples = len(self.raw_pcm) // 2
            duration_seconds = num_samples / 8000.0
            await asyncio.sleep(duration_seconds + 0.5)

    async def close(self):
        if self.connected:
            try:
                await self.websocket.close()
            except Exception as e:
                print(f"Error closing WebSocket: {e}")
            finally:
                self.connected = False
        print("Server: Connection closed")