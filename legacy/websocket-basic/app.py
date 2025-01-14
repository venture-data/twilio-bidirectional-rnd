from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from typing import Dict
import json
import os
from dotenv import load_dotenv

from assemblyai_client import AssemblyAIClient
from media_stream import MediaStream

load_dotenv()

app = FastAPI()

aai_api = os.getenv('ASSEMBLYAI_API_KEY')
aai_client = AssemblyAIClient(api_key=aai_api)

media_streams: Dict[int, MediaStream] = {}

@app.post("/twiml")
async def serve_twiml():
    xml_path = "websocket-basic/templates/streams.xml"
    return FileResponse(xml_path, media_type="application/xml")

@app.websocket("/streams")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
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
        if sid in media_streams:
            await media_streams[sid].close()
            del media_streams[sid]
