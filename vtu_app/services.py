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
        self.api_key = os.getenv('MONNIFY_API_KEY').strip()
        self.secret_key = os.getenv('MONNIFY_SECRET_KEY').strip()
        self.base_url = "https://api.monnify.com"  # Live
        # No proxy needed anymore!

    def get_auth_token(self):
        auth_str = f"{self.api_key}:{self.secret_key}"
        encoded_auth = base64.b64encode(auth_str.encode('ascii')).decode('ascii')

        url = f"{self.base_url}/api/v1/auth/login"
        headers = {'Authorization': f'Basic {encoded_auth}'}

        # Simple request without proxies
        response = requests.post(url, headers=headers, timeout=20)
        res_data = response.json()

        if response.status_code == 200 and res_data.get('requestSuccessful'):
            return res_data['responseBody']['accessToken']
        raise Exception(f"Login Failed: {res_data.get('responseMessage')}")

    def reserve_account(self, user):
        """Creates a dedicated bank account for a user"""
        token = self.get_auth_token()
        url = f"{self.base_url}/api/v2/bank-transfer/reserved-accounts"
        headers = {'Authorization': f'Bearer {token}'}
        data = {
            "accountReference": f"REF-{user.id}",
            "accountName": f"{user.first_name} {user.last_name}",
            "currencyCode": "NGN",
            "contractCode": settings.MONNIFY_CONTRACT_CODE,
            "customerEmail": user.email,
            "customerName": user.username,
            "getAllAvailableBanks": True
        }
        response = requests.post(url, json=data, headers=headers, timeout=15)
        return response.json()
