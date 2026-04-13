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
    
    # Extract data (matching your website form fields)
    username = data.get('username')
    email = data.get('email')
    password = data.get('password')
    first_name = data.get('first_name', '')
    last_name = data.get('last_name', '')

    # 1. Validation (Same as website)
    if User.objects.filter(username=username).exists():
        return Response({"message": "Username already exists"}, status=status.HTTP_400_BAD_REQUEST)
    
    if User.objects.filter(email=email).exists():
        return Response({"message": "Email already registered"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        # 2. Create User exactly like the web registration
        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name
        )

        # 3. Create Profile (Ensure it matches your website signal or manual creation)
        profile, created = Profile.objects.get_or_create(user=user)

        # 4. Trigger the Monnify Account Reservation (The "YUS" Branding)
        # This is the "Magic" that makes the mobile app match the site
        monnify = MonnifyService()
        response = monnify.reserve_account(user)
        
        if response.get('requestSuccessful'):
            accounts = response.get('responseBody', {}).get('accounts', [])
            profile.bank_accounts = accounts
            profile.save()
            return Response({
                "message": "Registration Successful",
                "accounts_generated": True
            }, status=status.HTTP_201_CREATED)
        else:
            # Even if Monnify fails, the user is created (just like your site logic)
            return Response({
                "message": "Account created, but bank numbers are pending.",
                "accounts_generated": False
            }, status=status.HTTP_201_CREATED)

    except Exception as e:
        return Response({"message": f"Server Error: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
