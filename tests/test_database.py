import unittest
from unittest.mock import patch

from flask import Flask

import database


class DatabaseRequestScopeTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.teardown_appcontext(database.close_db)

    @patch.dict("os.environ", {}, clear=True)
    @patch("database.mysql.connector.connect")
    def test_requests_get_isolated_connections_and_cleanup(self, connect):
        connections = []

        def make_connection(**kwargs):
            connection = unittest.mock.MagicMock()
            connection.kwargs = kwargs
            connections.append(connection)
            return connection

        connect.side_effect = make_connection
        with self.app.app_context():
            first = database.get_request_db()
            self.assertIs(first, database.db._get_current_object())
            self.assertIs(first, database.get_request_db())
            self.assertIs(database.cursor._get_current_object(), first.cursor.return_value)
            self.assertEqual(first.cursor.call_args.kwargs, {"buffered": True})
        with self.app.app_context():
            second = database.get_request_db()
            self.assertIsNot(first, second)

        self.assertEqual(len(connections), 2)
        for connection in connections:
            connection.rollback.assert_called_once_with()
            connection.cursor.return_value.close.assert_called_once_with()
            connection.close.assert_called_once_with()
        connect.assert_called_with(
            host="127.0.0.1", port=3306, user="casino_admin", password="",
            database="monero_casino", connection_timeout=10,
        )

    @patch("database.mysql.connector.connect")
    def test_service_connection_is_fresh_even_inside_app_context(self, connect):
        connection = connect.return_value
        with self.app.app_context():
            self.assertIs(database.get_db_connection(), connection)
            self.assertIs(database.get_db_connection(), connection)
        self.assertEqual(connect.call_count, 2)
        connection.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
