-- A neutralized copy (a test or staging database made from production) must
-- never receive from APIX: it would mark packages received on the live
-- account, and the production database would never see those invoices.
UPDATE goodahead_apix_connection
   SET receiving_state = 'off';
-- A StorageKey alone downloads a package or marks it received: the copy
-- keeps the package ids for its history, never the live account's keys.
UPDATE goodahead_invoice_import
   SET apix_storage_key = NULL
 WHERE apix_storage_key IS NOT NULL;
