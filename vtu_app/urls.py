from django.urls import path
from . import views
from .webhooks import monnify_webhook

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('buy-data/', views.buy_data, name='buy_data'),
    path('receipt/<int:tx_id>/', views.receipt, name='receipt'),
    path('monnify-webhook/', monnify_webhook, name='monnify_webhook'),
    path('complete-kyc/', views.complete_kyc, name='complete_kyc'),
    path('buy-airtime/', views.buy_airtime, name='buy_airtime'),
    path('transactions/', views.transaction_history, name='transaction_history'),
    path('buy-cable/', views.buy_cable, name='buy_cable'),
    path('buy-electricity/', views.electricity_view, name='electricity_view'),
    path('ajax/validate-cable/', views.validate_cable, name='validate_cable'),
    path('ajax/validate-meter/', views.validate_meter, name='validate_meter'),
    path('ajax/get-balance/', views.ajax_get_balance, name='get_balance'),
    path('set-pin/', views.set_transaction_pin, name='set_pin'),
    path('manager/', views.manager_dashboard, name='manager_dashboard'),
    path('staff/dashboard/', views.staff_dashboard, name='staff_dashboard'),
    path('settings/profile/', views.profile_settings, name='profile_settings'),
    path('profile/change-pin/', views.change_pin_view, name='change_pin'),
    path('ref/<str:ref_id>/', views.referral_redirect, name='referral_redirect'),
]
