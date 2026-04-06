from django.urls import path
from . import views
from .webhooks import monnify_webhook

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('buy-data/', views.buy_data, name='buy_data'),
    path('webhook/monnify/', monnify_webhook, name='monnify_webhook'),
]
