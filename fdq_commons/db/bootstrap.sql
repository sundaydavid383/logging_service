-- ==========================================
-- Fiducia DQMS Database Bootstrap (ADMIN ONLY)
-- Run ONCE per database as postgres/superuser
-- ==========================================

CREATE ROLE fdq_readonly;

GRANT CONNECT ON DATABASE fdq_db TO fdq_readonly;

GRANT USAGE ON SCHEMA public TO fdq_readonly;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO fdq_readonly;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT SELECT ON TABLES TO fdq_readonly;


-- App user permissions
GRANT ALL PRIVILEGES ON DATABASE fdq_db TO fdq_user;
GRANT USAGE, CREATE ON SCHEMA public TO fdq_user;
