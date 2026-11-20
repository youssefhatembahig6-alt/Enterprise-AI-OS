#!/bin/sh
# Runs once, as the PostgreSQL superuser, only when the data volume is empty
# (docker-entrypoint-initdb.d). Creates the privileged migration/owner role, the
# restricted application role and the application database. Neither role is a
# superuser or has BYPASSRLS; the application role owns nothing.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v owner_pw="$EAIOS_OWNER_DB_PASSWORD" -v app_pw="$EAIOS_APP_DB_PASSWORD" <<'SQL'
CREATE ROLE eaios_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD :'owner_pw';
CREATE ROLE eaios_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT PASSWORD :'app_pw';
CREATE DATABASE eaios OWNER eaios_owner;
REVOKE ALL ON DATABASE eaios FROM PUBLIC;
GRANT CONNECT ON DATABASE eaios TO eaios_app;
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname eaios <<'SQL'
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
