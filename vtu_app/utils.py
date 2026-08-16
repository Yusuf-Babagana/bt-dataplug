import requests
import os
import uuid
import logging

logger = logging.getLogger('vtu_app')

class ClubKonnectService:
    def __init__(self):
        # NELLOBYTE_* is the current naming; CK_* is kept as a fallback so
        # an environment that hasn't been updated yet still works.
        self.user_id = os.getenv('NELLOBYTE_USER_ID', os.getenv('CK_USER_ID', '')).strip()
        self.api_key = os.getenv('NELLOBYTE_API_KEY', os.getenv('CK_API_KEY', '')).strip()
        self.base_url = os.getenv('NELLOBYTE_BASE_URL', 'https://www.nellobytesystems.com').strip().rstrip('/') + '/'

    def verify_meter(self, disco_code, meter_no, meter_type):
        """Verify meter number to get customer name."""
        url = f"{self.base_url}APIVerifyElectricityV1.asp"
        params = {
            "UserID": self.user_id,
            "APIKey": self.api_key,
            "ElectricCompany": disco_code,
            "MeterNo": meter_no,
            "MeterType": meter_type
        }
        headers = {'User-Agent': 'Mozilla/5.0 BT-DataPlug/1.0'}
        try:
            response = requests.get(url, params=params, headers=headers, timeout=20)
            logger.info(f"Verify Meter Debug --- Status: {response.status_code} Body: {response.text}")
            return response.json() # Returns {"customer_name": "..."}
        except:
            return {"customer_name": "Error validating number"}

    def buy_electricity(self, disco_code, meter_no, meter_type, amount, phone):
        """Purchase electricity token or pay postpaid bill."""
        request_id = uuid.uuid4().hex[:12]
        url = f"{self.base_url}APIElectricityV1.asp"
        params = {
            "UserID": self.user_id,
            "APIKey": self.api_key,
            "ElectricCompany": disco_code,
            "MeterType": meter_type,
            "MeterNo": meter_no,
            "Amount": amount,
            "PhoneNo": phone,
            "RequestID": request_id
        }
        headers = {'User-Agent': 'Mozilla/5.0 BT-DataPlug/1.0'}
        try:
            response = requests.get(url, params=params, headers=headers, timeout=30)
            logger.info(f"Buy Electricity Debug --- Status: {response.status_code} Body: {response.text}")
            try:
                return response.json(), request_id
            except ValueError:
                return {"status": response.text.strip()}, request_id
        except Exception as e:
            return {"status": "ERROR", "remark": str(e)}, request_id
