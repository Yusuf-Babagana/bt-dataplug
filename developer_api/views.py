import logging
import time
import uuid
from decimal import Decimal

from django.http import Http404
from rest_framework import exceptions as drf_exc
from rest_framework.parsers import JSONParser
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from vtu_app.models import CablePlan, DataPlan, Profile, ServiceSwitch
from vtu_app.services.external_apis import ClubKonnectService

from . import services
from .auth import ApiKeyAuthentication
from .errors import ApiError, validation_error
from .models import ApiRequestLog, ApiTransaction

logger = logging.getLogger('vtu_app')
TEST_BALANCE = '100000.00'


class AppAuthenticated(BasePermission):
    def has_permission(self, request, view):
        return request.auth is not None and getattr(request._request, 'api_app', None) is request.auth


class BaseApiView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [AppAuthenticated]
    parser_classes = [JSONParser]
    throttle_classes = []
    service = None  # set on purchase/verify views to enforce per-application service access

    # --- envelope -----------------------------------------------------------------------------
    def initial(self, request, *args, **kwargs):
        request._request.api_started = time.monotonic()
        request._request.api_request_id = f'req_{uuid.uuid4().hex[:16]}'
        super().initial(request, *args, **kwargs)
        if self.service and not request._request.api_app.allows(self.service):
            raise ApiError(403, 'service_not_permitted', f'This application is not permitted to use the {self.service} service.')

    def ok(self, data, status=200, **extra):
        return Response({'success': True, 'data': data, **extra}, status=status)

    def handle_exception(self, exc):
        headers = {}
        if isinstance(exc, ApiError):
            status, body, headers = exc.status, self._error(exc.code, exc.message, exc.details), exc.headers
        elif isinstance(exc, drf_exc.ParseError):
            status, body = 400, self._error('invalid_json', 'The request body is not valid JSON.')
        elif isinstance(exc, drf_exc.UnsupportedMediaType):
            status, body = 415, self._error('unsupported_media_type', 'Send the body as JSON with Content-Type: application/json.')
        elif isinstance(exc, drf_exc.MethodNotAllowed):
            status, body = 405, self._error('method_not_allowed', 'Method not allowed for this endpoint.')
        elif isinstance(exc, (Http404, drf_exc.NotFound)):
            status, body = 404, self._error('not_found', 'Resource not found.')
        elif isinstance(exc, (drf_exc.NotAuthenticated, drf_exc.AuthenticationFailed)):
            status, body = 401, self._error('invalid_credentials', 'Authentication failed.')
        elif isinstance(exc, drf_exc.PermissionDenied):
            status, body = 403, self._error('forbidden', 'You do not have permission to do that.')
        else:
            logger.exception('API_UNHANDLED_ERROR path=%s', self.request.path)
            status, body = 500, self._error('internal_error', 'Something went wrong on our side. If this was a purchase, check its status before retrying.')
        response = Response(body, status=status)
        for k, v in headers.items():
            response[k] = v
        return response

    def _error(self, code, message, details=None):
        err = {'code': code, 'message': message}
        if details:
            err['details'] = details
        return {'success': False, 'error': err}

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        raw = request._request
        rid = getattr(raw, 'api_request_id', None) or f'req_{uuid.uuid4().hex[:16]}'
        if isinstance(getattr(response, 'data', None), dict):
            response.data['request_id'] = rid
        response['X-Request-Id'] = rid
        response['Cache-Control'] = 'no-store'
        try:
            err = ''
            if isinstance(getattr(response, 'data', None), dict) and not response.data.get('success', True):
                err = response.data.get('error', {}).get('code', '')[:40]
            started = getattr(raw, 'api_started', None)
            ApiRequestLog.objects.create(
                application=getattr(raw, 'api_app', None), public_key_tried='' if getattr(raw, 'api_app', None) else getattr(raw, 'api_public_key', ''),
                method=raw.method[:8], path=raw.path[:200], status_code=response.status_code, error_code=err,
                ip_address=getattr(raw, 'api_client_ip', None),
                duration_ms=int((time.monotonic() - started) * 1000) if started else 0)
        except Exception:
            logger.exception('API_LOG_ERROR')
        return response

    @property
    def app(self):
        return self.request._request.api_app


# ------------------------------------------------------------------------------------------------
# Read endpoints
# ------------------------------------------------------------------------------------------------
class BalanceView(BaseApiView):
    def get(self, request):
        if self.app.is_live:
            balance = str(Profile.objects.get(user=self.app.developer.user).wallet_balance)
        else:
            balance = TEST_BALANCE
        return self.ok({'balance': balance, 'currency': 'NGN', 'environment': self.app.environment})


class ServicesView(BaseApiView):
    def get(self, request):
        return self.ok({'services': [
            {'id': s, 'enabled': self.app.allows(s)} for s in ['airtime', 'data', 'electricity', 'cable']
        ], 'environment': self.app.environment})


class NetworksView(BaseApiView):
    def get(self, request):
        off = {s.network for s in ServiceSwitch.objects.filter(is_active=False)}
        return self.ok({'networks': [
            {'code': code, 'name': name, 'available': name not in off} for code, name in services.NETWORKS.items()
        ]})


class DiscosView(BaseApiView):
    service = 'electricity'

    def get(self, request):
        return self.ok({'discos': [{'code': c, 'name': n} for c, n in services.DISCOS.items()], 'meter_types': ['prepaid', 'postpaid'],
                        'minimum_amount': '1000.00', 'service_fee': str(services.ELECTRICITY_FEE)})


class DataPlansView(BaseApiView):
    service = 'data'

    def get(self, request):
        qs = DataPlan.objects.all().order_by('network', 'price')
        net = (request.query_params.get('network') or '').strip().upper()
        if net:
            code = services.NETWORK_ALIASES.get(net) or net
            qs = qs.filter(network=code)
        return self.ok({'plans': [
            {'plan_id': p.pk, 'network': services.NETWORKS.get(p.network, p.network), 'network_code': p.network,
             'name': p.plan_name, 'price': str(p.price)} for p in qs
        ]})


class CablePlansView(BaseApiView):
    service = 'cable'

    def get(self, request):
        qs = CablePlan.objects.all().order_by('cable_type', 'price')
        ct = (request.query_params.get('cable_tv') or '').strip().lower()
        if ct:
            qs = qs.filter(cable_type=ct)
        return self.ok({'plans': [
            {'package_id': p.plan_id, 'cable_tv': p.cable_type, 'name': p.name, 'price': str(p.price)} for p in qs
        ]})


class VerifyMeterView(BaseApiView):
    service = 'electricity'

    def get(self, request):
        q = request.query_params
        disco, meter_no, mtype = q.get('disco', ''), q.get('meter_no', ''), (q.get('meter_type') or '').lower()
        mtype = {'prepaid': '01', 'postpaid': '02'}.get(mtype, mtype)
        errors = {}
        if disco not in services.DISCOS:
            errors['disco'] = 'Unknown disco code.'
        if not meter_no.isdigit():
            errors['meter_no'] = 'Must be numeric.'
        if mtype not in ('01', '02'):
            errors['meter_type'] = 'Use "prepaid" or "postpaid".'
        if errors:
            raise validation_error(**errors)
        if not self.app.is_live:
            return self.ok({'customer_name': 'TEST CUSTOMER', 'meter_no': meter_no})
        res = ClubKonnectService().validate_meter(disco, meter_no, mtype)
        name = (res or {}).get('customer_name')
        if not name or 'invalid' in str(name).lower() or 'error' in str(name).lower():
            raise ApiError(422, 'verification_failed', 'We could not verify this meter number.')
        return self.ok({'customer_name': name, 'meter_no': meter_no})


class VerifyCableView(BaseApiView):
    service = 'cable'

    def get(self, request):
        q = request.query_params
        cable_tv, card = (q.get('cable_tv') or '').lower(), q.get('smartcard', '')
        errors = {}
        if cable_tv not in services.CABLE_TYPES:
            errors['cable_tv'] = 'Use dstv, gotv or startimes.'
        if not card.isdigit():
            errors['smartcard'] = 'Must be numeric.'
        if errors:
            raise validation_error(**errors)
        if not self.app.is_live:
            return self.ok({'customer_name': 'TEST CUSTOMER', 'smartcard': card})
        res = ClubKonnectService().verify_cable(cable_tv, card)
        name = (res or {}).get('customer_name')
        if not name or 'invalid' in str(name).lower() or 'error' in str(name).lower():
            raise ApiError(422, 'verification_failed', 'We could not verify this smartcard number.')
        return self.ok({'customer_name': name, 'smartcard': card})


# ------------------------------------------------------------------------------------------------
# Purchases
# ------------------------------------------------------------------------------------------------
class PurchaseView(BaseApiView):
    def post(self, request):
        data = request.data
        if not isinstance(data, dict):
            raise ApiError(400, 'invalid_json', 'The request body must be a JSON object.')
        key = request.headers.get('Idempotency-Key', '').strip()
        if not (8 <= len(key) <= 64):
            raise ApiError(400, 'idempotency_key_required', 'Send a unique Idempotency-Key header (8-64 characters) with every purchase.')
        client_ref = str(data.get('client_reference') or '')[:64]
        tx, replayed = services.execute_purchase(self.app, self.service, data, key, client_ref)
        body = services.serialize_transaction(tx)
        if tx.status == 'failed':
            return Response({'success': False, 'error': {'code': 'purchase_failed', 'message': tx.failure_reason or 'The purchase failed.'},
                             'data': body, 'idempotent_replay': replayed}, status=422)
        return self.ok(body, status=200 if replayed else 201, idempotent_replay=replayed)


class AirtimeView(PurchaseView):
    service = 'airtime'


class DataView(PurchaseView):
    service = 'data'


class ElectricityView(PurchaseView):
    service = 'electricity'


class CableView(PurchaseView):
    service = 'cable'


# ------------------------------------------------------------------------------------------------
# Transactions (always scoped to the calling application's developer)
# ------------------------------------------------------------------------------------------------
class TransactionListView(BaseApiView):
    def get(self, request):
        qs = ApiTransaction.objects.filter(application__developer=self.app.developer, is_test=not self.app.is_live)
        q = request.query_params
        if q.get('status') in ('pending', 'successful', 'failed', 'reversed'):
            qs = qs.filter(status=q['status'])
        if q.get('service') in ('airtime', 'data', 'electricity', 'cable'):
            qs = qs.filter(service=q['service'])
        if q.get('client_reference'):
            qs = qs.filter(client_reference=q['client_reference'][:64])
        try:
            limit = max(1, min(int(q.get('limit', 25)), 100))
            offset = max(0, int(q.get('offset', 0)))
        except ValueError:
            raise validation_error(limit='limit and offset must be integers.')
        total = qs.count()
        return self.ok({'transactions': [services.serialize_transaction(t) for t in qs[offset:offset + limit]],
                        'total': total, 'limit': limit, 'offset': offset})


class TransactionDetailView(BaseApiView):
    def get(self, request, reference):
        tx = ApiTransaction.objects.filter(application__developer=self.app.developer, is_test=not self.app.is_live, reference=reference).first()
        if not tx:
            raise ApiError(404, 'not_found', 'Transaction not found.')
        return self.ok(services.serialize_transaction(tx))
