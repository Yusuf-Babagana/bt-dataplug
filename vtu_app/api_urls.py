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
    path('cable-plans/', api_views.CablePlanList.as_view(), name='api_cable_plans'),
    
    # Wallet Record (Transaction History)
    path('transactions/', api_views.api_transaction_history, name='api_transactions'),

    # Transactions
    path('buy-data/', api_views.api_buy_data, name='api_buy_data'),
    path('buy-airtime/', api_views.api_buy_airtime, name='api_buy_airtime'),
    path('change-pin/', api_views.api_change_pin, name='api_change_pin'),
    
    # Cable TV
    path('validate-cable/', api_views.api_validate_cable, name='api_validate_cable'),
    path('buy-cable/', api_views.api_buy_cable, name='api_buy_cable'),

    # Electricity
    path('validate-meter/', api_views.api_validate_meter, name='api_validate_meter'),
    path('pay-electricity/', api_views.api_pay_electricity, name='api_pay_electricity'),
    
    # Notifications
    path('notifications/', api_views.api_get_notifications, name='api_notifications'),
]
