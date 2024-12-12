from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from typing import Dict
import base64
import json
import os
import audioop
from dotenv import load_dotenv

from assemblyai_client import AssemblyAIClient

load_dotenv()

app = FastAPI()

aai_api = os.getenv('ASSEMBLYAI_API_KEY')
aai_client = AssemblyAIClient(api_key=aai_api)

# Constants
CHUNK_DURATION_MS = 20  # Typical Twilio chunk duration if it's default
SILENCE_THRESHOLD = 500  # Amplitude threshold for silence, may need tuning
SILENCE_DURATION_MS = 2000  # 2 seconds
SILENCE_CHUNKS_REQUIRED = SILENCE_DURATION_MS // CHUNK_DURATION_MS  # 100 chunks if 20ms each

class MediaStream:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.messages = []
        self.has_seen_media = False
        self.connected = True
        self.silence_count = 0  # To track consecutive silent chunks
        self.received_audio = False  # To track if we have received any non-silent audio

    async def process_message(self, data: dict):
        if not self.connected:
            return

        event = data.get("event")

        if event == "connected":
            print(f"From Twilio: Connected event received: {data}")
        elif event == "start":
            print(f"From Twilio: Start event received: {data}")

        elif event == "media":
            if not self.has_seen_media:
                print(f"From Twilio: First media event received: {data}")
                self.has_seen_media = True

            # Decode this chunk of audio from µ-law
            ulaw_payload = base64.b64decode(data["media"]["payload"])
            # Convert µ-law to linear PCM (16-bit)
            linear = audioop.ulaw2lin(ulaw_payload, 2)

            # Measure RMS to detect silence
            rms = audioop.rms(linear, 2)  # Calculate RMS of 16-bit samples
            # Check if silent
            if rms < SILENCE_THRESHOLD:
                self.silence_count += 1
            else:
                self.silence_count = 0
                self.received_audio = True

            # Always store the original message so we can playback later
            self.messages.append(data)

            # If we have received some audio and now detected 2 seconds of silence, 
            # it's time to stop and transcribe.
            if self.received_audio and self.silence_count >= SILENCE_CHUNKS_REQUIRED:
                print(f"Detected {SILENCE_DURATION_MS}ms of silence. Stopping recording and transcribing.")
                await self.handle_transcription_and_playback()
                await self.close()

        elif event == "mark":
            print(f"From Twilio: Mark event received: {data}")

        elif event == "close":
            # If Twilio explicitly closes, just handle whatever we have so far
            print(f"From Twilio: Close event received: {data}")
            if self.received_audio and self.messages:
                await self.handle_transcription_and_playback()
            await self.close()

    async def handle_transcription_and_playback(self):
        if not self.messages:
            print("No audio messages to process.")
            return

        try:
            # Extract µ-law audio from messages
            payloads = [base64.b64decode(msg["media"]["payload"]) for msg in self.messages]

            # Convert µ-law to linear PCM for transcription
            linear_payloads = [audioop.ulaw2lin(p, 2) for p in payloads]
            raw_pcm = b"".join(linear_payloads)

            # Transcribe the raw PCM audio
            transcript_text = aai_client.transcribe_audio(raw_pcm)
            print("AssemblyAI transcription:", transcript_text)

            # Now send the original µ-law audio back as a single combined media message
            combined_payload = base64.b64encode(b"".join(payloads)).decode("utf-8")

            stream_sid = self.messages[0]["streamSid"]
            message = {
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": combined_payload},
            }
            await self.websocket.send_text(json.dumps(message))

            # Optionally, send a mark event indicating playback done
            mark_message = {
                "event": "mark",
                "streamSid": stream_sid,
                "mark": {"name": "Playback done"},
            }
            await self.websocket.send_text(json.dumps(mark_message))

        except Exception as e:
            print("Error during transcription or playback:", e)

    async def close(self):
        if self.connected:
            try:
                await self.websocket.close()
            except Exception as e:
                print(f"Error while closing WebSocket: {e}")
            finally:
                self.connected = False
        print("Server: Connection closed")


media_streams: Dict[int, MediaStream] = {}

@app.post("/twiml")
async def serve_twiml():
    xml_path = "websocket-basic/templates/streams.xml"
    return FileResponse(xml_path, media_type="application/xml")

@app.websocket("/streams")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    sid = id(websocket)
    media_stream = MediaStream(websocket)
    media_streams[sid] = media_stream
    print(f"From Twilio: Connection accepted: {sid}")

    try:
        while True:
            data = await websocket.receive_text()
            data = json.loads(data)
            await media_stream.process_message(data)
    except WebSocketDisconnect:
        print(f"From Twilio: Connection disconnected: {sid}")
        if sid in media_streams:
            if media_streams[sid].connected:
                await media_streams[sid].close()
            del media_streams[sid]
    except Exception as e:
        print(f"Error in WebSocket connection: {e}")
        if sid in media_streams:
            await media_streams[sid].close()
            del media_streams[sid]
