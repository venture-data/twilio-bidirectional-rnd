from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from typing import Dict
import base64
import json
import os
import audioop
import time
from dotenv import load_dotenv

from assemblyai_client import AssemblyAIClient

load_dotenv()
app = FastAPI()

ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")

# Initialize the AssemblyAI client
aai_client = AssemblyAIClient(api_key=ASSEMBLYAI_API_KEY)

class MediaStream:
    def __init__(self, websocket: WebSocket, aai_client: AssemblyAIClient):
        self.websocket = websocket
        self.aai_client = aai_client
        self.messages = []
        self.has_seen_media = False
        self.connected = True  
        self.start_time = None
        self.recording_finished = False

    async def process_message(self, data: dict):
        if not self.connected:
            return

        event_type = data.get("event")
        
        if event_type == "connected":
            print(f"From Twilio: Connected event received: {data}")

        elif event_type == "start":
            print(f"From Twilio: Start event received: {data}")
            # Record the start time when we start receiving audio
            self.start_time = time.monotonic()

        elif event_type == "media":
            if not self.has_seen_media:
                print(f"From Twilio: First Media event received: {data}")
                self.has_seen_media = True

            # If we haven't finished recording, store the media
            if not self.recording_finished:
                self.messages.append(data)
                elapsed_time = time.monotonic() - self.start_time
                # Check if 5 seconds have passed
                if elapsed_time >= 5.0:
                    # We reached our recording limit
                    self.recording_finished = True
                    await self.process_and_transcribe()

        elif event_type == "mark":
            # We are no longer using marks in this scenario
            print(f"From Twilio: Mark event received: {data}")

        elif event_type == "close":
            print(f"From Twilio: Close event received: {data}")
            await self.close()

    async def process_and_transcribe(self):
        if not self.connected:
            return

        messages = self.messages[:]
        self.messages = []
        stream_sid = messages[0]["streamSid"]

        # Decode from base64 μ-law
        payloads = [base64.b64decode(msg["media"]["payload"]) for msg in messages]

        # Convert μ-law to linear PCM
        linear_payloads = [audioop.ulaw2lin(p, 2) for p in payloads]
        raw_pcm = b"".join(linear_payloads)

        # Transcribe using AssemblyAI
        try:
            transcript_text = self.aai_client.transcribe_audio(raw_pcm)
            print("AssemblyAI transcription:", transcript_text)
        except Exception as e:
            print("Error during transcription:", e)

        # Convert linear PCM back to μ-law before sending
        mu_law_data = audioop.lin2ulaw(raw_pcm, 2)
        combined_payload = base64.b64encode(mu_law_data).decode("utf-8")

        try:
            # Send the media event back to Twilio
            media_message = {
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": combined_payload},
            }
            await self.websocket.send_text(json.dumps(media_message))

            # Send a mark event, just like your working code did before
            mark_message = {
                "event": "mark",
                "streamSid": stream_sid,
                "mark": {"name": "Transcribed playback"}
            }
            await self.websocket.send_text(json.dumps(mark_message))

            print("Playback done. Closing the connection.")
            await self.close()
        except Exception as e:
            print(f"Error while sending message: {e}")
            self.connected = False



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
media_streams: Dict[str, MediaStream] = {}

@app.post("/twiml")
async def serve_twiml():
    """Serve the TwiML XML."""
    # Update this path as needed
    xml_path = "/Users/ammarahmad/Documents/Venture Data/Call Agent/twilio-twoway-example/websocket-basic/templates/streams.xml"
    return FileResponse(xml_path, media_type="application/xml")

@app.websocket("/streams")
async def websocket_endpoint(websocket: WebSocket):
    """Handle WebSocket connections."""
    # Specify the TLCP subprotocol to match Twilio's expectations
    print("About to accept WebSocket with TLCP subprotocol...")
    await websocket.accept(subprotocol='TLCP')
    print("WebSocket accepted.")

    sid = id(websocket)
    media_stream = MediaStream(websocket, aai_client)
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
        if sid in media_streams and media_streams[sid].connected:
            await media_streams[sid].close()
        if sid in media_streams:
            del media_streams[sid]
