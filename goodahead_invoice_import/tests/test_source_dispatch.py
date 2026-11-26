# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The core's source dispatch (core/source split), with stand-in readers.

The readers below exist only in this file: the core alone has none. They show
that the mail door and the job go by priority and by the stored reader, never
by the order in which source add-ons were loaded. Invented data only.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_import.lib.import_source import (
    STRICT_POLICY,
    SourceReader,
    readers_in_order,
)


@tagged("post_install", "-at_install")
class TestCoreSourceDispatch(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.Import = cls.env["goodahead.invoice.import"]

    def test_readers_in_order_ignores_the_dict_order(self):
        late = SourceReader("zz_late", 50)
        early = SourceReader("zz_early", 5, STRICT_POLICY, structured=True)
        tie = SourceReader("zz_alpha", 50)
        self.assertEqual(
            readers_in_order({"zz_late": late, "zz_early": early, "zz_alpha": tie}),
            ["zz_early", "zz_alpha", "zz_late"],
        )
        self.assertEqual(
            readers_in_order({"zz_alpha": tie, "zz_late": late, "zz_early": early}),
            ["zz_early", "zz_alpha", "zz_late"],
        )

    def test_mail_that_no_reader_takes_fails_unsupported(self):
        with patch.object(type(self.Import), "_import_readers", lambda self: {}):
            record = self.Import.message_new(
                {
                    "email_from": "zz@example.test",
                    "subject": "ZZ nothing to read",
                    "attachments": [("zz.bin", b"\x00\x01")],
                }
            )
        self.assertEqual(record.state, "failed")
        self.assertEqual(record.source, "email")
        self.assertIn("Format not supported", record.error_message)
        self.assertIn("zz.bin", record.error_message)
        self.assertIn("No import source is installed", record.error_message)

    def test_a_record_no_reader_claims_fails_with_a_reason(self):
        record = self.Import.create({"name": "zz.xml"})
        with patch.object(type(self.Import), "_import_readers", lambda self: {}):
            result = record._process_import()
        self.assertTrue(result.startswith("FAILED"))
        self.assertEqual(record.state, "failed")
        self.assertIn("No reader", record.error_message)
