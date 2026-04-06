from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('buy-data/', views.buy_data, name='buy_data'),
]
