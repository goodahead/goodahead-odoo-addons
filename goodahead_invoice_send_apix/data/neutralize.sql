-- A neutralized copy (a test or staging database made from production) must
-- never send real invoices (send ADR-0011, AC-41): sending is switched off for
-- every company and every queued send is cancelled with its job. Running
-- sends are left to the sweeper, which fails them after 10 minutes. The APIX
-- credentials are cleared by goodahead_apix's own script. Run by the ERP's
-- neutralization, not at install.
UPDATE goodahead_apix_connection
   SET sending_enabled = false;

UPDATE queue_job j
   SET state = 'cancelled',
       date_cancelled = (now() AT TIME ZONE 'UTC'),
       result = 'Cancelled by database neutralization'
  FROM goodahead_invoice_send_attempt a
 WHERE a.queue_job_id = j.id
   AND a.state = 'queued'
   AND j.state IN ('wait_dependencies', 'pending', 'enqueued', 'failed');

UPDATE goodahead_invoice_send_attempt
   SET state = 'cancelled',
       end_date = (now() AT TIME ZONE 'UTC'),
       needs_attention = false,
       reason = '[send.cancelled_by_neutralization] The queued send was cancelled because this database copy was neutralized.'
 WHERE state = 'queued';
