-- Report query role (M2). Idempotent; run as the PostgreSQL superuser with
-- psql -v report_pw=... . Keep in sync with app/db/roles.py REPORT_ROLE_DEFAULTS.
SELECT format('CREATE ROLE eaios_report LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT CONNECTION LIMIT 8 PASSWORD %L', :'report_pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eaios_report') \gexec
ALTER ROLE eaios_report LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT CONNECTION LIMIT 8 PASSWORD :'report_pw';
ALTER ROLE eaios_report SET default_transaction_read_only = 'on';
ALTER ROLE eaios_report SET statement_timeout = '5000';
ALTER ROLE eaios_report SET lock_timeout = '1000';
ALTER ROLE eaios_report SET idle_in_transaction_session_timeout = '15000';
ALTER ROLE eaios_report SET search_path = 'reporting';
ALTER ROLE eaios_report SET work_mem = '16MB';
ALTER ROLE eaios_report SET temp_file_limit = '32MB';
GRANT CONNECT ON DATABASE eaios TO eaios_report;
