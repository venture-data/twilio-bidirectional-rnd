
import os
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from twilio.rest import Client
from dotenv import load_dotenv
from typing import Optional

load_dotenv()

app = FastAPI()

# Set up templates and static files
templates = Jinja2Templates(directory="templates")
# app.mount("/static", StaticFiles(directory="static"), name="static")

# Configuration
TWILIO_AUTH_TOKEN = os.getenv('AUTH_TOKEN')
TWILIO_SID = os.getenv('ACCOUNT_SID')
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER")

twilio_client = Client(TWILIO_SID, TWILIO_AUTH_TOKEN)

class CallRequest(BaseModel):
    to: str
    twilio_call_url: Optional[str] = "https://handler.twilio.com/twiml/EH9d9a02c85d858747bf10c9c8880bd078"

# local: https://handler.twilio.com/twiml/EH9d9a02c85d858747bf10c9c8880bd078
# Cloud: https://handler.twilio.com/twiml/EH0db6372522f950d90f33662d5f3b3881

@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/start-call/")
async def start_call(request: CallRequest):
    to = request.to
    call = twilio_client.calls.create(
        from_="+17753177891",
        to=to,
        url=request.twilio_call_url
    )
    return {"status": "Call initiated", "sid": call.sid}
