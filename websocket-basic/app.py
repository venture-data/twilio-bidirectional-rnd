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

class MediaStream:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.messages = []
        self.has_seen_media = False
        self.connected = True

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
            # Accumulate the audio messages
            self.messages.append(data)
        elif event == "mark":
            print(f"From Twilio: Mark event received: {data}")
        elif event == "close":
            print(f"From Twilio: Close event received: {data}")
            # Once we receive close, it means Twilio has ended sending audio due to speech timeout.
            # Now process the entire collected audio for transcription.
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

            # Now send the original µ-law audio back to Twilio as a single combined media message.
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


# Dictionary to store active WebSocket connections
media_streams: Dict[int, MediaStream] = {}

@app.post("/twiml")
async def serve_twiml():
    """Serve the TwiML XML."""
    # Update this path accordingly
    xml_path = "websocket-basic/templates/streams.xml"
    return FileResponse(xml_path, media_type="application/xml")

@app.websocket("/streams")
async def websocket_endpoint(websocket: WebSocket):
    """Handle WebSocket connections."""
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
