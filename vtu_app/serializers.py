from rest_framework import serializers
from .models import Profile, DataPlan, Transaction

class DataPlanSerializer(serializers.ModelSerializer):
    # This ensures the mobile app always gets clean, trimmed names
    network = serializers.SerializerMethodField()

    class Meta:
        model = DataPlan
        # Using the exact fields from your 'Choices' list in the error
        fields = ['id', 'network', 'plan_name', 'price', 'dataplan_id']

    def get_network(self, obj):
        mapping = {
            '01': 'MTN',
            '02': 'GLO',
            '03': '9MOBILE',
            '04': 'AIRTEL'
        }
        raw_network = obj.network.strip()
        return mapping.get(raw_network, raw_network).upper()

class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = ['id', 'service_type', 'amount', 'status', 'recipient', 'created_at']
