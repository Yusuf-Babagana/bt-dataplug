import time
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.contrib.auth.models import User
from .models import DataPlan, Transaction, Profile
from .serializers import DataPlanSerializer, TransactionSerializer
from .services import MonnifyService, ClubKonnectService
from .services.transaction_service import TransactionService

class MobileDashboard(APIView):
    permission_classes = [IsAuthenticated] # Must have a Token

    def get(self, request):
        user = request.user
        profile = user.profile
        
        # We ensure the bank_accounts field is sent as a list
        return Response({
            "username": user.username,
            "profile": {
                "wallet_balance": str(profile.wallet_balance),
                "bank_accounts": profile.bank_accounts if isinstance(profile.bank_accounts, list) else []
            },
            "system_announcement": "Welcome to the new BT DataPlug Mobile App!"
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

class TransactionHistory(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Using order_by instead of the typo'd order_back
        transactions = Transaction.objects.filter(user=request.user).order_by("-created_at")[:20]
        data = [{
            "id": tx.id,
            "type": tx.service_type,
            "amount": tx.amount,
            "status": tx.status,
            "date": tx.created_at.strftime("%d %b, %H:%M")
        } for tx in transactions]
        return Response(data)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def api_buy_data(request):
    """
    Production-grade Mobile Data Purchase.
    Uses TransactionService for atomic balance deductions and profit tracking.
    """
    user = request.user
    plan_id = request.data.get('plan_id')
    phone = request.data.get('phone')

    if not plan_id or not phone:
        return Response({"message": "Missing plan_id or phone number"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        plan = DataPlan.objects.get(id=plan_id)
    except DataPlan.DoesNotExist:
        return Response({"message": "Invalid Plan Selected"}, status=status.HTTP_404_NOT_FOUND)

    # 1. ACQUISITION OF LOCK & ATOMIC DEBIT
    success, result = TransactionService.process_debit(
        user=user,
        amount=plan.price,
        service_type="Data Purchase (Mobile)",
        plan_name=plan.plan_name,
        recipient=phone,
        reference=f"DTM-{int(time.time())}",
        description=f"Mobile purchase of {plan.plan_name} for {phone}",
        cost_price=plan.cost_price
    )

    if not success:
        return Response({"message": f"Transaction failed: {result}"}, status=status.HTTP_400_BAD_REQUEST)

    # 2. CALL PROVIDER API
    try:
        ck = ClubKonnectService()
        # Corrected argument order: network, plan_id, phone
        response, req_id = ck.buy_data(plan.network, plan.dataplan_id, phone)

        if response.get('status') == 'ORDER_RECEIVED':
            # 3. SUCCESS - Finalize record
            Transaction.objects.filter(reference=result.reference).update(status="Successful")
            return Response({
                "message": "Transaction Successful!",
                "new_balance": str(user.profile.wallet_balance),
                "order_id": response.get('order_id', req_id)
            }, status=status.HTTP_200_OK)
        
        else:
            # 4. REFUND ON API FAILURE
            TransactionService.process_refund(user, plan.price, result.reference, "API Failure (Mobile)")
            return Response({
                "message": f"Service provider busy: {response.get('remarks', 'Try again later')}. Funds refunded."
            }, status=status.HTTP_400_BAD_REQUEST)

    except Exception as e:
        TransactionService.process_refund(user, plan.price, result.reference, "System Crash (Mobile)")
        return Response({"message": f"System error occurred. Funds refunded."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
