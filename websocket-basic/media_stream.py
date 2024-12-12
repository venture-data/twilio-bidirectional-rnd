import base64
import json
import audioop
import asyncio
from fastapi import WebSocket
from assemblyai_client import AssemblyAIClient

# Constants
CHUNK_DURATION_MS = 20   # Approx. Twilio chunk duration (~20ms)
SILENCE_THRESHOLD = 500  # Amplitude threshold for silence, adjust as needed
SILENCE_DURATION_MS = 2000  # 2 seconds of silence
SILENCE_CHUNKS_REQUIRED = SILENCE_DURATION_MS // CHUNK_DURATION_MS  # e.g., 100 chunks

class MediaStream:
    def __init__(self, websocket: WebSocket, aai_client: AssemblyAIClient):
        self.websocket = websocket
        self.aai_client = aai_client
        self.messages = []
        self.has_seen_media = False
        self.connected = True
        self.silence_count = 0  # Track consecutive silent chunks
        self.received_audio = False  # True once we detect any speech
        self.processing_finished = False  # Once we've processed and played back audio

    async def process_message(self, data: dict):
        if not self.connected or self.processing_finished:
            return

        event = data.get("event")

        if event == "connected":
            print(f"From Twilio: Connected event: {data}")
        elif event == "start":
            print(f"From Twilio: Start event: {data}")
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

        # Decode µ-law payload
        ulaw_payload = base64.b64decode(data["media"]["payload"])
        # Convert µ-law to linear PCM (16-bit)
        linear = audioop.ulaw2lin(ulaw_payload, 2)

        # Detect silence via RMS
        rms = audioop.rms(linear, 2)
        if rms < SILENCE_THRESHOLD:
            self.silence_count += 1
        else:
            self.silence_count = 0
            self.received_audio = True

        # Store the original message for later playback
        self.messages.append(data)

        # If we have received some audio and now have 2s of silence, we consider that the end of speech.
        if self.received_audio and self.silence_count >= SILENCE_CHUNKS_REQUIRED:
            print(f"Detected {SILENCE_DURATION_MS}ms of silence. Stopping and transcribing.")
            await self.handle_transcription_and_playback()

            # After transcription and playback, wait for the audio duration before closing the connection.
            # This ensures Twilio has time to play the audio back to the caller.
            await self.wait_for_audio_playback()

            # Now close the connection so Twilio proceeds to the final <Say>
            await self.close()

    async def handle_transcription_and_playback(self):
        if not self.messages:
            print("No audio messages to process.")
            return

        try:
            # Extract µ-law audio for transcription and playback
            payloads = [base64.b64decode(msg["media"]["payload"]) for msg in self.messages]

            # Convert µ-law to linear PCM for transcription
            linear_payloads = [audioop.ulaw2lin(p, 2) for p in payloads]
            raw_pcm = b"".join(linear_payloads)

            # Transcribe
            transcript_text = self.aai_client.transcribe_audio(raw_pcm)
            print("AssemblyAI transcription:", transcript_text)

            # Now send the original µ-law audio back to Twilio
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

            # Store raw_pcm for duration calculation
            self.raw_pcm = raw_pcm
            print("Playback audio sent back to Twilio. Will wait before closing.")
        except Exception as e:
            print("Error during transcription or playback:", e)
            self.processing_finished = True

    async def wait_for_audio_playback(self):
        # Wait the duration of the audio so Twilio can play it
        # Duration calculation: (len(raw_pcm)/2 samples) at 8000 Hz.
        # seconds = number_of_samples / sample_rate
        # samples = len(raw_pcm)/2
        # sample_rate = 8000
        if hasattr(self, 'raw_pcm'):
            num_samples = len(self.raw_pcm) // 2
            duration_seconds = num_samples / 8000.0
            # Add a small buffer to ensure full playback
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
