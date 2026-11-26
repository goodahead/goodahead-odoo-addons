# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The install hooks, exercised inside one rolled-back transaction.

The full rename runs only on a real upgrade from the old AI add-on, which a
TransactionCase cannot reproduce (the module is already installed). What can be
pinned here is every decision the hook makes on the way: when it does nothing,
when it refuses, what it counts as the core's, and that the reference sweep and
the ownership move do what they say on rows staged for the purpose. The
end-to-end rename is rehearsed on a copy of a database with the old add-on (see
the task notes of T1).
"""

import uuid

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_import import hooks


@tagged("post_install", "-at_install")
class TestRenameHook(TransactionCase):
    def _set_module_state(self, name, state):
        self.env.cr.execute(
            "UPDATE ir_module_module SET state = %s WHERE name = %s", (state, name)
        )

    def _ai_module_present(self):
        return bool(hooks._module_id(self.env.cr, hooks.AI_MODULE))

    def _create_old_table(self):
        self.env.cr.execute(f'CREATE TABLE "{hooks.OLD_TABLE}" (id serial PRIMARY KEY)')

    # -- when it does nothing, and when it refuses ---------------------------

    def test_fresh_install_does_nothing(self):
        self.assertFalse(hooks._table_exists(self.env.cr, hooks.OLD_TABLE))
        self.env.cr.execute("SELECT count(*) FROM ir_model_data")
        before = self.env.cr.fetchone()[0]
        hooks.pre_init_hook(self.env)
        self.env.cr.execute("SELECT count(*) FROM ir_model_data")
        self.assertEqual(self.env.cr.fetchone()[0], before)
        self.assertTrue(hooks._table_exists(self.env.cr, hooks.NEW_TABLE))

    def test_refuses_when_the_ai_add_on_is_not_being_upgraded(self):
        """The core installed on its own beside the old table: refused, and
        the old table is still there under its old name."""
        if not self._ai_module_present():
            self.skipTest("the AI add-on is not known to this database")
        self._create_old_table()
        for state in ("installed", "uninstalled", "to install"):
            with self.subTest(state=state):
                self._set_module_state(hooks.AI_MODULE, state)
                with self.assertRaisesRegex(UserError, "upgrading goodahead_invoice_ai_import"):
                    hooks.pre_init_hook(self.env)
                self.assertTrue(hooks._table_exists(self.env.cr, hooks.OLD_TABLE))

    def test_refuses_while_jobs_for_the_old_model_are_queued(self):
        if not self._ai_module_present():
            self.skipTest("the AI add-on is not known to this database")
        self._create_old_table()
        self._set_module_state(hooks.AI_MODULE, "to upgrade")
        for state in hooks.ACTIVE_JOB_STATES:
            self.env.cr.execute(
                "INSERT INTO queue_job (uuid, state, model_name) VALUES (%s, %s, %s)",
                (str(uuid.uuid4()), state, hooks.OLD_MODEL),
            )
        # A finished job does not block.
        self.env.cr.execute(
            "INSERT INTO queue_job (uuid, state, model_name) VALUES (%s, 'done', %s)",
            (str(uuid.uuid4()), hooks.OLD_MODEL),
        )
        with self.assertRaisesRegex(UserError, f"^{len(hooks.ACTIVE_JOB_STATES)} invoice"):
            hooks.pre_init_hook(self.env)
        self.assertTrue(hooks._table_exists(self.env.cr, hooks.OLD_TABLE))

    def test_the_supported_path_passes_the_guards(self):
        if not self._ai_module_present():
            self.skipTest("the AI add-on is not known to this database")
        self._set_module_state(hooks.AI_MODULE, "to upgrade")
        self.env.cr.execute(
            "INSERT INTO queue_job (uuid, state, model_name) VALUES (%s, 'done', %s)",
            (str(uuid.uuid4()), hooks.OLD_MODEL),
        )
        hooks.assert_supported_path(self.env.cr)

    # -- what counts as the core's -------------------------------------------

    def test_core_data_xmlids_are_read_from_the_manifest(self):
        names = hooks.core_data_xmlids()
        for expected in (
            "view_ai_invoice_import_tree",
            "view_ai_invoice_import_form",
            "action_ai_invoice_import",
            "menu_ai_invoice_import",
            "menu_ai_invoice_import_config",
            "res_config_settings_view_form",
            "view_move_form_vendor_suggestions",
            "view_vendor_data_review_form",
            "access_ai_invoice_import_accountant",
            "access_partner_identifier_user",
            "expense_category_goods",
        ):
            self.assertIn(expected, names)
        # Records the AI add-on keeps, and other modules' ids, are not ours.
        for foreign in ("view_prompt_profile_form", "channel_ai_import", "view_move_form"):
            self.assertNotIn(foreign, names)
        # Every one of them really is the core's in this database.
        Data = self.env["ir.model.data"]
        for name in names:
            self.assertTrue(
                Data.search_count([("module", "=", hooks.CORE_MODULE), ("name", "=", name)]),
                name,
            )

    def test_core_owned_fields_leave_the_ai_fields_alone(self):
        owned = hooks.core_owned_fields(self.env)
        record = owned[hooks.NEW_MODEL]
        for name in ("state", "extraction_data", "move_id", "id", "create_date", "message_ids"):
            self.assertIn(name, record)
        for name in ("extraction_model", "input_tokens", "output_tokens"):
            self.assertNotIn(name, record)
        self.assertIn("ai_vendor_candidate_ids", owned["account.move"])
        self.assertIn("identifier_ids", owned["res.partner"])
        self.assertIn("ai_public_email_domains", owned["res.config.settings"])
        self.assertNotIn("claude_invoice_model_id", owned.get("res.config.settings", ()))
        self.assertIn(hooks.NEW_MODEL, hooks.core_defined_models())
        self.assertNotIn("account.move", hooks.core_defined_models())

    def test_the_ai_add_on_still_owns_its_own_fields(self):
        """What the ownership rule protects: the AI fields' external ids are the
        AI add-on's, so an upgrade never drops their columns."""
        if self.env["ir.module.module"].search(
            [("name", "=", hooks.AI_MODULE), ("state", "=", "installed")]
        ):
            data = self.env["ir.model.data"].search(
                [
                    ("model", "=", "ir.model.fields"),
                    ("name", "=", "field_goodahead_invoice_import__extraction_model"),
                ]
            )
            self.assertEqual(data.mapped("module"), [hooks.AI_MODULE])
        data = self.env["ir.model.data"].search(
            [
                ("model", "=", "ir.model.fields"),
                ("name", "=", "field_goodahead_invoice_import__state"),
            ]
        )
        self.assertIn(hooks.CORE_MODULE, data.mapped("module"))

    # -- the rewrite, on staged rows -----------------------------------------

    def test_rename_model_references_rewrites_text_columns(self):
        old, new = "x.goodahead.old.probe", "x.goodahead.new.probe"
        # Raw rows: the ORM would refuse to point at a model that does not exist.
        cr = self.env.cr
        cr.execute(
            "INSERT INTO ir_attachment (name, type, res_model, res_id) "
            "VALUES ('probe.txt', 'binary', %s, 1) RETURNING id",
            (old,),
        )
        attachment_id = cr.fetchone()[0]
        cr.execute(
            "INSERT INTO mail_message (model, res_id, message_type) "
            "VALUES (%s, 1, 'comment') RETURNING id",
            (old,),
        )
        message_id = cr.fetchone()[0]
        self.env.cr.execute(
            "INSERT INTO queue_job (uuid, state, model_name, records, func_string) "
            "VALUES (%s, 'done', %s, %s::jsonb, %s) RETURNING id",
            (
                str(uuid.uuid4()),
                old,
                '{"_type": "odoo_recordset", "model": "%s", "ids": [1]}' % old,
                f"{old}(1,)._process_import()",
            ),
        )
        job_id = self.env.cr.fetchone()[0]
        counts = hooks.rename_model_references(cr, old, new)
        cr.execute("SELECT res_model FROM ir_attachment WHERE id = %s", (attachment_id,))
        self.assertEqual(cr.fetchone()[0], new)
        cr.execute("SELECT model FROM mail_message WHERE id = %s", (message_id,))
        self.assertEqual(cr.fetchone()[0], new)
        self.assertEqual(counts[("ir_attachment", "res_model")], 1)
        self.env.cr.execute(
            "SELECT model_name, records->>'model', func_string FROM queue_job WHERE id = %s",
            (job_id,),
        )
        self.assertEqual(
            self.env.cr.fetchone(), (new, new, f"{new}(1,)._process_import()")
        )

    def test_model_name_columns_skip_integer_links(self):
        columns = set(hooks.model_name_columns(self.env.cr))
        self.assertIn(("mail_message", "model"), columns)
        self.assertIn(("ir_filters", "model_id"), columns)
        # `ir_model_access.model_id` is an integer link to ir_model.
        self.assertNotIn(("ir_model_access", "model_id"), columns)

    def test_move_ownership_hands_the_core_back_what_it_declares(self):
        """Stage the old state for a few rows — owned by the AI add-on — and
        let the move put them back. A field the core does not declare stays."""
        if not self._ai_module_present():
            self.skipTest("the AI add-on is not known to this database")
        Data = self.env["ir.model.data"].sudo()
        staged = Data.search(
            [
                ("module", "=", hooks.CORE_MODULE),
                (
                    "name",
                    "in",
                    [
                        "view_ai_invoice_import_tree",
                        "access_ai_invoice_import_manager",
                        "model_goodahead_invoice_import",
                        "field_goodahead_invoice_import__state",
                        "field_account_move__ai_vendor_candidate_ids",
                        "selection__goodahead_invoice_import__state__failed",
                    ],
                ),
            ]
        )
        self.assertEqual(len(staged), 6)
        # The AI add-on's own id must not exist twice under one module.
        Data.search(
            [("module", "=", hooks.AI_MODULE), ("name", "in", staged.mapped("name"))]
        ).unlink()
        staged.write({"module": hooks.AI_MODULE})
        ai_field = Data.search(
            [("name", "=", "field_goodahead_invoice_import__extraction_model")]
        )
        ai_field.write({"module": hooks.AI_MODULE})
        self.env.flush_all()

        moved = hooks.move_ownership(self.env)
        self.env.invalidate_all()

        self.assertEqual(set(staged.mapped("module")), {hooks.CORE_MODULE})
        if ai_field:
            self.assertEqual(set(ai_field.mapped("module")), {hooks.AI_MODULE})
        self.assertGreaterEqual(moved["data"], 2)
        # Idempotent: a second run moves nothing and collides with nothing.
        again = hooks.move_ownership(self.env)
        self.assertEqual(again.get("data"), 0)

    # -- the post-install index ----------------------------------------------

    def test_bill_ref_index_is_created_and_dropped(self):
        def exists():
            self.env.cr.execute(
                "SELECT 1 FROM pg_indexes WHERE indexname = %s", (hooks.BILL_REF_INDEX,)
            )
            return bool(self.env.cr.fetchone())

        self.assertTrue(exists(), "post_init_hook creates the index")
        hooks.uninstall_hook(self.env)
        self.assertFalse(exists())
        hooks.create_bill_ref_index(self.env.cr)
        self.assertTrue(exists())
