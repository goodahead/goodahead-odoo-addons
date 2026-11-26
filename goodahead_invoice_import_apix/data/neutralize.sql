-- A neutralized copy (a test or staging database made from production) must
-- never receive from APIX: it would mark packages received on the live
-- account, and the production database would never see those invoices.
UPDATE goodahead_apix_connection
   SET receiving_state = 'off';
