import os
from fastapi import FastAPI
from pydantic import BaseModel
from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()


# Configuration
TWILIO_AUTH_TOKEN = os.getenv('AUTH_TOKEN')
TWILIO_SID = os.getenv('ACCOUNT_SID')
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER")

twilio_client = Client(TWILIO_SID, TWILIO_AUTH_TOKEN)

class CallRequest(BaseModel):
    to: str

@app.post("/start-call/")
async def start_call(request: CallRequest):
    to = request.to
    """
    Start an outbound call using Twilio.
    """
    twilio_call_url = "https://handler.twilio.com/twiml/EH9d9a02c85d858747bf10c9c8880bd078"  # Use your ngrok URL.

    call = twilio_client.calls.create(
        from_="+17753177891", # +17753177891 +12185857512 +15512967933
        to=to,
        url=twilio_call_url
    )
    return {"status": "Call initiated", "sid": call.sid}