from django.urls import path
from rest_framework.authtoken.views import obtain_auth_token
from . import api_views

urlpatterns = [
    # Mobile Login: Returns a Token
    path('login/', obtain_auth_token, name='api_login'),
    path('register/', api_views.api_register, name='api_register'),
    
    # Dashboard Data (Balance, History)
    path('dashboard/', api_views.MobileDashboard.as_view(), name='api_dashboard'),
    
    # Data Plans for the mobile dropdown
    path('data-plans/', api_views.DataPlanList.as_view(), name='api_plans'),
    
    # Wallet Record (Transaction History)
    path('transactions/', api_views.TransactionHistory.as_view(), name='api_transactions'),

    # Transactions
    path('buy-data/', api_views.api_buy_data, name='api_buy_data'),
]
