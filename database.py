"""Shared MySQL connection helpers for Flask requests and background workers."""

import os
import functools
import time

import mysql.connector
from flask import g, has_app_context
from werkzeug.local import LocalProxy


def _connect():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "casino_admin"),
        password=os.getenv("DB_PASSWORD", ""),
        database=os.getenv("DB_NAME", "monero_casino"),
        connection_timeout=10,
    )


def get_db_connection():
    """Return a fresh connection for short-lived service or worker work."""
    return _connect()


def retry_deadlocks(operation):
    """Retry only transactions the caller has rolled back and closed.

    InnoDB foreign-key locks can deadlock even with conditional balance debits.
    Never apply this decorator to operations that relay a wallet transaction.
    """
    @functools.wraps(operation)
    def wrapped(*args, **kwargs):
        for attempt in range(4):
            try:
                return operation(*args, **kwargs)
            except mysql.connector.Error as exc:
                if exc.errno not in (1213, 1205):
                    raise
                if attempt == 3:
                    raise ValueError('Account is busy. Please retry this request.') from exc
                time.sleep(0.025 * (attempt + 1))
    return wrapped


def get_request_db():
    """Return the connection scoped to the current Flask application context."""
    if not has_app_context():
        raise RuntimeError("get_request_db() requires a Flask application context")
    connection = g.get("_database_connection")
    if connection is None:
        connection = _connect()
        g._database_connection = connection
        g._database_cursor = connection.cursor(buffered=True)
    return connection


def _get_request_cursor():
    get_request_db()
    return g._database_cursor


db = LocalProxy(get_request_db)
cursor = LocalProxy(_get_request_cursor)


def close_db(exception=None):
    """Rollback unfinished request work and release its cursor and connection."""
    if not has_app_context():
        return
    cur = g.pop("_database_cursor", None)
    connection = g.pop("_database_connection", None)
    if connection is None:
        return
    try:
        connection.rollback()
    finally:
        try:
            if cur is not None:
                cur.close()
        finally:
            connection.close()
