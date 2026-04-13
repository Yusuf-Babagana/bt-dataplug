from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.contrib.auth.models import User
from .models import DataPlan, Transaction, Profile
from .serializers import DataPlanSerializer, TransactionSerializer
from .services import MonnifyService

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
    permission_classes = [] # Public access
    
    def get(self, request):
        # Removed .filter(is_active=True) because the field doesn't exist yet
        plans = DataPlan.objects.all() 
        serializer = DataPlanSerializer(plans, many=True)
        return Response(serializer.data)

@api_view(['POST'])
@permission_classes([AllowAny])
def api_register(request):
    data = request.data
    username = data.get('username')
    email = data.get('email')
    password = data.get('password')
    first_name = data.get('first_name', '')
    last_name = data.get('last_name', '')

    if User.objects.filter(username=username).exists():
        return Response({"message": "Username already taken"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        # 1. Create User
        user = User.objects.create_user(
            username=username, 
            email=email, 
            password=password,
            first_name=first_name,
            last_name=last_name
        )
        
        # 2. Profile is usually created via signals, but let's ensure it exists
        profile, created = Profile.objects.get_or_create(user=user)
        
        # 3. Trigger Monnify Account Reservation (The YUS Branding)
        try:
            monnify = MonnifyService()
            res = monnify.reserve_account(user)
            if res.get('requestSuccessful'):
                profile.bank_accounts = res.get('responseBody', {}).get('accounts', [])
                profile.save()
        except Exception as e:
            print(f"Monnify background error: {e}")

        return Response({"message": "Registration successful"}, status=status.HTTP_201_CREATED)

    except Exception as e:
        return Response({"message": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
