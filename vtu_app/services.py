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
        import os

        # 1. Strip everything to ensure NO hidden spaces
        api_key = str(os.getenv('MONNIFY_API_KEY')).strip()
        secret_key = str(os.getenv('MONNIFY_SECRET_KEY')).strip()

        # 2. Format the string exactly: "ApiKey:SecretKey"
        auth_str = f"{api_key}:{secret_key}"

        # 3. Encode to Base64 without any extra characters
        encoded_auth = base64.b64encode(auth_str.encode('ascii')).decode('ascii')

        url = "https://api.monnify.com/api/v1/auth/login"
        headers = {
            'Authorization': f'Basic {encoded_auth}',
            'Content-Type': 'application/json'
        }

        # 4. Fire the request (No proxy needed on Paid Plan!)
        response = requests.post(url, headers=headers, timeout=20)
        res_data = response.json()

        if response.status_code == 200 and res_data.get('requestSuccessful'):
            return res_data['responseBody']['accessToken']
        else:
            # This will tell us if it's "Invalid Client" or "Inactive Account"
            error_msg = res_data.get('responseMessage', 'Unauthorized')
            raise Exception(f"Monnify says: {error_msg} (Code: {response.status_code})")

    def reserve_account(self, user):
        """Creates a dedicated bank account for a user"""
        token = self.get_auth_token()
        url = f"{self.base_url}/api/v2/bank-transfer/reserved-accounts"
        headers = {'Authorization': f'Bearer {token}'}

        # Fall back to username if first/last name are blank
        # (Django's default UserCreationForm doesn't require names)
        full_name = f"{user.first_name} {user.last_name}".strip()
        if not full_name:
            full_name = user.username

        data = {
            "accountReference": f"REF-{user.id}",
            "accountName": full_name,
            "currencyCode": "NGN",
            "contractCode": self.contract_code,
            "customerEmail": user.email,
            "customerName": full_name,
            "getAllAvailableBanks": True
        }

        # Debug: log exactly what we're sending
        print(f"[Monnify] reserve_account payload: {data}")

        response = requests.post(url, json=data, headers=headers, timeout=20)
        return response.json()
