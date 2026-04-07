import requests
import os
import hmac
import hashlib
import base64
from django.conf import settings


class ClubKonnectService:
    def __init__(self):
        self.user_id = os.getenv('CK_USER_ID')
        self.api_key = os.getenv('CK_API_KEY')
        self.base_url = "https://www.nellobytesystems.com"
        # No proxy needed anymore!

    def get_balance(self):
        url = f"{self.base_url}/APIBalance.asp?UserID={self.user_id}&APIKey={self.api_key}"
        try:
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                try:
                    return response.json().get('balance', response.text)
                except Exception:
                    return response.text
            return "API Error"
        except requests.exceptions.Timeout:
            return "Timeout"
        except Exception as e:
            return f"Error: {str(e)}"

    def buy_data(self, network, plan, phone):
        """Triggers a data purchase"""
        url = f"{self.base_url}/Data.asp?UserID={self.user_id}&APIKey={self.api_key}&MobileNetwork={network}&DataPlan={plan}&MobileNumber={phone}"
        response = requests.get(url, timeout=15)
        return response.json()


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

    def reserve_account(self, user, bvn):
        """Creates a dedicated bank account for a user"""
        token = self.get_auth_token()
        url = f"{self.base_url}/api/v2/bank-transfer/reserved-accounts"
        headers = {'Authorization': f'Bearer {token}'}

        # Handle blank names
        full_name = f"{user.first_name} {user.last_name}".strip()
        if not full_name:
            full_name = user.username

        # Handle blank email (Monnify requires this!)
        email = user.email if user.email else f"{user.username}@btdataplug.com"

        data = {
            "accountReference": f"REF-{user.id}",
            "accountName": full_name,
            "currencyCode": "NGN",
            "contractCode": self.contract_code,
            "customerEmail": email,
            "customerName": full_name,
            "customerBvn": bvn, # Added BVN for KYC
            "getAllAvailableBanks": True
        }

        # Debug: log exactly what we're sending
        print(f"[Monnify] Payload being sent: {data}")

        response = requests.post(url, json=data, headers=headers, timeout=20)
        return response.json()
