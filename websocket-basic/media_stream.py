import base64
import json
import audioop
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

    async def process_message(self, data: dict):
        if not self.connected:
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
            # When Twilio sends a close event, we transcribe and playback if we haven't done so.
            await self.handle_close_event(data)

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
            # Do NOT close immediately. Let Twilio handle the call flow.
            # Twilio might continue to stream audio or eventually send a 'close' event.

    async def handle_close_event(self, data: dict):
        print(f"From Twilio: Close event: {data}")
        # If we haven't already processed the audio, do so now.
        if self.received_audio and self.messages:
            await self.handle_transcription_and_playback()
        # Now close the connection after Twilio indicated it's done.
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

            print("Playback audio sent back to Twilio. Awaiting Twilio's next action or close event.")

        except Exception as e:
            print("Error during transcription or playback:", e)

    async def close(self):
        if self.connected:
            try:
                await self.websocket.close()
            except Exception as e:
                print(f"Error closing WebSocket: {e}")
            finally:
                self.connected = False
        print("Server: Connection closed")
