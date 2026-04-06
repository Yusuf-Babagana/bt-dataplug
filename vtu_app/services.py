import requests
import os

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
