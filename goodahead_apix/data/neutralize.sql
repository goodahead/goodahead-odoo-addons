-- A neutralized copy (a test or staging database made from production) must
-- never reach APIX with the production account: it would receive from or send
-- through the live inbox. The transfer credentials are cleared; an
-- Administrator enters them again on purpose (send ADR-0011, AC-41). The
-- Business ID and environment stay. Run by the ERP's neutralization, not at
-- install.
UPDATE goodahead_apix_connection
   SET transfer_id = NULL,
       transfer_key = NULL,
       status = 'unchecked',
       status_message = 'Credentials cleared by database neutralization';
