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
        
        # PythonAnywhere proxy setup
        self.proxy = {
            "http": "http://proxy.server:3128",
            "https": "http://proxy.server:3128",
        }

    def get_balance(self):
        url = f"{self.base_url}/APIBalance.asp?UserID={self.user_id}&APIKey={self.api_key}"
        try:
            # Added proxies and a timeout limit
            response = requests.get(url, proxies=self.proxy, timeout=10)
            
            if response.status_code == 200:
                # Try parsing JSON or return text
                try:
                    return response.json().get('balance', response.text)
                except:
                    return response.text
            return "API Error"
        except requests.exceptions.ProxyError:
            return "Proxy Error"
        except requests.exceptions.Timeout:
            return "Timeout"
        except Exception as e:
            return f"Error: {str(e)}"

    def buy_data(self, network, plan, phone):
        """Triggers a data purchase"""
        url = f"{self.base_url}/Data.asp?UserID={self.user_id}&APIKey={self.api_key}&MobileNetwork={network}&DataPlan={plan}&MobileNumber={phone}"
        response = requests.get(url)
        return response.json()

class MonnifyService:
    def __init__(self):
        self.api_key = settings.MONNIFY_API_KEY
        self.secret_key = settings.MONNIFY_SECRET_KEY
        self.base_url = settings.MONNIFY_BASE_URL
        
        self.proxy = {
            "http": "http://proxy.server:3128",
            "https": "http://proxy.server:3128",
        }

    def get_auth_token(self):
        """Generates the required Bearer Token for Monnify requests"""
        auth_str = f"{self.api_key}:{self.secret_key}"
        encoded_auth = base64.b64encode(auth_str.encode()).decode()
        url = f"{self.base_url}/api/v1/auth/login"
        headers = {'Authorization': f'Basic {encoded_auth}'}
        
        try:
            response = requests.post(url, headers=headers, proxies=self.proxy, timeout=15)
            
            # Temporary Debug Prints
            print("--- MONNIFY RAW AUTH RESPONSE ---")
            print(f"Status Code: {response.status_code}")
            print(f"RAW TEXT: {response.text}")
            print("---------------------------------")
            
            res_data = response.json()
            
            # Check if login was successful
            if response.status_code == 200 and res_data.get('requestSuccessful'):
                return res_data['responseBody']['accessToken']
            else:
                # This will show in your Django messages
                raise Exception(f"Login Failed: {res_data.get('responseMessage', response.text)}")
                
        except requests.exceptions.ProxyError:
            raise Exception("PythonAnywhere Proxy Blocked this request. Is Monnify Whitelisted?")
        except Exception as e:
            raise Exception(f"Auth Error: {str(e)}")

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
        response = requests.post(url, json=data, headers=headers, proxies=self.proxy, timeout=15)
        return response.json()
