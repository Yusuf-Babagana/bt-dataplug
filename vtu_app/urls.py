from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('buy-data/', views.dashboard, name='buy_data'), # Temporary placeholder
]
