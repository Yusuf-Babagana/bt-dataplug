import json
import time
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import get_resolver
from django.utils import timezone
from rest_framework.test import APIClient

from vtu_app.models import CablePlan, DataPlan, Profile, ServiceSwitch, Transaction as CoreTx, WalletTransaction

from . import services, webhooks
from .models import ApiApplication, ApiRequestLog, ApiTransaction, DeveloperAccount, WebhookDelivery
from .openapi import build_spec

BASE = '/api/v1/dev'
FAST_HASH = ['django.contrib.auth.hashers.MD5PasswordHasher']  # speeds tests; Profile hashes PINs on every save
OK_RESPONSE = ({'status': 'ORDER_RECEIVED', 'orderid': '999'}, 'rid1')


def make_developer(name='dev1', balance='5000.00'):
    user = User.objects.create_user(name, f'{name}@example.com', 'pw12345')
    profile, _ = Profile.objects.get_or_create(user=user)
    profile.wallet_balance = Decimal(balance)
    profile.save()
    return DeveloperAccount.objects.create(user=user, company_name=f'{name} Ltd')


def make_app(dev, env='live', status='active', **kw):
    app = ApiApplication(developer=dev, name=f'{dev.user.username}-{env}', environment=env, status=status, **kw)
    secret = app.issue_credentials(new_public_key=True)
    app.save()
    return app, secret


def headers(app, secret, key=None):
    h = {'HTTP_X_API_KEY': app.public_key, 'HTTP_AUTHORIZATION': f'Bearer {secret}'}
    if key:
        h['HTTP_IDEMPOTENCY_KEY'] = key
    return h


def wallet(dev):
    return Profile.objects.get(user=dev.user).wallet_balance


@override_settings(ALLOWED_HOSTS=['testserver'], DEVELOPER_API_ASYNC_WEBHOOKS=False, PASSWORD_HASHERS=FAST_HASH)
class ApiTestBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.dev = make_developer()
        self.app, self.secret = make_app(self.dev)
        self.plan = DataPlan.objects.create(network='01', plan_name='MTN 1GB SME - 7 days', dataplan_id='1000', cost_price=Decimal('450'), price=Decimal('400'))
        self.cable = CablePlan.objects.create(cable_type='gotv', name='Jolli', plan_id='gotv-jolli', cost_price=Decimal('2400'), price=Decimal('2500'))
        ck_patch = mock.patch('developer_api.services.ClubKonnectService')
        self.ck = ck_patch.start().return_value
        self.addCleanup(ck_patch.stop)
        self.ck.buy_data.return_value = OK_RESPONSE
        self.ck.buy_airtime.return_value = OK_RESPONSE
        self.ck.buy_cable.return_value = OK_RESPONSE
        self.ck.pay_electricity.return_value = ({'status': 'ORDER_RECEIVED', 'metertoken': '1111-2222'}, 'rid')

    def get(self, path, app=None, secret=None, **extra):
        app = app or self.app
        return self.client.get(BASE + path, **headers(app, secret or self.secret), **extra)

    def post(self, path, body, key='key-00000001', app=None, secret=None):
        app = app or self.app
        return self.client.post(BASE + path, data=json.dumps(body), content_type='application/json', **headers(app, secret or self.secret, key))

    def buy_data(self, key='key-00000001', phone='08012345678', **kw):
        return self.post('/data/', {'plan_id': self.plan.pk, 'phone': phone}, key=key, **kw)


class AuthenticationTests(ApiTestBase):
    def test_missing_credentials(self):
        r = self.client.get(BASE + '/balance/')
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()['error']['code'], 'missing_credentials')

    def test_wrong_secret_is_rejected_and_logged(self):
        r = self.get('/balance/', secret='sk_live_wrong')
        self.assertEqual((r.status_code, r.json()['error']['code']), (401, 'invalid_credentials'))
        self.assertTrue(ApiRequestLog.objects.filter(error_code='invalid_credentials').exists())

    def test_secret_is_stored_hashed(self):
        self.assertNotIn(self.secret, self.app.secret_hash)
        self.assertEqual(len(self.app.secret_hash), 64)
        self.assertTrue(self.app.check_secret(self.secret))

    def test_valid_credentials(self):
        r = self.get('/balance/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['data']['balance'], '5000.00')
        self.assertIn('request_id', r.json())

    def test_pending_live_app_blocked(self):
        app, secret = make_app(self.dev, status='pending')
        r = self.get('/balance/', app=app, secret=secret)
        self.assertEqual((r.status_code, r.json()['error']['code']), (403, 'application_pending'))

    def test_suspended_app_and_suspended_developer_blocked(self):
        self.app.status = 'suspended'
        self.app.save()
        self.assertEqual(self.get('/balance/').json()['error']['code'], 'application_suspended')
        self.app.status = 'active'
        self.app.save()
        self.dev.status = 'suspended'
        self.dev.save()
        self.assertEqual(self.get('/balance/').status_code, 403)

    def test_revoked_credentials_stop_working(self):
        self.app.revoke()
        self.app.save()
        self.assertEqual(self.get('/balance/').status_code, 401)

    def test_regenerated_secret_invalidates_old_one(self):
        new_secret = self.app.issue_credentials()
        self.app.save()
        self.assertEqual(self.get('/balance/').status_code, 401)
        self.assertEqual(self.get('/balance/', secret=new_secret).status_code, 200)

    def test_ip_whitelist(self):
        self.app.ip_whitelist = '203.0.113.0/24\n198.51.100.7'
        self.app.save()
        self.assertEqual(self.get('/balance/', REMOTE_ADDR='192.0.2.5').json()['error']['code'], 'ip_not_allowed')
        self.assertEqual(self.get('/balance/', REMOTE_ADDR='203.0.113.50').status_code, 200)

    def test_rate_limit(self):
        self.app.rate_limit_per_minute = 3
        self.app.save()
        codes = [self.get('/balance/').status_code for _ in range(5)]
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertEqual(codes[3], 429)

    def test_service_permission(self):
        self.app.allowed_services = ['airtime']
        self.app.save()
        r = self.buy_data()
        self.assertEqual((r.status_code, r.json()['error']['code']), (403, 'service_not_permitted'))
        self.assertEqual(wallet(self.dev), Decimal('5000.00'))

    def test_repeated_bad_credentials_lock_out_ip(self):
        for _ in range(20):
            self.get('/balance/', secret='sk_live_bad', REMOTE_ADDR='192.0.2.9')
        r = self.get('/balance/', REMOTE_ADDR='192.0.2.9')  # even the valid secret is refused from that IP
        self.assertEqual(r.status_code, 429)

    def test_errors_never_leak_internals(self):
        with mock.patch('developer_api.views.Profile.objects.get', side_effect=RuntimeError('db password=hunter2')):
            r = self.get('/balance/')
        self.assertEqual(r.status_code, 500)
        self.assertNotIn('hunter2', r.content.decode())
        self.assertEqual(r.json()['error']['code'], 'internal_error')


class CatalogTests(ApiTestBase):
    def test_plans_and_networks(self):
        r = self.get('/data-plans/?network=MTN').json()['data']['plans']
        self.assertEqual([p['plan_id'] for p in r], [self.plan.pk])
        self.assertEqual(r[0]['price'], '400.00')
        self.assertNotIn('cost_price', json.dumps(r))
        ServiceSwitch.objects.create(network='Glo', is_active=False)
        nets = {n['name']: n['available'] for n in self.get('/networks/').json()['data']['networks']}
        self.assertFalse(nets['Glo'])
        self.assertTrue(nets['MTN'])

    def test_cable_plans(self):
        r = self.get('/cable-plans/?cable_tv=gotv').json()['data']['plans']
        self.assertEqual(r[0]['package_id'], 'gotv-jolli')


class PurchaseTests(ApiTestBase):
    def test_idempotency_key_required(self):
        r = self.post('/data/', {'plan_id': self.plan.pk, 'phone': '08012345678'}, key=None)
        self.assertEqual((r.status_code, r.json()['error']['code']), (400, 'idempotency_key_required'))

    def test_successful_data_purchase_debits_once_and_records_profit(self):
        r = self.buy_data()
        self.assertEqual(r.status_code, 201, r.content)
        data = r.json()['data']
        self.assertEqual((data['status'], data['amount']), ('successful', '400.00'))
        self.assertEqual(wallet(self.dev), Decimal('4600.00'))
        core = CoreTx.objects.get(reference=data['reference'])
        self.assertEqual((core.status, core.amount_customer_paid, core.cost_from_klubconnect), ('Successful', Decimal('400.00'), Decimal('450.00')))
        self.assertEqual(core.net_profit, Decimal('-50.00'))  # selling below cost is allowed
        self.ck.buy_data.assert_called_once()
        self.assertEqual(self.ck.buy_data.call_args.args[:3], ('01', '1000', '08012345678'))

    def test_client_cannot_override_price(self):
        r = self.post('/data/', {'plan_id': self.plan.pk, 'phone': '08012345678', 'amount': '1', 'price': 1}, key='key-price-0001')
        self.assertEqual(r.json()['data']['amount'], '400.00')
        self.assertEqual(wallet(self.dev), Decimal('4600.00'))

    def test_idempotent_retry_charges_once_and_calls_provider_once(self):
        first = self.buy_data(key='same-key-001')
        second = self.buy_data(key='same-key-001')
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()['idempotent_replay'])
        self.assertEqual(first.json()['data']['reference'], second.json()['data']['reference'])
        self.assertEqual(wallet(self.dev), Decimal('4600.00'))
        self.assertEqual(self.ck.buy_data.call_count, 1)
        self.assertEqual(ApiTransaction.objects.count(), 1)

    def test_same_key_different_body_rejected(self):
        self.buy_data(key='same-key-002')
        r = self.buy_data(key='same-key-002', phone='08099999991')
        self.assertEqual((r.status_code, r.json()['error']['code']), (422, 'idempotency_key_reused'))
        self.assertEqual(self.ck.buy_data.call_count, 1)

    def test_request_still_processing_returns_409(self):
        order = services.build_order('data', {'plan_id': self.plan.pk, 'phone': '08012345678'})
        ApiTransaction.objects.create(application=self.app, reference='BTD-X1', idempotency_key='busy-key-001', request_fingerprint=order.fingerprint,
                                      service='data', description='d', recipient='08012345678', amount=Decimal('400'), status='processing')
        r = self.buy_data(key='busy-key-001')
        self.assertEqual((r.status_code, r.json()['error']['code']), (409, 'request_in_progress'))
        self.assertEqual(wallet(self.dev), Decimal('5000.00'))
        self.ck.buy_data.assert_not_called()

    def test_insufficient_balance(self):
        Profile.objects.filter(user=self.dev.user).update(wallet_balance=Decimal('100'))
        r = self.buy_data()
        self.assertEqual((r.status_code, r.json()['error']['code']), (402, 'insufficient_funds'))
        self.assertEqual(wallet(self.dev), Decimal('100.00'))
        self.assertEqual(ApiTransaction.objects.count(), 0)
        self.ck.buy_data.assert_not_called()
        # after funding, the very same key works
        Profile.objects.filter(user=self.dev.user).update(wallet_balance=Decimal('1000'))
        self.assertEqual(self.buy_data().status_code, 201)

    def test_wallet_cannot_be_overspent_across_requests(self):
        Profile.objects.filter(user=self.dev.user).update(wallet_balance=Decimal('500'))
        codes = [self.buy_data(key=f'spend-key-{i:03d}').status_code for i in range(3)]
        self.assertEqual(codes, [201, 402, 402])
        self.assertEqual(wallet(self.dev), Decimal('100.00'))

    def test_provider_rejection_refunds_exactly_once(self):
        self.ck.buy_data.return_value = ({'status': 'INVALID_DATAPLAN'}, 'rid')
        r = self.buy_data()
        self.assertEqual((r.status_code, r.json()['error']['code']), (422, 'purchase_failed'))
        self.assertEqual(r.json()['data']['status'], 'failed')
        self.assertEqual(wallet(self.dev), Decimal('5000.00'))
        self.assertNotIn('INVALID_DATAPLAN', r.content.decode())  # provider detail is not exposed
        tx = ApiTransaction.objects.get()
        services.settle(tx.pk, 'failed')                      # late duplicate settle
        services.settle(tx.pk, 'failed')
        self.assertEqual(wallet(self.dev), Decimal('5000.00'))
        self.assertEqual(WalletTransaction.objects.filter(reference__startswith='REF-').count(), 1)
        # retrying the same key replays the failure and never calls the provider again
        again = self.buy_data()
        self.assertEqual(again.json()['data']['status'], 'failed')
        self.assertEqual(self.ck.buy_data.call_count, 1)

    def test_invalid_dataplan_disables_plan_and_hides_it(self):
        self.ck.buy_data.return_value = ({'status': 'INVALID_DATAPLAN'}, 'rid')
        self.buy_data()
        self.plan.refresh_from_db()
        self.assertFalse(self.plan.is_active)
        listed = [p['plan_id'] for p in self.get('/data-plans/').json()['data']['plans']]
        self.assertNotIn(self.plan.pk, listed)
        r = self.buy_data(key='key-00000002')  # a disabled plan is refused before any debit or provider call
        self.assertEqual((r.status_code, r.json()['error']['code']), (422, 'validation_error'))
        self.assertEqual(wallet(self.dev), Decimal('5000.00'))
        self.assertEqual(self.ck.buy_data.call_count, 1)

    def test_other_provider_failures_keep_plan_active(self):
        self.ck.buy_data.return_value = ({'status': 'INSUFFICIENT_APIBALANCE'}, 'rid')
        self.buy_data()
        self.plan.refresh_from_db()
        self.assertTrue(self.plan.is_active)

    def test_provider_timeout_is_pending_and_not_refunded(self):
        self.ck.buy_data.return_value = ({'status': 'ERROR', 'remark': 'Connection Timeout'}, 'rid')
        r = self.buy_data()
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()['data']['status'], 'pending')
        self.assertEqual(wallet(self.dev), Decimal('4600.00'))
        self.assertEqual(WalletTransaction.objects.filter(reference__startswith='REF-').count(), 0)

    def test_provider_exception_is_pending_not_refunded(self):
        self.ck.buy_data.side_effect = RuntimeError('boom')
        r = self.buy_data()
        self.assertEqual(r.json()['data']['status'], 'pending')
        self.assertEqual(wallet(self.dev), Decimal('4600.00'))

    def test_reconcile_pending_to_success(self):
        self.ck.buy_data.return_value = ({'status': 'ERROR'}, 'rid')
        ref = self.buy_data().json()['data']['reference']
        self.ck.query_order.return_value = {'status': 'ORDER_COMPLETED'}
        services.reconcile(ApiTransaction.objects.get(reference=ref))
        self.assertEqual(ApiTransaction.objects.get(reference=ref).status, 'successful')
        self.assertEqual(wallet(self.dev), Decimal('4600.00'))
        self.assertEqual(CoreTx.objects.get(reference=ref).status, 'Successful')

    def test_reconcile_cancelled_refunds_once_and_unknown_does_nothing(self):
        self.ck.buy_data.return_value = ({'status': 'ERROR'}, 'rid')
        ref = self.buy_data().json()['data']['reference']
        self.ck.query_order.return_value = {'status': 'ORDER_ONHOLD'}
        services.reconcile(ApiTransaction.objects.get(reference=ref))
        self.assertEqual(ApiTransaction.objects.get(reference=ref).status, 'pending')
        self.ck.query_order.return_value = {'status': 'ORDER_CANCELLED'}
        for _ in range(2):
            services.reconcile(ApiTransaction.objects.get(reference=ref))
        self.assertEqual(ApiTransaction.objects.get(reference=ref).status, 'failed')
        self.assertEqual(wallet(self.dev), Decimal('5000.00'))

    def test_late_success_cannot_override_refunded_failure(self):
        self.ck.buy_data.return_value = ({'status': 'INVALID_DATAPLAN'}, 'rid')
        self.buy_data()
        tx = ApiTransaction.objects.get()
        services.settle(tx.pk, 'successful', {'status': 'ORDER_RECEIVED'})
        self.assertEqual(ApiTransaction.objects.get().status, 'failed')

    def test_airtime_pricing_and_validation(self):
        r = self.post('/airtime/', {'network': 'mtn', 'phone': '08012345678', 'amount': 1000}, key='air-key-0001')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()['data']['amount'], '990.00')
        self.assertEqual(self.ck.buy_airtime.call_args.args[:3], ('01', Decimal('1000'), '08012345678'))
        bad = self.post('/airtime/', {'network': 'MTN', 'phone': '123', 'amount': 10}, key='air-key-0002')
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(set(bad.json()['error']['details']), {'phone', 'amount'})

    def test_electricity_returns_token_and_adds_fee(self):
        r = self.post('/electricity/', {'disco': '04', 'meter_no': '45012345678', 'meter_type': 'prepaid', 'amount': 2000, 'phone': '08012345678'}, key='ele-key-0001')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()['data']['amount'], '2100.00')
        self.assertEqual(r.json()['data']['result'], {'token': '1111-2222'})
        self.assertEqual(wallet(self.dev), Decimal('2900.00'))

    def test_cable_purchase(self):
        r = self.post('/cable/', {'cable_tv': 'gotv', 'package_id': 'gotv-jolli', 'smartcard': '7012345678', 'phone': '08012345678'}, key='cab-key-0001')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(wallet(self.dev), Decimal('2500.00'))

    def test_disabled_network_blocks_purchase(self):
        ServiceSwitch.objects.create(network='MTN', is_active=False)
        r = self.buy_data()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(wallet(self.dev), Decimal('5000.00'))

    def test_invalid_plan(self):
        r = self.post('/data/', {'plan_id': 99999, 'phone': '08012345678'}, key='bad-plan-001')
        self.assertEqual(r.status_code, 422)

    def test_malformed_and_non_json_bodies(self):
        r = self.client.post(BASE + '/data/', data='{oops', content_type='application/json', **headers(self.app, self.secret, 'json-key-001'))
        self.assertEqual((r.status_code, r.json()['error']['code']), (400, 'invalid_json'))
        r = self.client.post(BASE + '/data/', data='a=b', content_type='application/x-www-form-urlencoded', **headers(self.app, self.secret, 'json-key-002'))
        self.assertEqual(r.status_code, 415)


class SandboxTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.test_app, self.test_secret = make_app(self.dev, env='test')

    def buy(self, phone, key):
        return self.post('/data/', {'plan_id': self.plan.pk, 'phone': phone}, key=key, app=self.test_app, secret=self.test_secret)

    def test_sandbox_never_touches_wallet_or_provider(self):
        r = self.buy('08012345678', 'sbx-key-0001')
        self.assertEqual(r.json()['data']['status'], 'successful')
        self.assertEqual(r.json()['data']['environment'], 'test')
        self.assertEqual(wallet(self.dev), Decimal('5000.00'))
        self.ck.buy_data.assert_not_called()
        self.assertFalse(CoreTx.objects.exists())

    def test_sandbox_magic_numbers(self):
        self.assertEqual(self.buy('08012345678', 'sbx-key-0002').status_code, 201)
        self.assertEqual(self.buy('08010000000'[:7] + '0000', 'sbx-key-0003').status_code, 422)
        self.assertEqual(self.buy('08010009999', 'sbx-key-0004').json()['data']['status'], 'pending')

    def test_test_and_live_transactions_are_separate(self):
        self.buy('08012345678', 'sbx-key-0005')
        self.buy_data(key='live-key-0001')
        test_list = self.get('/transactions/', app=self.test_app, secret=self.test_secret).json()['data']
        live_list = self.get('/transactions/').json()['data']
        self.assertEqual((test_list['total'], live_list['total']), (1, 1))
        ref = test_list['transactions'][0]['reference']
        self.assertEqual(self.get(f'/transactions/{ref}/').status_code, 404)  # live key cannot see sandbox order


class IsolationTests(ApiTestBase):
    def test_developer_cannot_read_another_developers_transactions(self):
        ref = self.buy_data().json()['data']['reference']
        other = make_developer('dev2')
        other_app, other_secret = make_app(other)
        self.assertEqual(self.get(f'/transactions/{ref}/', app=other_app, secret=other_secret).status_code, 404)
        self.assertEqual(self.get('/transactions/', app=other_app, secret=other_secret).json()['data']['total'], 0)
        self.assertEqual(self.get(f'/transactions/{ref}/').status_code, 200)

    def test_same_idempotency_key_is_scoped_per_application(self):
        other = make_developer('dev3', '5000')
        other_app, other_secret = make_app(other)
        self.buy_data(key='shared-key-01')
        r = self.buy_data(key='shared-key-01', app=other_app, secret=other_secret)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(wallet(other), Decimal('4600.00'))

    def test_transaction_filters(self):
        self.post('/data/', {'plan_id': self.plan.pk, 'phone': '08012345678', 'client_reference': 'order-7'}, key='filter-key-01')
        listed = self.get('/transactions/?client_reference=order-7').json()['data']
        self.assertEqual(listed['total'], 1)
        self.assertEqual(self.get('/transactions/?status=failed').json()['data']['total'], 0)


@override_settings(ALLOWED_HOSTS=['testserver'], DEVELOPER_API_ASYNC_WEBHOOKS=False, PASSWORD_HASHERS=FAST_HASH)
class WebhookTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.app.webhook_url = 'https://hooks.example.com/bt'
        self.app.save()

    def test_event_queued_with_valid_signature(self):
        self.buy_data()
        d = WebhookDelivery.objects.get()
        self.assertEqual(d.event_type, 'transaction.successful')
        self.assertTrue(d.event_id.startswith('evt_'))
        with mock.patch('developer_api.webhooks.requests.post') as post, mock.patch('developer_api.webhooks.validate_webhook_url'):
            post.return_value.status_code = 200
            webhooks.deliver(d)
        kwargs = post.call_args.kwargs
        sig = kwargs['headers'][webhooks.SIGNATURE_HEADER]
        parts = dict(p.split('=') for p in sig.split(','))
        expected = webhooks.sign(self.app.webhook_signing_secret, parts['t'], kwargs['data'])
        self.assertEqual(parts['v1'], expected)
        self.assertEqual(json.loads(kwargs['data'])['data']['status'], 'successful')
        self.assertFalse(kwargs['allow_redirects'])
        d.refresh_from_db()
        self.assertEqual((d.status, d.attempts), ('delivered', 1))

    def test_signature_differs_per_secret_version(self):
        old = self.app.webhook_signing_secret
        self.app.webhook_secret_version += 1
        self.assertNotEqual(old, self.app.webhook_signing_secret)

    def test_retry_backoff_then_exhausted_and_delivered_not_resent(self):
        self.buy_data()
        d = WebhookDelivery.objects.get()
        with mock.patch('developer_api.webhooks.requests.post') as post, mock.patch('developer_api.webhooks.validate_webhook_url'):
            post.return_value.status_code = 500
            webhooks.deliver(d)
            d.refresh_from_db()
            self.assertEqual((d.status, d.attempts), ('failed', 1))
            self.assertGreater(d.next_attempt_at, timezone.now())
            for _ in range(len(webhooks.RETRY_DELAYS)):
                webhooks.deliver(d)
            self.assertEqual(d.status, 'exhausted')
            calls = post.call_count
            webhooks.deliver(d)
            self.assertEqual(post.call_count, calls)  # gave up: no more attempts

    def test_webhook_failure_never_affects_purchase(self):
        with mock.patch('developer_api.webhooks.requests.post', side_effect=ConnectionError('down')):
            r = self.buy_data()
        self.assertEqual(r.status_code, 201)
        self.assertEqual(wallet(self.dev), Decimal('4600.00'))

    def test_failed_and_pending_events(self):
        self.ck.buy_data.return_value = ({'status': 'INVALID_DATAPLAN'}, 'r')
        self.buy_data(key='wh-key-00001')
        self.plan.refresh_from_db()
        self.assertFalse(self.plan.is_active)  # INVALID_DATAPLAN switches the plan off
        self.plan.is_active = True
        self.plan.save()
        self.ck.buy_data.return_value = ({'status': 'ERROR'}, 'r')
        self.buy_data(key='wh-key-00002')
        types = sorted(WebhookDelivery.objects.values_list('event_type', flat=True))
        self.assertEqual(types, ['transaction.failed', 'transaction.pending'])

    def test_no_url_no_event(self):
        self.app.webhook_url = ''
        self.app.save()
        self.buy_data()
        self.assertFalse(WebhookDelivery.objects.exists())

    def test_private_and_http_urls_rejected(self):
        from .security import validate_webhook_url
        for bad in ['http://example.com/x', 'https://127.0.0.1/x', 'https://localhost/x', 'https://10.0.0.5/x', 'https://169.254.169.254/latest']:
            with self.assertRaises(ValueError, msg=bad):
                validate_webhook_url(bad)


@override_settings(ALLOWED_HOSTS=['testserver'], DEVELOPER_API_ASYNC_WEBHOOKS=False, PASSWORD_HASHERS=FAST_HASH)
class PortalTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('portal', 'p@example.com', 'pw12345')
        Profile.objects.get_or_create(user=self.user)
        self.client.login(username='portal', password='pw12345')

    def enable(self):
        return self.client.post('/developers/console/enable/', {'company_name': 'Acme', 'accept_terms': 'on'})

    def test_docs_and_spec_are_public(self):
        self.client.logout()
        self.assertEqual(self.client.get('/developers/').status_code, 200)
        self.assertEqual(self.client.get('/developers/reference/').status_code, 200)
        self.assertEqual(self.client.get('/developers/openapi.json').json()['openapi'], '3.0.3')

    def test_console_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get('/developers/console/').status_code, 302)

    def test_create_app_shows_secret_once_and_stores_hash(self):
        self.enable()
        r = self.client.post('/developers/console/apps/new/', {'name': 'Shop', 'environment': 'test'}, follow=True)
        app = ApiApplication.objects.get()
        self.assertEqual(app.status, 'active')
        body = r.content.decode()
        secret = [w for w in body.replace('<', ' ').replace('>', ' ').split() if w.startswith('sk_test_')][0]
        self.assertTrue(app.check_secret(secret))
        self.assertNotIn(secret, app.secret_hash)
        again = self.client.get(f'/developers/console/apps/{app.pk}/').content.decode()
        self.assertNotIn(secret, again)

    def test_live_app_starts_pending(self):
        self.enable()
        self.client.post('/developers/console/apps/new/', {'name': 'Prod', 'environment': 'live'})
        self.assertEqual(ApiApplication.objects.get().status, 'pending')

    def test_cannot_open_or_modify_another_developers_app(self):
        other = make_developer('victim')
        app, _ = make_app(other)
        self.enable()
        self.assertEqual(self.client.get(f'/developers/console/apps/{app.pk}/').status_code, 404)
        self.client.post(f'/developers/console/apps/{app.pk}/action/', {'action': 'revoke'})
        app.refresh_from_db()
        self.assertEqual(app.status, 'active')

    def test_rotate_and_revoke(self):
        self.enable()
        self.client.post('/developers/console/apps/new/', {'name': 'Shop', 'environment': 'test'})
        app = ApiApplication.objects.get()
        old = app.secret_hash
        self.client.post(f'/developers/console/apps/{app.pk}/action/', {'action': 'rotate_secret'})
        app.refresh_from_db()
        self.assertNotEqual(old, app.secret_hash)
        self.client.post(f'/developers/console/apps/{app.pk}/action/', {'action': 'revoke'})
        app.refresh_from_db()
        self.assertEqual((app.status, app.secret_hash), ('revoked', ''))

    def test_settings_validation(self):
        self.enable()
        self.client.post('/developers/console/apps/new/', {'name': 'Shop', 'environment': 'test'})
        app = ApiApplication.objects.get()
        url = f'/developers/console/apps/{app.pk}/action/'
        self.client.post(url, {'action': 'settings', 'webhook_url': 'http://insecure.example.com', 'svc_data': 'on'})
        app.refresh_from_db()
        self.assertEqual(app.webhook_url, '')
        self.client.post(url, {'action': 'settings', 'webhook_url': '', 'ip_whitelist': 'not-an-ip', 'svc_data': 'on'})
        app.refresh_from_db()
        self.assertEqual(app.ip_whitelist, '')
        self.client.post(url, {'action': 'settings', 'webhook_url': '', 'ip_whitelist': '203.0.113.5', 'svc_data': 'on', 'svc_airtime': 'on'})
        app.refresh_from_db()
        self.assertEqual((app.ip_whitelist, app.allowed_services), ('203.0.113.5', ['airtime', 'data']))


class DocsSyncTests(TestCase):
    def test_every_route_is_documented_and_vice_versa(self):
        from . import api_urls
        routes = {}
        for p in api_urls.urlpatterns:
            path = '/' + str(p.pattern).replace('<str:reference>', '{reference}')
            routes[path] = p.name
        spec = build_spec('x')['paths']
        self.assertEqual(set(routes), set(spec))
        for path, item in spec.items():
            for method in item:
                self.assertIn(method, ('get', 'post'))
        from .openapi import PATHS
        self.assertEqual({PATHS[p][0] for p in PATHS}, set(routes.values()))


class ReconcileCommandTests(ApiTestBase):
    def test_stuck_processing_with_debit_becomes_pending_not_refunded(self):
        from django.core.management import call_command
        ref = self.buy_data().json()['data']['reference']
        ApiTransaction.objects.filter(reference=ref).update(status='processing', updated_at=timezone.now() - timedelta(minutes=10))
        self.ck.query_order.return_value = {'status': 'WHO_KNOWS'}
        call_command('reconcile_api_transactions')
        self.assertEqual(ApiTransaction.objects.get(reference=ref).status, 'pending')
        self.assertEqual(wallet(self.dev), Decimal('4600.00'))
