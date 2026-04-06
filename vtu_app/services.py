import requests
import os

class ClubKonnectService:
    def __init__(self):
        self.user_id = os.getenv('CK_USER_ID')
        self.api_key = os.getenv('CK_API_KEY')
        # Updated Base URL to their primary API engine
        self.base_url = "https://www.nellobytesystems.com"

    def get_balance(self):
        """Checks your reseller balance"""
        # Note the change to APIBalance.asp
        url = f"{self.base_url}/APIBalance.asp?UserID={self.user_id}&APIKey={self.api_key}"
        try:
            response = requests.get(url)
            
            # Check if we got a 404 or other server error
            if response.status_code != 200:
                return "Error: API Endpoint Not Found"
            
            # If successful, they usually return a JSON string like {"balance":"403.64"}
            # or a simple text value. Let's try to parse it safely.
            try:
                data = response.json()
                return data.get('wallet_balance', data.get('balance', response.text))
            except:
                return response.text
        except requests.RequestException:
            return "Connection Timeout"

    def buy_data(self, network, plan, phone):
        """Triggers a data purchase"""
        url = f"{self.base_url}/Data.asp?UserID={self.user_id}&APIKey={self.api_key}&MobileNetwork={network}&DataPlan={plan}&MobileNumber={phone}"
        response = requests.get(url)
        return response.json()
