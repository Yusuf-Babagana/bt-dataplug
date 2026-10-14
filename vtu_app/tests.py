from decimal import Decimal

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from .models import Profile, Transaction, WalletTransaction


def make_user(name, balance='0.00', email=None, pin='1234'):
    user = User.objects.create_user(name, email or f'{name}@example.com', 'OldPass1')
    profile, _ = Profile.objects.get_or_create(user=user)
    profile.wallet_balance = Decimal(balance)
    profile.save()
    profile.set_pin(pin)
    return user


@override_settings(ALLOWED_HOSTS=['testserver'])
class WalletTransferTests(TestCase):
    def setUp(self):
        cache.clear()
        self.alice = make_user('alice', '1000.00')
        self.bob = make_user('bob', '0.00')
        self.client.login(username='alice', password='OldPass1')

    def balance(self, user):
        return Profile.objects.get(user=user).wallet_balance

    def test_successful_transfer_moves_money_and_logs_both_sides(self):
        self.client.post('/transfer/', {'recipient': 'Bob', 'amount': '300', 'pin': '1234'})
        self.assertEqual(self.balance(self.alice), Decimal('700.00'))
        self.assertEqual(self.balance(self.bob), Decimal('300.00'))
        self.assertEqual(WalletTransaction.objects.filter(transaction_type='DEBIT', user=self.alice).count(), 1)
        self.assertEqual(WalletTransaction.objects.filter(transaction_type='CREDIT', user=self.bob).count(), 1)
        self.assertEqual(Transaction.objects.filter(user=self.alice, service_type='Wallet Transfer').count(), 1)

    def test_wrong_pin_moves_nothing(self):
        self.client.post('/transfer/', {'recipient': 'bob', 'amount': '300', 'pin': '9999'})
        self.assertEqual(self.balance(self.alice), Decimal('1000.00'))
        self.assertEqual(self.balance(self.bob), Decimal('0.00'))

    def test_insufficient_balance_moves_nothing(self):
        self.client.post('/transfer/', {'recipient': 'bob', 'amount': '5000', 'pin': '1234'})
        self.assertEqual(self.balance(self.alice), Decimal('1000.00'))
        self.assertEqual(self.balance(self.bob), Decimal('0.00'))

    def test_cannot_send_to_self_or_unknown_or_below_minimum(self):
        self.client.post('/transfer/', {'recipient': 'alice', 'amount': '100', 'pin': '1234'})
        self.client.post('/transfer/', {'recipient': 'nobody', 'amount': '100', 'pin': '1234'})
        self.client.post('/transfer/', {'recipient': 'bob', 'amount': '10', 'pin': '1234'})
        self.client.post('/transfer/', {'recipient': 'bob', 'amount': '-500', 'pin': '1234'})
        self.assertEqual(self.balance(self.alice), Decimal('1000.00'))
        self.assertEqual(self.balance(self.bob), Decimal('0.00'))

    def test_pin_lockout_after_five_failures(self):
        for _ in range(5):
            self.client.post('/transfer/', {'recipient': 'bob', 'amount': '100', 'pin': '0000'})
        # Even the correct PIN is refused while locked.
        self.client.post('/transfer/', {'recipient': 'bob', 'amount': '100', 'pin': '1234'})
        self.assertEqual(self.balance(self.alice), Decimal('1000.00'))

    def test_login_required(self):
        self.client.logout()
        r = self.client.post('/transfer/', {'recipient': 'bob', 'amount': '100', 'pin': '1234'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.balance(self.alice), Decimal('1000.00'))


@override_settings(ALLOWED_HOSTS=['testserver'], EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class PasswordApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user('carol', email='carol@example.com')
        token, _ = Token.objects.get_or_create(user=self.user)
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_change_password_success_keeps_token_valid(self):
        r = self.api.post('/api/v1/change-password/', {'old_password': 'OldPass1', 'new_password': 'NewPass22'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('NewPass22'))
        self.assertEqual(self.api.get('/api/v1/dashboard/').status_code, 200)

    def test_change_password_rejects_wrong_old_short_same_and_missing(self):
        for body in [
            {'old_password': 'wrong', 'new_password': 'NewPass22'},
            {'old_password': 'OldPass1', 'new_password': '123'},
            {'old_password': 'OldPass1', 'new_password': 'OldPass1'},
            {'old_password': 'OldPass1'},
        ]:
            self.assertEqual(self.api.post('/api/v1/change-password/', body, format='json').status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('OldPass1'))

    def test_change_password_requires_auth(self):
        r = APIClient().post('/api/v1/change-password/', {'old_password': 'OldPass1', 'new_password': 'NewPass22'}, format='json')
        self.assertEqual(r.status_code, 401)

    def test_reset_sends_email_for_known_address_and_same_answer_for_unknown(self):
        anon = APIClient()
        known = anon.post('/api/v1/password-reset/', {'email': 'carol@example.com'}, format='json')
        unknown = anon.post('/api/v1/password-reset/', {'email': 'ghost@example.com'}, format='json')
        self.assertEqual(known.status_code, 200)
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(known.json(), unknown.json())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('/accounts/reset/', mail.outbox[0].body)

    def test_reset_is_rate_limited(self):
        anon = APIClient()
        codes = [anon.post('/api/v1/password-reset/', {'email': 'x@example.com'}, format='json').status_code for _ in range(7)]
        self.assertIn(429, codes)


@override_settings(ALLOWED_HOSTS=['testserver'])
class PageTests(TestCase):
    def setUp(self):
        self.user = make_user('dave', '500.00')
        self.client.login(username='dave', password='OldPass1')

    def test_new_pages_render(self):
        for path in ['/calculator/', '/transfer/', '/coming-soon/bulk-sms/', '/coming-soon/result-checker/', '/', '/transactions/']:
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_unknown_coming_soon_redirects_home(self):
        self.assertEqual(self.client.get('/coming-soon/nope/').status_code, 302)

    def test_password_reset_pages_render_for_anonymous(self):
        self.client.logout()
        for path in ['/accounts/password_reset/', '/accounts/password_reset/done/', '/accounts/reset/done/']:
            self.assertEqual(self.client.get(path).status_code, 200, path)
