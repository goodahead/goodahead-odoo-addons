-- A neutralized copy (a test or staging database made from production) must
-- never call Claude with the production key: it would send real documents to
-- Anthropic again and bill the production account.
UPDATE res_company
   SET claude_api_key = NULL;
