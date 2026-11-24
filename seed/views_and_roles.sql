-- ===========================================================================
-- Read-only Postgres role per application role, one view set per role, and RLS
-- on the base tables.
--
-- How the two layers fit together
-- -------------------------------
-- * Column and table visibility is the VIEW's job. A supervisor's view set has
--   no defects and no supplier_parts; the QE's supplier_parts view simply does
--   not project unit_price_inr, so the column cannot be selected, aliased or
--   aggregated - it does not exist in that namespace.
-- * ROW visibility is RLS's job. Policies read current_setting('app.user_id'),
--   join to users to derive the role and scope, and restrict a supervisor to
--   their own line.
--
-- The subtle part: a view is permission-checked as its OWNER, and a table owner
-- is normally exempt from RLS. So the tables are owned by a dedicated
-- non-superuser role (ironask_owner) and every table is set to FORCE ROW LEVEL
-- SECURITY, which removes that exemption. The application roles therefore hold
-- no privilege at all on the base tables (requirement: "revoke everything on
-- base tables"), yet RLS still decides which rows come back through the views.
--
-- Fail-closed: if app.user_id is unset or unknown, public.app_role() is NULL, every
-- policy predicate is false and every view returns zero rows.
-- ===========================================================================

-- --------------------------------------------------------------- owner role
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ironask_owner') THEN
        CREATE ROLE ironask_owner NOLOGIN;
    END IF;
END $$;

ALTER TABLE machines        OWNER TO ironask_owner;
ALTER TABLE work_orders     OWNER TO ironask_owner;
ALTER TABLE downtime_events OWNER TO ironask_owner;
ALTER TABLE defects         OWNER TO ironask_owner;
ALTER TABLE supplier_parts  OWNER TO ironask_owner;
ALTER TABLE users           OWNER TO ironask_owner;

-- ------------------------------------------------------- scope helper functions
-- Every reference is schema-qualified and search_path is pinned on the
-- function, because an application role's search_path is its own view schema
-- only and an unqualified `users` would not resolve there.
--
-- SECURITY DEFINER is required, not incidental: pinning search_path blocks
-- inlining, so without it the body would execute with the CALLER's privileges
-- and the application roles hold none on public.users. Running as the owner
-- leaks nothing - each function only ever reports the role or scope belonging
-- to the app.user_id the server already set for this transaction, and cannot
-- be used to enumerate anyone else.
CREATE OR REPLACE FUNCTION public.app_user_id() RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = public, pg_temp
AS $$
    SELECT nullif(current_setting('app.user_id', true), '')
$$;

CREATE OR REPLACE FUNCTION public.app_role() RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = public, pg_temp
AS $$
    SELECT u.role FROM public.users u WHERE u.user_id = public.app_user_id()
$$;

CREATE OR REPLACE FUNCTION public.app_scope() RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = public, pg_temp
AS $$
    SELECT u.scope FROM public.users u WHERE u.user_id = public.app_user_id()
$$;

ALTER FUNCTION public.app_user_id() OWNER TO ironask_owner;
ALTER FUNCTION public.app_role()    OWNER TO ironask_owner;
ALTER FUNCTION public.app_scope()   OWNER TO ironask_owner;

-- =========================================================== row level security

ALTER TABLE machines        ENABLE ROW LEVEL SECURITY;
ALTER TABLE work_orders     ENABLE ROW LEVEL SECURITY;
ALTER TABLE downtime_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE defects         ENABLE ROW LEVEL SECURITY;
ALTER TABLE supplier_parts  ENABLE ROW LEVEL SECURITY;

-- Remove the owner's exemption, otherwise the views (checked as the owner)
-- would see every row.
ALTER TABLE machines        FORCE ROW LEVEL SECURITY;
ALTER TABLE work_orders     FORCE ROW LEVEL SECURITY;
ALTER TABLE downtime_events FORCE ROW LEVEL SECURITY;
ALTER TABLE defects         FORCE ROW LEVEL SECURITY;
ALTER TABLE supplier_parts  FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS machines_sel        ON machines;
DROP POLICY IF EXISTS work_orders_sel     ON work_orders;
DROP POLICY IF EXISTS downtime_events_sel ON downtime_events;
DROP POLICY IF EXISTS defects_sel         ON defects;
DROP POLICY IF EXISTS supplier_parts_sel  ON supplier_parts;

-- A supervisor sees only their own line. QE and the plant head see every line.
CREATE POLICY machines_sel ON machines FOR SELECT USING (
    public.app_role() IN ('quality_engineer', 'plant_head')
    OR (public.app_role() = 'line_supervisor' AND line = public.app_scope())
);

CREATE POLICY work_orders_sel ON work_orders FOR SELECT USING (
    public.app_role() IN ('quality_engineer', 'plant_head')
    OR (
        public.app_role() = 'line_supervisor'
        AND EXISTS (
            SELECT 1 FROM machines m
            WHERE m.machine_id = work_orders.machine_id
              AND m.line = public.app_scope()
        )
    )
);

CREATE POLICY downtime_events_sel ON downtime_events FOR SELECT USING (
    public.app_role() IN ('quality_engineer', 'plant_head')
    OR (
        public.app_role() = 'line_supervisor'
        AND EXISTS (
            SELECT 1 FROM machines m
            WHERE m.machine_id = downtime_events.machine_id
              AND m.line = public.app_scope()
        )
    )
);

-- Defects and supplier parts are not part of a supervisor's visibility at all.
CREATE POLICY defects_sel ON defects FOR SELECT USING (
    public.app_role() IN ('quality_engineer', 'plant_head')
);

CREATE POLICY supplier_parts_sel ON supplier_parts FOR SELECT USING (
    public.app_role() IN ('quality_engineer', 'plant_head')
);

-- No INSERT/UPDATE/DELETE policy exists anywhere, so even if a write privilege
-- were granted by mistake, every write would still match no policy and fail.

-- ================================================================ role schemas

DROP SCHEMA IF EXISTS sup CASCADE;
DROP SCHEMA IF EXISTS qe CASCADE;
DROP SCHEMA IF EXISTS head CASCADE;

CREATE SCHEMA sup  AUTHORIZATION ironask_owner;
CREATE SCHEMA qe   AUTHORIZATION ironask_owner;
CREATE SCHEMA head AUTHORIZATION ironask_owner;

-- ---- line_supervisor: machines, work_orders, downtime_events only ---------
CREATE VIEW sup.machines AS
    SELECT machine_id, line, type, model, commissioned_on FROM machines;

CREATE VIEW sup.work_orders AS
    SELECT wo_id, machine_id, part_no, qty_planned, qty_good, started_at, ended_at
    FROM work_orders;

CREATE VIEW sup.downtime_events AS
    SELECT event_id, machine_id, start_at, end_at, category, reason_code, alarm_code
    FROM downtime_events;

-- ---- quality_engineer: the above + defects + supplier_parts WITHOUT price --
CREATE VIEW qe.machines AS
    SELECT machine_id, line, type, model, commissioned_on FROM machines;

CREATE VIEW qe.work_orders AS
    SELECT wo_id, machine_id, part_no, qty_planned, qty_good, started_at, ended_at
    FROM work_orders;

CREATE VIEW qe.downtime_events AS
    SELECT event_id, machine_id, start_at, end_at, category, reason_code, alarm_code
    FROM downtime_events;

CREATE VIEW qe.defects AS
    SELECT defect_id, wo_id, part_no, defect_type, qty, found_at, stage FROM defects;

-- unit_price_inr is deliberately absent. This is the price leak barrier at the
-- SQL layer: the column is not in the qe namespace at all.
CREATE VIEW qe.supplier_parts AS
    SELECT part_no, supplier, valid_from FROM supplier_parts;

-- ---- plant_head: everything, including the price --------------------------
CREATE VIEW head.machines AS
    SELECT machine_id, line, type, model, commissioned_on FROM machines;

CREATE VIEW head.work_orders AS
    SELECT wo_id, machine_id, part_no, qty_planned, qty_good, started_at, ended_at
    FROM work_orders;

CREATE VIEW head.downtime_events AS
    SELECT event_id, machine_id, start_at, end_at, category, reason_code, alarm_code
    FROM downtime_events;

CREATE VIEW head.defects AS
    SELECT defect_id, wo_id, part_no, defect_type, qty, found_at, stage FROM defects;

CREATE VIEW head.supplier_parts AS
    SELECT part_no, supplier, unit_price_inr, valid_from FROM supplier_parts;

-- CRITICAL: a view is permission-checked, and has its RLS evaluated, as its
-- OWNER. These views are created by the bootstrap superuser, and a superuser
-- bypasses RLS unconditionally - which would quietly hand every role every
-- row. Reassign them to the non-superuser owner, which FORCE ROW LEVEL
-- SECURITY does subject to the policies above.
DO $$
DECLARE v record;
BEGIN
    FOR v IN
        SELECT schemaname, viewname FROM pg_views
        WHERE schemaname IN ('sup', 'qe', 'head')
    LOOP
        EXECUTE format('ALTER VIEW %I.%I OWNER TO ironask_owner', v.schemaname, v.viewname);
    END LOOP;
END $$;

-- Guard against the bug ever coming back.
DO $$
DECLARE bad int;
BEGIN
    SELECT count(*) INTO bad
    FROM pg_views v
    JOIN pg_roles r ON r.rolname = v.viewowner
    WHERE v.schemaname IN ('sup', 'qe', 'head')
      AND (r.rolsuper OR r.rolbypassrls);
    IF bad > 0 THEN
        RAISE EXCEPTION
            'ironask: % role view(s) are owned by a superuser / BYPASSRLS role; RLS would not apply',
            bad;
    END IF;
END $$;

-- ============================================================= login roles

DO $$
DECLARE
    pw text := current_setting('ironask.role_password', true);
BEGIN
    IF pw IS NULL OR pw = '' THEN
        pw := 'ironask_role_pw';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_line_supervisor') THEN
        EXECUTE format('CREATE ROLE app_line_supervisor LOGIN PASSWORD %L', pw);
    ELSE
        EXECUTE format('ALTER ROLE app_line_supervisor LOGIN PASSWORD %L', pw);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_quality_engineer') THEN
        EXECUTE format('CREATE ROLE app_quality_engineer LOGIN PASSWORD %L', pw);
    ELSE
        EXECUTE format('ALTER ROLE app_quality_engineer LOGIN PASSWORD %L', pw);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_plant_head') THEN
        EXECUTE format('CREATE ROLE app_plant_head LOGIN PASSWORD %L', pw);
    ELSE
        EXECUTE format('ALTER ROLE app_plant_head LOGIN PASSWORD %L', pw);
    END IF;
END $$;

-- ------------------------------------------------------- revoke, then grant
-- Start from nothing. These roles must hold no privilege on any base table,
-- on the document index, on the graph mirror, or on the users table.
REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM app_line_supervisor, app_quality_engineer, app_plant_head;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM app_line_supervisor, app_quality_engineer, app_plant_head;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM app_line_supervisor, app_quality_engineer, app_plant_head;
REVOKE ALL ON SCHEMA public FROM app_line_supervisor, app_quality_engineer, app_plant_head;
REVOKE ALL ON DATABASE ironask FROM app_line_supervisor, app_quality_engineer, app_plant_head;

GRANT CONNECT ON DATABASE ironask TO app_line_supervisor, app_quality_engineer, app_plant_head;

-- USAGE on public is needed so the helper functions in the view bodies resolve,
-- but USAGE alone conveys no access to any table in it.
GRANT USAGE ON SCHEMA public TO app_line_supervisor, app_quality_engineer, app_plant_head;
GRANT EXECUTE ON FUNCTION public.app_user_id(), public.app_role(), public.app_scope()
    TO app_line_supervisor, app_quality_engineer, app_plant_head;

GRANT USAGE ON SCHEMA sup  TO app_line_supervisor;
GRANT USAGE ON SCHEMA qe   TO app_quality_engineer;
GRANT USAGE ON SCHEMA head TO app_plant_head;

-- SELECT on the allowed views only. No other privilege, on anything.
GRANT SELECT ON sup.machines, sup.work_orders, sup.downtime_events
    TO app_line_supervisor;

GRANT SELECT ON qe.machines, qe.work_orders, qe.downtime_events,
                qe.defects, qe.supplier_parts
    TO app_quality_engineer;

GRANT SELECT ON head.machines, head.work_orders, head.downtime_events,
                head.defects, head.supplier_parts
    TO app_plant_head;

-- Each role resolves unqualified names in its own namespace, so generated SQL
-- can say `downtime_events` and reach only that role's view.
ALTER ROLE app_line_supervisor  SET search_path = sup;
ALTER ROLE app_quality_engineer SET search_path = qe;
ALTER ROLE app_plant_head       SET search_path = head;

-- A runaway query must not hold a shop-floor question open.
ALTER ROLE app_line_supervisor  SET statement_timeout = '5s';
ALTER ROLE app_quality_engineer SET statement_timeout = '5s';
ALTER ROLE app_plant_head       SET statement_timeout = '5s';

-- Belt and braces: make the whole session read-only for these roles.
ALTER ROLE app_line_supervisor  SET default_transaction_read_only = on;
ALTER ROLE app_quality_engineer SET default_transaction_read_only = on;
ALTER ROLE app_plant_head       SET default_transaction_read_only = on;
