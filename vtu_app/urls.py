from django.urls import path
from . import views
from .webhooks import monnify_webhook

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('buy-data/', views.buy_data, name='buy_data'),
    path('monnify-webhook/', monnify_webhook, name='monnify_webhook'),
    path('complete-kyc/', views.complete_kyc, name='complete_kyc'),
]
