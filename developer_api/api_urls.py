from django.urls import path
from . import views

# Mounted at /api/v1/dev/ — the public developer API.
urlpatterns = [
    path('balance/', views.BalanceView.as_view(), name='dev_balance'),
    path('services/', views.ServicesView.as_view(), name='dev_services'),
    path('networks/', views.NetworksView.as_view(), name='dev_networks'),
    path('data-plans/', views.DataPlansView.as_view(), name='dev_data_plans'),
    path('cable-plans/', views.CablePlansView.as_view(), name='dev_cable_plans'),
    path('electricity/discos/', views.DiscosView.as_view(), name='dev_discos'),
    path('verify/meter/', views.VerifyMeterView.as_view(), name='dev_verify_meter'),
    path('verify/cable/', views.VerifyCableView.as_view(), name='dev_verify_cable'),
    path('airtime/', views.AirtimeView.as_view(), name='dev_airtime'),
    path('data/', views.DataView.as_view(), name='dev_data'),
    path('electricity/', views.ElectricityView.as_view(), name='dev_electricity'),
    path('cable/', views.CableView.as_view(), name='dev_cable'),
    path('transactions/', views.TransactionListView.as_view(), name='dev_transactions'),
    path('transactions/<str:reference>/', views.TransactionDetailView.as_view(), name='dev_transaction'),
]
