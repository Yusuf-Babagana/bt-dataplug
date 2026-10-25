from django.urls import path
from . import portal

urlpatterns = [
    path('', portal.docs, name='developer_docs'),
    path('openapi.json', portal.openapi_json, name='developer_openapi'),
    path('reference/', portal.swagger, name='developer_reference'),
    path('console/', portal.console, name='developer_console'),
    path('console/enable/', portal.enable, name='developer_enable'),
    path('console/apps/new/', portal.app_create, name='developer_app_create'),
    path('console/apps/<int:pk>/', portal.app_detail, name='developer_app'),
    path('console/apps/<int:pk>/action/', portal.app_action, name='developer_app_action'),
]
