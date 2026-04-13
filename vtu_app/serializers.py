from rest_framework import serializers
from .models import Profile, DataPlan, Transaction

class DataPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = DataPlan
        # Using the exact fields from your 'Choices' list in the error
        fields = ['id', 'network', 'plan_name', 'price', 'dataplan_id']

class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = ['id', 'service_type', 'amount', 'status', 'recipient', 'created_at']
