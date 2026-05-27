-- Allow same email across different companies (multi-tenant fix)
ALTER TABLE portal_users DROP CONSTRAINT portal_users_email_key;
ALTER TABLE portal_users ADD CONSTRAINT portal_users_email_company_key UNIQUE (email, company);
