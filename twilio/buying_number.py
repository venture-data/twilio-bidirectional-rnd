from fastapi import FastAPI, HTTPException, Query
from typing import Optional
import requests
from pydantic import BaseModel
from fastapi.responses import JSONResponse
import logging
from base64 import b64encode

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = FastAPI(title="Twilio Phone Number Service")

# Twilio credentials
ACCOUNT_SID = "AC6ca1af2aa8f007b5e5ea9c94a17c336f"
AUTH_TOKEN = "969118e942bfbe5268cc42e86b1f9a1b"
BASE_URL = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}"

# Create basic auth header
auth_string = f"{ACCOUNT_SID}:{AUTH_TOKEN}"
basic_auth = b64encode(auth_string.encode()).decode()

class PhoneNumberPurchase(BaseModel):
    phone_number: str
    friendly_name: Optional[str] = None

def make_twilio_request(method: str, url: str, params: dict = None, data: dict = None):
    headers = {
        'Authorization': f'Basic {basic_auth}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    
    try:
        logger.debug(f"Making Twilio request: {method} {url}")
        logger.debug(f"Headers: {headers}")
        logger.debug(f"Params: {params}")
        
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            data=data
        )
        
        # Log the response for debugging
        logger.debug(f"Response status: {response.status_code}")
        logger.debug(f"Response content: {response.text[:200]}...")
        
        if response.status_code == 401:
            raise HTTPException(status_code=401, detail="Invalid Twilio credentials")
            
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Twilio API error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/search-numbers/")
async def search_phone_numbers(
    country_code: str = Query(default="US", description="Country code (e.g., US)"),
    number_type: str = Query(default="Local", description="Type of number (Local, Mobile, or TollFree)"),
    area_code: Optional[str] = Query(default=None, description="Area code (e.g., 775)"),
    sms_enabled: Optional[bool] = Query(default=None, description="Filter for SMS capability"),
    mms_enabled: Optional[bool] = Query(default=None, description="Filter for MMS capability"),
    voice_enabled: Optional[bool] = Query(default=None, description="Filter for voice capability")
):
    """
    Search for available phone numbers based on specified criteria
    """
    try:
        # Construct the search URL
        search_url = f"{BASE_URL}/AvailablePhoneNumbers/{country_code}/{number_type}.json"
        
        # Prepare search parameters
        search_params = {}
        if area_code:
            search_params["AreaCode"] = area_code
        if sms_enabled is not None:
            search_params["SmsEnabled"] = str(sms_enabled).lower()
        if mms_enabled is not None:
            search_params["MmsEnabled"] = str(mms_enabled).lower()
        if voice_enabled is not None:
            search_params["VoiceEnabled"] = str(voice_enabled).lower()

        # Make the request to Twilio
        result = make_twilio_request("GET", search_url, params=search_params)
        
        # Return the available phone numbers
        return {
            "status": "success",
            "data": result.get("available_phone_numbers", [])
        }
        
    except Exception as e:
        logger.error(f"Error searching for numbers: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/purchase-number/")
async def purchase_phone_number(purchase_data: PhoneNumberPurchase):
    """
    Purchase a specific phone number
    """
    try:
        purchase_url = f"{BASE_URL}/IncomingPhoneNumbers.json"
        
        purchase_params = {
            "PhoneNumber": purchase_data.phone_number
        }
        if purchase_data.friendly_name:
            purchase_params["FriendlyName"] = purchase_data.friendly_name

        result = make_twilio_request("POST", purchase_url, data=purchase_params)
        
        return JSONResponse(
            content={
                "status": "success",
                "message": "Phone number purchased successfully",
                "data": {
                    "phone_number": result.get("phone_number"),
                    "sid": result.get("sid")
                }
            },
            status_code=201
        )
    except Exception as e:
        logger.error(f"Error purchasing number: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="debug")