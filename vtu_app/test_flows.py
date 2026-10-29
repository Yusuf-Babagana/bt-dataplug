"""
End-to-end tests of the three customer flows through the real gateway and the real Nellobytes adapter
(only the HTTP boundary is stubbed): mobile API (what the Play Store app calls) and the website.
"""
from decimal import Decimal
from unittest import mock

import requests
from django.contrib.auth.models import User
from django.test import override_settings
from rest_framework.test import APIClient

from vtu_app.gateway import engine
from vtu_app.models import CablePlan, DataPlan, Notification, Profile, ProviderState, PurchaseAttempt, Transaction
from vtu_app.testing import GatewayTestCase, SupplierStub

FAST_HASH = ['django.contrib.auth.hashers.MD5PasswordHasher']
OK = ({'status': 'ORDER_RECEIVED', 'orderid': '999'}, 'rid')


def make_user(name='mob', balance='20000', pin='1234'):
    user = User.objects.create_user(name, f'{name}@example.com', 'pw12345')
    profile, _ = Profile.objects.get_or_create(user=user)
    profile.wallet_balance = Decimal(balance)
    profile.save()
    profile.set_pin(pin)
    return User.objects.get(pk=user.pk)     # fresh instance: no stale cached profile


def wallet(user):
    return Profile.objects.get(user=user).wallet_balance


@override_settings(ALLOWED_HOSTS=['testserver'], PASSWORD_HASHERS=FAST_HASH)
class FlowBase(GatewayTestCase):
    nello = True
    fakes = False

    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.plan = DataPlan.objects.create(network='01', plan_name='MTN 1GB SME', dataplan_id='1000', cost_price=Decimal('450'),
                                            price=Decimal('500'), additional_fee=Decimal('5'))
        self.cable = CablePlan.objects.create(cable_type='gotv', name='Jolli', plan_id='gotv-jolli', cost_price=Decimal('2400'),
                                              price=Decimal('2500'))
        self.ck = SupplierStub()
        for method in ('buy_data', 'buy_airtime', 'buy_cable', 'pay_electricity'):
            getattr(self.ck, method).return_value = OK
        patch = mock.patch('vtu_app.gateway.adapters.nellobytes.requests.get', side_effect=self.ck)
        patch.start()
        self.addCleanup(patch.stop)

    def last_tx(self):
        return Transaction.objects.latest('id')

    def data_body(self, **kw):
        return {'plan_id': self.plan.pk, 'phone': '08030000000', 'pin': '1234', **kw}

    def supplier_says(self, status):
        self.ck.query_order.return_value = {'orderstatus': status, 'statuscode': '200' if status == 'ORDER_COMPLETED' else '100'}


class MobileDataTests(FlowBase):
    def test_accepted_order_keeps_the_response_shape_the_app_reads_and_stays_pending(self):
        r = self.api.post('/api/v1/buy-data/', self.data_body(), format='json')
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        for key in ('message', 'new_balance', 'transaction_id', 'plan', 'phone', 'amount_paid', 'order_id', 'reference'):
            self.assertIn(key, d)
        self.assertEqual((d['message'], d['plan'], d['phone'], d['new_balance']), ('Transaction Successful!', 'MTN 1GB SME', '08030000000', '19500.00'))
        tx = self.last_tx()
        self.assertEqual((tx.status, tx.provider, tx.order_id, tx.bt_service_charge), ('Pending', 'nellobytes', '999', Decimal('5.00')))
        self.assertEqual(self.ck.buy_data.call_args.args[:3], ('01', '1000', '08030000000'))
        self.assertEqual(d['reference'], tx.reference)

    def test_confirmation_finalises_profit_and_notifies_once(self):
        self.api.post('/api/v1/buy-data/', self.data_body(), format='json')
        tx = self.last_tx()
        self.supplier_says('ORDER_COMPLETED')
        self.assertEqual(engine.reconcile_transaction(tx), 'Successful')
        self.assertEqual(engine.reconcile_transaction(tx), 'Successful')
        tx.refresh_from_db()
        self.assertEqual(tx.net_profit, Decimal('50.00'))
        self.assertEqual(Notification.objects.filter(user=self.user, title='Data purchase successful').count(), 1)

    def test_timeout_is_not_treated_as_failure(self):
        """Regression: a timeout used to refund the customer even though the order may have been placed."""
        self.ck.buy_data.side_effect = requests.ReadTimeout()
        r = self.api.post('/api/v1/buy-data/', self.data_body(), format='json')
        self.assertEqual(r.status_code, 200)
        self.assertIn('being processed', r.json()['message'])
        tx = self.last_tx()
        self.assertEqual((tx.status, tx.refunded_at, wallet(self.user)), ('Pending', None, Decimal('19500.00')))
        self.assertEqual(tx.attempts.get().outcome, 'AMBIGUOUS')
        self.supplier_says('ORDER_CANCELLED')
        self.assertEqual(engine.reconcile_transaction(tx), 'Refunded')      # only a confirmed failure refunds
        self.assertEqual(wallet(self.user), Decimal('20000.00'))

    def test_confirmed_rejection_refunds_exactly_and_tells_the_customer_why(self):
        self.ck.buy_data.return_value = ({'status': 'INVALID_RECIPIENT'}, 'rid')
        r = self.api.post('/api/v1/buy-data/', self.data_body(), format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('INVALID_RECIPIENT', r.json()['message'])
        self.assertIn('refunded', r.json()['message'])
        tx = self.last_tx()
        self.assertEqual((tx.status, wallet(self.user)), ('Refunded', Decimal('20000.00')))
        self.assertTrue(Notification.objects.filter(user=self.user, title='Data purchase failed').exists())

    def test_supplier_outage_message_hides_supplier_detail(self):
        self.ck.buy_data.return_value = ({'status': 'MTN SME data service is currently unavailable, pls try again later'}, 'rid')
        r = self.api.post('/api/v1/buy-data/', self.data_body(), format='json')
        self.assertEqual(r.status_code, 502)
        self.assertNotIn('MTN SME', r.json()['message'])
        self.assertEqual(wallet(self.user), Decimal('20000.00'))

    def test_supplier_float_empty_is_a_maintenance_message_not_a_customer_problem(self):
        self.ck.buy_data.return_value = ({'status': 'INSUFFICIENT_APIBALANCE'}, 'rid')
        r = self.api.post('/api/v1/buy-data/', self.data_body(), format='json')
        self.assertEqual(r.status_code, 503)
        self.assertIn('maintenance', r.json()['message'])

    def test_invalid_dataplan_reply_hides_the_plan_from_everyone(self):
        self.ck.buy_data.return_value = ({'status': 'INVALID_DATAPLAN'}, 'rid')
        self.api.post('/api/v1/buy-data/', self.data_body(), format='json')
        self.plan.refresh_from_db()
        self.assertFalse(self.plan.is_active)
        self.assertEqual(len(self.api.get('/api/v1/data-plans/').json()), 0)
        r = self.api.post('/api/v1/buy-data/', self.data_body(), format='json')
        self.assertEqual(r.status_code, 400)

    def test_guards_move_no_money_and_never_reach_the_supplier(self):
        cases = [
            (self.data_body(pin='0000'), 400),
            (self.data_body(plan_id=99999), 404),
            ({'plan_id': self.plan.pk, 'phone': '0803'}, 400),
        ]
        for body, code in cases:
            self.assertEqual(self.api.post('/api/v1/buy-data/', body, format='json').status_code, code)
        poor = make_user('poor', '100')
        c = APIClient()
        c.force_authenticate(poor)
        r = c.post('/api/v1/buy-data/', self.data_body(), format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('Insufficient', r.json()['message'])
        self.ck.buy_data.assert_not_called()
        self.assertEqual((wallet(self.user), Transaction.objects.count()), (Decimal('20000.00'), 0))

    def test_nothing_is_debited_when_no_provider_can_serve(self):
        ProviderState.objects.update_or_create(name='nellobytes', defaults={'admin_enabled': False})
        r = self.api.post('/api/v1/buy-data/', self.data_body(), format='json')
        self.assertEqual(r.status_code, 503)
        self.assertEqual((wallet(self.user), Transaction.objects.count()), (Decimal('20000.00'), 0))

    def test_idempotency_key_prevents_a_double_purchase(self):
        h = {'HTTP_IDEMPOTENCY_KEY': 'client-key-0001'}
        r1 = self.api.post('/api/v1/buy-data/', self.data_body(), format='json', **h)
        r2 = self.api.post('/api/v1/buy-data/', self.data_body(), format='json', **h)
        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertTrue(r2.json()['idempotent_replay'])
        self.assertEqual(r2.json()['reference'], r1.json()['reference'])
        self.assertEqual(self.ck.buy_data.call_count, 1)
        self.assertEqual((wallet(self.user), Transaction.objects.count()), (Decimal('19500.00'), 1))

    def test_without_a_key_behaviour_is_unchanged(self):
        self.api.post('/api/v1/buy-data/', self.data_body(), format='json')
        self.api.post('/api/v1/buy-data/', self.data_body(), format='json')
        self.assertEqual(Transaction.objects.count(), 2)

    def test_history_shape_unchanged(self):
        self.api.post('/api/v1/buy-data/', self.data_body(), format='json')
        row = self.api.get('/api/v1/transactions/').json()[0]
        self.assertEqual(set(row), {'id', 'service', 'recipient', 'amount', 'status', 'date', 'time', 'ref'})
        self.assertEqual(row['status'], 'Pending')


class MobileAirtimeTests(FlowBase):
    def body(self, **kw):
        return {'network': 'MTN', 'amount': '1000', 'phone': '08030000000', 'pin': '1234', **kw}

    def test_customer_pays_99_percent_and_supplier_gets_the_face_amount(self):
        r = self.api.post('/api/v1/buy-airtime/', self.body(), format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()['message'], r.json()['amount_paid']), ('Airtime Sent!', '990.00'))
        self.assertEqual(self.ck.buy_airtime.call_args.args[:3], ('01', 1000.0, '08030000000'))
        tx = self.last_tx()
        self.assertEqual((tx.amount_customer_paid, tx.cost_from_klubconnect, wallet(self.user)), (Decimal('990.00'), Decimal('970.00'), Decimal('19010.00')))

    def test_rejection_refunds_exactly_what_was_paid(self):
        self.ck.buy_airtime.return_value = ({'status': 'INVALID_RECIPIENT'}, 'rid')
        r = self.api.post('/api/v1/buy-airtime/', self.body(), format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(wallet(self.user), Decimal('20000.00'))

    def test_validation(self):
        for body in (self.body(network='BOGUS'), self.body(amount='abc'), self.body(amount='-5'), self.body(pin='0000'),
                     self.body(amount='nan'), self.body(amount='inf')):
            self.assertEqual(self.api.post('/api/v1/buy-airtime/', body, format='json').status_code, 400, body)
        self.ck.buy_airtime.assert_not_called()


class MobileCableAndElectricityTests(FlowBase):
    def cable_body(self, **kw):
        return {'cable_tv': 'gotv', 'package_id': 'gotv-jolli', 'smart_card': '1234567890', 'phone': '08030000000', 'pin': '1234', **kw}

    def test_cable_purchase_and_scoping(self):
        r = self.api.post('/api/v1/buy-cable/', self.cable_body(), format='json')
        self.assertEqual((r.status_code, r.json()['message']), (200, 'Subscription Received Successfully'))
        self.assertEqual(self.ck.buy_cable.call_args.args[:4], ('gotv', 'gotv-jolli', '1234567890', '08030000000'))
        self.assertEqual(self.api.post('/api/v1/buy-cable/', self.cable_body(package_id='nope'), format='json').status_code, 400)
        self.assertEqual(self.api.post('/api/v1/buy-cable/', self.cable_body(cable_tv='dstv'), format='json').status_code, 400)

    def test_cable_plan_list_hides_inactive_plans(self):
        CablePlan.objects.create(cable_type='dstv', name='Old', plan_id='old', cost_price=1, price=1, is_active=False)
        names = [p['name'] for p in self.api.get('/api/v1/cable-plans/').json()]
        self.assertEqual(names, ['Jolli'])

    def elec(self, **kw):
        return {'disco': '04', 'meter_no': '45012345678', 'meter_type': '01', 'amount': '2000', 'phone': '08030000000', 'pin': '1234', **kw}

    def test_electricity_adds_the_fee_and_returns_a_token_only_when_the_supplier_gave_one(self):
        self.ck.pay_electricity.return_value = ({'status': 'ORDER_RECEIVED', 'orderid': '5', 'metertoken': '1111-2222'}, 'rid')
        r = self.api.post('/api/v1/pay-electricity/', self.elec(), format='json')
        self.assertEqual((r.status_code, r.json()['token']), (200, '1111-2222'))
        self.assertEqual(wallet(self.user), Decimal('17900.00'))         # 2000 + 100 fee
        tx = self.last_tx()
        self.assertEqual((tx.amount_customer_paid, tx.bt_service_charge), (Decimal('2100.00'), Decimal('100.00')))
        self.assertEqual(self.ck.pay_electricity.call_args.args[:3], ('04', '45012345678', '01'))
        self.ck.pay_electricity.return_value = OK
        r = self.api.post('/api/v1/pay-electricity/', self.elec(), format='json')
        self.assertNotIn('token', r.json())             # the app then shows "Sent via SMS"

    def test_electricity_validation_and_rejection_refund(self):
        self.assertEqual(self.api.post('/api/v1/pay-electricity/', self.elec(amount='500'), format='json').status_code, 400)
        self.assertEqual(self.api.post('/api/v1/pay-electricity/', self.elec(amount='nan'), format='json').status_code, 400)
        self.ck.pay_electricity.return_value = ({'status': 'INVALID_METERNO'}, 'rid')
        r = self.api.post('/api/v1/pay-electricity/', self.elec(), format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(wallet(self.user), Decimal('20000.00'))        # bill + fee both returned

    def test_validators_keep_their_contract(self):
        self.ck.verify.return_value = {'customer_name': 'JOHN DOE'}
        self.assertEqual(self.api.get('/api/v1/validate-meter/', {'disco': '04', 'meter_no': '1', 'meter_type': '01'}).json(), {'customer_name': 'JOHN DOE'})
        self.assertEqual(self.api.get('/api/v1/validate-cable/', {'cable_tv': 'gotv', 'smart_card': '1'}).json(), {'customer_name': 'JOHN DOE'})
        self.ck.verify.return_value = {'customer_name': 'INVALID_METERNO'}
        self.assertEqual(self.api.get('/api/v1/validate-meter/', {'disco': '04', 'meter_no': '1', 'meter_type': '01'}).json(), {'customer_name': 'INVALID_METERNO'})
        self.ck.verify.return_value = {'customer_name': 'INVALID_SMARTCARDNO'}
        self.assertEqual(self.api.get('/api/v1/validate-cable/', {'cable_tv': 'gotv', 'smart_card': '1'}).json(), {'customer_name': 'INVALID_SMARTCARDNO'})
        self.assertEqual(self.api.get('/api/v1/validate-cable/', {'cable_tv': 'gotv'}).status_code, 400)


class WebFlowTests(FlowBase):
    def setUp(self):
        super().setUp()
        self.client.login(username='mob', password='pw12345')

    def test_data_purchase_redirects_to_the_receipt_and_stays_pending(self):
        r = self.client.post('/buy-data/', {'plan': self.plan.pk, 'phone': '08030000000', 'pin': '1234'})
        tx = self.last_tx()
        self.assertRedirects(r, f'/receipt/{tx.id}/', fetch_redirect_response=False)
        self.assertEqual((tx.status, tx.provider, wallet(self.user)), ('Pending', 'nellobytes', Decimal('19500.00')))
        self.assertEqual(self.client.get(f'/receipt/{tx.id}/').status_code, 200)

    def test_data_rejection_refunds_and_returns_to_dashboard(self):
        self.ck.buy_data.return_value = ({'status': 'INVALID_RECIPIENT'}, 'rid')
        r = self.client.post('/buy-data/', {'plan': self.plan.pk, 'phone': '08030000000', 'pin': '1234'})
        self.assertRedirects(r, '/', fetch_redirect_response=False)
        self.assertEqual((self.last_tx().status, wallet(self.user)), ('Refunded', Decimal('20000.00')))

    def test_airtime_failure_refunds_what_was_paid_not_the_face_amount(self):
        """Regression: the website refunded the FACE amount (1000) although the customer paid 990."""
        self.ck.buy_airtime.return_value = ({'status': 'INVALID_RECIPIENT'}, 'rid')
        self.client.post('/buy-airtime/', {'network': '01', 'amount': '1000', 'phone': '08030000000', 'pin': '1234'})
        self.assertEqual((self.last_tx().status, wallet(self.user)), ('Refunded', Decimal('20000.00')))

    def test_airtime_success(self):
        r = self.client.post('/buy-airtime/', {'network': '01', 'amount': '1000', 'phone': '08030000000', 'pin': '1234'})
        tx = self.last_tx()
        self.assertRedirects(r, f'/receipt/{tx.id}/', fetch_redirect_response=False)
        self.assertEqual((tx.status, tx.amount_customer_paid, wallet(self.user)), ('Pending', Decimal('990.00'), Decimal('19010.00')))
        self.client.post('/buy-airtime/', {'network': '09', 'amount': '1000', 'phone': '0803', 'pin': '1234'})
        self.assertEqual(Transaction.objects.count(), 1)         # invalid network moved no money

    def test_cable_and_electricity(self):
        r = self.client.post('/buy-cable/', {'cable_tv': 'gotv', 'package': 'gotv-jolli', 'smart_card': '123456789', 'phone': '08030000000', 'pin': '1234'})
        self.assertRedirects(r, f'/receipt/{self.last_tx().id}/', fetch_redirect_response=False)
        r = self.client.post('/buy-electricity/', {'disco': '04', 'meter_no': '45012345678', 'meter_type': '01', 'amount': '2000', 'pin': '1234'})
        tx = self.last_tx()
        self.assertRedirects(r, f'/receipt/{tx.id}/', fetch_redirect_response=False)
        self.assertEqual((tx.amount_customer_paid, tx.bt_service_charge), (Decimal('2100.00'), Decimal('100.00')))
        self.ck.pay_electricity.return_value = ({'status': 'INVALID_METERNO'}, 'rid')
        before = wallet(self.user)
        self.client.post('/buy-electricity/', {'disco': '04', 'meter_no': '1', 'meter_type': '01', 'amount': '2000', 'pin': '1234'})
        self.assertEqual(wallet(self.user), before)              # debited then refunded in full

    def test_ajax_validators(self):
        self.ck.verify.return_value = {'customer_name': 'JANE'}
        self.assertEqual(self.client.get('/ajax/validate-meter/', {'disco': '04', 'meter': '1', 'type': '01'}).json(), {'customer_name': 'JANE'})
        self.assertEqual(self.client.get('/ajax/validate-cable/', {'cable_tv': 'gotv', 'smartcard': '1'}).json(), {'customer_name': 'JANE'})
        self.assertEqual(self.client.get('/ajax/validate-cable/', {'cable_tv': 'gotv'}).status_code, 400)

    def test_wrong_pin_and_no_provider(self):
        self.client.post('/buy-data/', {'plan': self.plan.pk, 'phone': '08030000000', 'pin': '9999'})
        ProviderState.objects.update_or_create(name='nellobytes', defaults={'admin_enabled': False})
        self.client.post('/buy-data/', {'plan': self.plan.pk, 'phone': '08030000000', 'pin': '1234'})
        self.assertEqual((Transaction.objects.count(), wallet(self.user)), (0, Decimal('20000.00')))
        self.ck.buy_data.assert_not_called()


class AdminTests(FlowBase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser('root', 'r@example.com', 'pw12345')
        self.client.login(username='root', password='pw12345')

    def test_pages_render_and_hide_secrets(self):
        r = self.client.get('/admin/vtu_app/providerstate/')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b'SUPER-SECRET-KEY-123', r.content)
        state = ProviderState.objects.get(name='nellobytes')
        r = self.client.get(f'/admin/vtu_app/providerstate/{state.pk}/change/')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b'SUPER-SECRET-KEY-123', r.content)
        self.assertIn(b'(set)', r.content)
        for path in ('purchaseattempt', 'transaction', 'dataplan', 'cableplan'):
            self.assertEqual(self.client.get(f'/admin/vtu_app/{path}/').status_code, 200, path)

    def test_transaction_page_shows_attempts_and_audit_trail(self):
        APIClient()
        self.api.post('/api/v1/buy-data/', self.data_body(), format='json')
        tx = self.last_tx()
        r = self.client.get(f'/admin/vtu_app/transaction/{tx.pk}/change/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'accepted by nellobytes')

    def test_connectivity_action_places_no_order(self):
        state = ProviderState.objects.get_or_create(name='nellobytes')[0]
        self.client.post('/admin/vtu_app/providerstate/', {'action': 'test_connectivity', '_selected_action': [state.pk]})
        self.assertEqual(self.ck.balance.call_count, 1)
        self.ck.buy_data.assert_not_called()
        self.assertEqual(Transaction.objects.count(), 0)

    def test_admin_refund_action_cannot_double_refund_an_auto_refunded_order(self):
        self.ck.buy_data.return_value = ({'status': 'INVALID_RECIPIENT'}, 'rid')
        self.api.post('/api/v1/buy-data/', self.data_body(), format='json')
        tx = self.last_tx()
        self.assertEqual(wallet(self.user), Decimal('20000.00'))
        self.client.post('/admin/vtu_app/transaction/', {'action': 'refund_to_wallet', '_selected_action': [tx.pk]})
        self.assertEqual(wallet(self.user), Decimal('20000.00'))

    def test_admin_check_with_provider_action_settles_pending_orders(self):
        self.api.post('/api/v1/buy-data/', self.data_body(), format='json')
        tx = self.last_tx()
        self.supplier_says('ORDER_COMPLETED')
        self.client.post('/admin/vtu_app/transaction/', {'action': 'check_with_provider', '_selected_action': [tx.pk]})
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'Successful')
