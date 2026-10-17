"""Focused regression tests for the synthetic ATS drift endpoint."""
import os
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import app


class FakeScope:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def set_tag(self, *_):
        pass

    def set_context(self, *_):
        pass


class FakeSentry:
    def __init__(self):
        self.messages = []

    @contextmanager
    def push_scope(self):
        yield FakeScope()

    def capture_message(self, message, level):
        self.messages.append((message, level))

    def capture_exception(self, error):
        raise AssertionError("ATS drift should not capture an exception")


class AtsDriftTests(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self.db.close()
        self.old_db = app.DB
        app.DB = app.Path(self.db.name)
        self.sentry = FakeSentry()
        self.env = patch.dict(os.environ, {"DEMO_ADMIN_KEY": "test-key", "SENTRY_DSN": "test-dsn"})
        self.env.start()
        self.sentry_patch = patch.dict("sys.modules", {"sentry_sdk": self.sentry})
        self.sentry_patch.start()
        app.seed()

    def tearDown(self):
        self.sentry_patch.stop()
        self.env.stop()
        app.DB = self.old_db
        os.unlink(self.db.name)

    def test_repeated_invocation_only_counts_and_reports_real_transitions(self):
        before = app.WEBHOOK_ERRORS.labels("semantic_ack_mismatch")._value.get()

        first = app.trigger("ats-drift", "test-key")
        second = app.trigger("ats-drift", "test-key")

        self.assertEqual(first["changed_orders"], 16)
        self.assertFalse(first["no_op"])
        self.assertEqual(second["changed_orders"], 0)
        self.assertTrue(second["no_op"])
        self.assertEqual(app.WEBHOOK_ERRORS.labels("semantic_ack_mismatch")._value.get() - before, 16)
        self.assertEqual(self.sentry.messages, [("Synthetic ATS webhook semantic ACK mismatch: 16 orders", "error")])
        with app.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders WHERE internal_status='COMPLETED' AND ats_status='PENDING'").fetchone()[0], 16)

    def test_reset_allows_a_new_transition_and_authorization_is_required(self):
        with self.assertRaises(app.HTTPException) as error:
            app.trigger("ats-drift", "wrong-key")
        self.assertEqual(error.exception.status_code, 403)

        first = app.trigger("ats-drift", "test-key")
        reset = app.reset("test-key")
        second = app.trigger("ats-drift", "test-key")

        self.assertEqual(first["changed_orders"], 16)
        self.assertEqual(reset["result"], "synthetic orders reset")
        self.assertEqual(second["changed_orders"], 16)
        self.assertEqual(len(self.sentry.messages), 2)


if __name__ == "__main__":
    unittest.main()
