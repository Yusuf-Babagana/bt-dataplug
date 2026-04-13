from rest_framework import serializers
from .models import Profile, DataPlan, Transaction

class DataPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = DataPlan
        fields = ['id', 'network', 'size', 'selling_price', 'validity']

class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = ['id', 'service_type', 'amount', 'status', 'recipient', 'created_at']
