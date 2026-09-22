"""HTTP boundary tests: retired sessions and money-route service integration."""
import importlib
import os
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

from flask import Flask


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {'FLASK_SECRET_KEY': 'test-only-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ',
                                     'FLASK_SESSION_COOKIE_SECURE': 'false'}):
            cls.module = importlib.import_module('app')
        cls.app = cls.module.app
        cls.app.config.update(TESTING=True)

    def authenticated_client(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = 7
        return client

    def test_retired_key_cannot_access_money_routes(self):
        attacker = Flask('attacker')
        attacker.secret_key = 'your_super_secret_key'
        forged = attacker.session_interface.get_signing_serializer(attacker).dumps({'user_id': 1})
        client = self.app.test_client()
        client.set_cookie('session', forged)
        with patch('mysql.connector.connect', side_effect=AssertionError('must not reach database')):
            for url in ('/withdraw', '/admin_withdrawal', '/admin'):
                response = client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.location.endswith('/login'))

    def test_money_post_uses_request_id_and_pending_status(self):
        conn = Mock()
        conn.cursor.return_value.fetchone.return_value = {'is_admin': 0}
        client = self.authenticated_client()
        with patch.object(self.module, 'get_db_connection', return_value=conn), \
                patch.object(self.module, 'reserve_withdrawal', return_value={'id': 5}) as reserve, \
                patch.object(self.module, 'process_withdrawal', return_value={'state': 'prepared'}):
            response = client.post('/withdraw', data={'withdraw_address': '4' * 95,
                                   'withdraw_amount': '0.5', 'request_id': 'stable-request'})
        self.assertEqual(response.status_code, 302)
        reserve.assert_called_once_with(7, 'user', '4' * 95, '0.5', 'stable-request')
        with client.session_transaction() as session:
            self.assertIn('reserved', session['_flashes'][0][1])

    def test_nonadmin_cannot_reserve_house_funds(self):
        conn = Mock()
        conn.cursor.return_value.fetchone.return_value = {'is_admin': 0}
        with patch.object(self.module, 'get_db_connection', return_value=conn), \
                patch.object(self.module, 'reserve_withdrawal') as reserve:
            response = self.authenticated_client().post('/admin_withdrawal')
        self.assertEqual(response.status_code, 403)
        reserve.assert_not_called()

    def test_withdrawal_templates_expose_stable_retry_without_metadata(self):
        # Anonymous rendering avoids the admin context processor database query.
        with self.app.test_request_context('/'):
            for template in ('withdraw.html', 'admin_withdrawal.html'):
                html = self.module.render_template(template, balance=Decimal('1'), request_id='new-request',
                    withdrawals=[{'request_id': 'existing-request', 'address': '4' * 95,
                                  'amount': Decimal('0.5'), 'state': 'prepared', 'tx_hash': 'a' * 64,
                                  'tx_metadata': 'never-display-this', 'created_at': '2026-09-22'}])
                self.assertIn('value="existing-request"', html)
                self.assertIn('value="new-request"', html)
                self.assertIn('Retry', html)
                self.assertNotIn('never-display-this', html)

    def test_invalid_bet_is_reported_without_spending(self):
        with patch.object(self.module, 'reserve_bet', side_effect=ValueError('Invalid amount.')) as place:
            client = self.authenticated_client()
            response = client.post('/place_bet', data={'game_type': 'digits', 'chosen_number': '05', 'bet_amount': 'NaN'})
        self.assertEqual(response.status_code, 302)
        place.assert_called_once_with(7, 'NaN', 'digits', '05')
        with client.session_transaction() as session:
            self.assertEqual(session['_flashes'][0], ('error', 'Invalid amount.'))
