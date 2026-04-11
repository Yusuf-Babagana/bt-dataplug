import requests
import os
import hmac
import hashlib
import base64
import uuid
from django.conf import settings


class ClubKonnectService:
    def __init__(self):
        self.user_id = os.getenv('CK_USER_ID', '').strip()
        self.api_key = os.getenv('CK_API_KEY', '').strip()
        self.base_url = "https://www.nellobytesystems.com"
        self.balance_url = f"{self.base_url}/APIWalletBalanceV1.asp"

    def get_balance(self):
        """Check your main BT DataPlug balance on ClubKonnect"""
        url = f"{self.balance_url}?UserID={self.user_id}&APIKey={self.api_key}"
        try:
            # Since you are on a Paid Account, no proxy is needed
            response = requests.get(url, timeout=15)
            data = response.json()
            
            # Check for error strings in the response
            if isinstance(data, str) and "INVALID" in data:
                return {"error": data}
                
            return data # Returns {"date": "...", "id": "...", "balance": "3500"}
        except Exception as e:
            return {"error": str(e)}

    def buy_data(self, network_code, plan_id, phone):
        """Send data to a customer using the APIDatabundleV1 endpoint."""
        request_id = uuid.uuid4().hex[:12]
        url = (
            f"https://www.nellobytesystems.com/APIDatabundleV1.asp"
            f"?UserID={self.user_id}&APIKey={self.api_key}"
            f"&MobileNetwork={network_code}&DataPlan={plan_id}"
            f"&MobileNumber={phone}&RequestID={request_id}"
        )
        
        # Professional User-Agent to prevent security blocks
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) BT-DataPlug/1.0'
        }

        try:
            response = requests.get(url, headers=headers, timeout=30)

            # DEEP DEBUG: Shows in PythonAnywhere Error Log
            print(f"--- ClubKonnect Debug ---")
            print(f"URL: {url.replace(self.api_key, 'HIDDEN')}")
            print(f"Status: {response.status_code}")
            print(f"Body: {response.text}")

            # Safely attempt to parse JSON. If ClubKonnect returns a plain string 
            # (like INSUFFICIENT_BALANCE), we capture it without crashing.
            try:
                return response.json(), request_id
            except ValueError:
                # If response is not JSON, return the text as a status/remark
                return {"status": response.text.strip(), "remark": response.text.strip()}, request_id

        except Exception as e:
            print(f"ClubKonnect Connection Failed: {str(e)}")
            return {"status": "ERROR", "remark": "Connection Timeout"}, request_id

    def buy_airtime(self, network_code, amount, phone):
        """Purchase airtime using the APIAirtimeV1 endpoint."""
        request_id = uuid.uuid4().hex[:12]
        url = (
            f"https://www.nellobytesystems.com/APIAirtimeV1.asp"
            f"?UserID={self.user_id}&APIKey={self.api_key}"
            f"&MobileNetwork={network_code}&Amount={amount}"
            f"&MobileNumber={phone}&RequestID={request_id}"
        )
        
        headers = {'User-Agent': 'Mozilla/5.0 BT-DataPlug/1.0'}

        try:
            response = requests.get(url, headers=headers, timeout=30)
            print(f"--- Airtime Debug --- Status: {response.status_code} Body: {response.text}")

            try:
                return response.json(), request_id
            except ValueError:
                return {"status": response.text.strip(), "remark": response.text.strip()}, request_id

        except Exception as e:
            return {"status": "ERROR", "remark": str(e)}, request_id

    def verify_cable(self, cable_tv, smartcard):
        """Verify the customer name using Smartcard/IUC."""
        url = (
            f"https://www.nellobytesystems.com/APIVerifyCableTVV1.0.asp"
            f"?UserID={self.user_id}&APIKey={self.api_key}"
            f"&CableTV={cable_tv}&SmartCardNo={smartcard}"
        )
        headers = {'User-Agent': 'Mozilla/5.0 BT-DataPlug/1.0'}
        try:
            response = requests.get(url, headers=headers, timeout=20)
            print(f"--- Cable Verify Debug --- Status: {response.status_code} Body: {response.text}")
            return response.json() # Returns {"customer_name": "..."}
        except:
            return {"customer_name": "Error validating number"}

    def buy_cable(self, cable_tv, package, smartcard, phone):
        """Purchase the cable subscription."""
        request_id = uuid.uuid4().hex[:12]
        url = (
            f"https://www.nellobytesystems.com/APICableTVV1.asp"
            f"?UserID={self.user_id}&APIKey={self.api_key}&CableTV={cable_tv}"
            f"&Package={package}&SmartCardNo={smartcard}&PhoneNo={phone}&RequestID={request_id}"
        )
        headers = {'User-Agent': 'Mozilla/5.0 BT-DataPlug/1.0'}
        try:
            response = requests.get(url, headers=headers, timeout=30)
            print(f"--- Cable Buy Debug --- Status: {response.status_code} Body: {response.text}")
            try:
                return response.json(), request_id
            except ValueError:
                return {"status": response.text.strip()}, request_id
        except Exception as e:
            return {"status": "ERROR", "remark": str(e)}, request_id


class MonnifyService:
    def __init__(self):
        self.api_key = os.getenv('MONNIFY_API_KEY')
        self.secret_key = os.getenv('MONNIFY_SECRET_KEY')
        self.contract_code = os.getenv('MONNIFY_CONTRACT_CODE')

        # Check if any are None BEFORE stripping
        if not all([self.api_key, self.secret_key, self.contract_code]):
            raise Exception("Critical Error: Monnify credentials missing in .env file!")

        self.api_key = self.api_key.strip()
        self.secret_key = self.secret_key.strip()
        self.contract_code = self.contract_code.strip()
        self.base_url = "https://api.monnify.com"
        # No proxy needed anymore!

    def get_auth_token(self):
        import base64
        import requests

        # Use the already cleaned keys from __init__
        auth_str = f"{self.api_key}:{self.secret_key}"
        encoded_auth = base64.b64encode(auth_str.encode('ascii')).decode('ascii')

        url = "https://api.monnify.com/api/v1/auth/login"
        headers = {
            'Authorization': f'Basic {encoded_auth}',
            'Content-Type': 'application/json'
        }

        response = requests.post(url, headers=headers, timeout=20)
        res_data = response.json()

        if response.status_code == 200 and res_data.get('requestSuccessful'):
            return res_data['responseBody']['accessToken']
        else:
            error_msg = res_data.get('responseMessage', 'Unauthorized')
            raise Exception(f"Monnify says: {error_msg} (Code: {response.status_code})")

    def reserve_account(self, user):
        """
        Creates a legal Tier 1 bank account.
        We REMOVE the BVN/NIN fields so the customer doesn't have to provide them.
        We use the CUSTOMER'S username as the account name.
        """
        token = self.get_auth_token()
        url = f"{self.base_url}/api/v2/bank-transfer/reserved-accounts"
        headers = {'Authorization': f'Bearer {token}'}

        # We use the customer's username for the account name as you requested
        # We prefix it with 'BT-' so it looks professional in their bank app
        display_name = f"BT-{user.username}".upper()
        email = user.email if user.email else f"{user.username}@btdataplug.com"

        data = {
            "accountReference": f"REF-{user.id}",
            "accountName": display_name, 
            "currencyCode": "NGN",
            "contractCode": self.contract_code,
            "customerEmail": email,
            "customerName": user.username, # Their username
            "getAllAvailableBanks": True
            # NOTICE: No BVN or NIN here. This triggers a 'Tier 1' account.
        }

        # Debug: log payload
        print(f"[Monnify] Payload being sent: {data}")

        response = requests.post(url, json=data, headers=headers, timeout=20)
        return response.json()