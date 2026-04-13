from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .models import DataPlan, Transaction
from .serializers import DataPlanSerializer, TransactionSerializer

class MobileDashboard(APIView):
    permission_classes = [IsAuthenticated] # Must have a Token

    def get(self, request):
        user = request.user
        recent_tx = Transaction.objects.filter(user=user).order_by('-created_at')[:5]
        
        return Response({
            "username": user.username,
            "wallet_balance": user.profile.wallet_balance,
            "recent_transactions": TransactionSerializer(recent_tx, many=True).data,
            "api_status": "Active"
        })

class DataPlanList(APIView):
    # Public: So users can see prices even before login
    def get(self, request):
        plans = DataPlan.objects.filter(is_active=True)
        serializer = DataPlanSerializer(plans, many=True)
        return Response(serializer.data)
