from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from typing import Dict
import base64
import json
import os

app = FastAPI()

REPEAT_THRESHOLD = 50

# MediaStream class to handle WebSocket connections
class MediaStream:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.messages = []
        self.has_seen_media = False
        self.repeat_count = 0
        self.connected = True  # Flag to track WebSocket state

    async def process_message(self, data: dict):
        if not self.connected:
            return

        if data["event"] == "connected":
            print(f"From Twilio: Connected event received: {data}")
        elif data["event"] == "start":
            print(f"From Twilio: Start event received: {data}")
        elif data["event"] == "media":
            if not self.has_seen_media:
                print(f"From Twilio: Media event received: {data}")
                self.has_seen_media = True

            # Store media messages
            self.messages.append(data)
            if len(self.messages) >= REPEAT_THRESHOLD:
                print(f"From Twilio: {len(self.messages)} omitted media messages")
                await self.repeat()
        elif data["event"] == "mark":
            print(f"From Twilio: Mark event received: {data}")
        elif data["event"] == "close":
            print(f"From Twilio: Close event received: {data}")
            await self.close()

    async def repeat(self):
        if not self.connected:
            return

        messages = self.messages[:]
        self.messages = []
        stream_sid = messages[0]["streamSid"]

        # Combine all the media payloads
        payloads = [base64.b64decode(msg["media"]["payload"]) for msg in messages]
        combined_payload = base64.b64encode(b"".join(payloads)).decode("utf-8")

        try:
            message = {
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": combined_payload},
            }
            await self.websocket.send_text(json.dumps(message))

            mark_message = {
                "event": "mark",
                "streamSid": stream_sid,
                "mark": {"name": f"Repeat message {self.repeat_count}"},
            }
            await self.websocket.send_text(json.dumps(mark_message))
            self.repeat_count += 1

            if self.repeat_count == 5:
                print(f"Server: Repeated {self.repeat_count} times...closing")
                await self.websocket.close(code=1000, reason="Repeated 5 times")
                self.connected = False

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
        print(f"Server: Connection closed")


# Dictionary to store active WebSocket connections
media_streams: Dict[str, MediaStream] = {}


@app.post("/twiml")
async def serve_twiml():
    """Serve the TwiML XML."""
    xml_path = "websocket-basic/templates/streams.xml"  # Update this path
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
            await media_streams[sid].close()
            del media_streams[sid]
    except Exception as e:
        print(f"Error in WebSocket connection: {e}")
        if sid in media_streams:
            await media_streams[sid].close()
            del media_streams[sid]
