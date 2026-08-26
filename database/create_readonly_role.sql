-- M6 Task 1: a non-superuser, read-only role for the runtime query path.
--
-- AUDIT.md P2 established that `enterprise_user` is a PostgreSQL superuser with
-- Create role, Create DB, Replication and Bypass RLS, and that the runtime used it for
-- every query. "The runtime workflow is read-only" was enforced by nothing below the
-- application.
--
-- This role is what the retrieval layer connects as. The loaders keep using the owning
-- role, because they legitimately write.
--
-- Idempotent: safe to re-run after a schema reload.
--
-- Usage:
--   psql -v readonly_password="'...'" -f database/create_readonly_role.sql

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------- role
--
-- `\gexec` rather than a DO block: psql does not substitute :variables inside
-- dollar-quoted strings, so the password would arrive as the literal text
-- ":readonly_password". quote_literal() does the escaping.

SELECT 'CREATE ROLE enterprise_readonly LOGIN PASSWORD '
       || quote_literal(:'readonly_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'enterprise_readonly')
\gexec

SELECT 'ALTER ROLE enterprise_readonly WITH LOGIN PASSWORD '
       || quote_literal(:'readonly_password')
\gexec

-- Explicitly deny every role attribute. CREATE ROLE defaults these off, but an
-- ALTER on an existing role must not inherit whatever it had before.
ALTER ROLE enterprise_readonly
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;

-- ---------------------------------------------------------------- start from nothing
REVOKE ALL ON DATABASE enterprise_ai FROM enterprise_readonly;
REVOKE ALL ON SCHEMA ecommerce FROM enterprise_readonly;
REVOKE ALL ON ALL TABLES IN SCHEMA ecommerce FROM enterprise_readonly;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA ecommerce FROM enterprise_readonly;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA ecommerce FROM enterprise_readonly;

-- The public schema is writable by PUBLIC on PostgreSQL < 15 and is a standard way
-- for a "read-only" role to create scratch tables.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM enterprise_readonly;

-- ---------------------------------------------------------------- grant only SELECT
GRANT CONNECT ON DATABASE enterprise_ai TO enterprise_readonly;
GRANT USAGE ON SCHEMA ecommerce TO enterprise_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA ecommerce TO enterprise_readonly;

-- Tables and views created later must also be SELECT-only, not unreachable and not
-- writable. Without this, a schema reload silently breaks the runtime.
ALTER DEFAULT PRIVILEGES FOR ROLE enterprise_user IN SCHEMA ecommerce
    GRANT SELECT ON TABLES TO enterprise_readonly;

-- ---------------------------------------------------------------- report
SELECT
    rolname,
    rolsuper      AS superuser,
    rolcreaterole AS create_role,
    rolcreatedb   AS create_db,
    rolreplication AS replication,
    rolbypassrls  AS bypass_rls,
    rolcanlogin   AS can_login
FROM pg_roles
WHERE rolname IN ('enterprise_user', 'enterprise_readonly')
ORDER BY rolname;

SELECT
    grantee,
    privilege_type,
    count(*) AS object_count
FROM information_schema.role_table_grants
WHERE grantee = 'enterprise_readonly'
  AND table_schema = 'ecommerce'
GROUP BY grantee, privilege_type
ORDER BY privilege_type;
