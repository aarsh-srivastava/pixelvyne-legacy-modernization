-- Conversion of samples/oracle/06_triggers.sql. This one is done correctly (no trap):
-- PostgreSQL needs a trigger function, :NEW/:OLD become NEW/OLD, the WHEN clause and
-- the sequence call are kept.

CREATE OR REPLACE FUNCTION emp_salary_audit_trg_fn() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  INSERT INTO salary_audit (audit_id, emp_id, old_salary, new_salary, changed_on, change_type)
  VALUES (nextval('salary_audit_seq'),
          NEW.emp_id,
          OLD.base_salary,
          NEW.base_salary,
          date_trunc('day', localtimestamp),
          CASE WHEN NEW.base_salary > OLD.base_salary THEN 'RAISE' ELSE 'CUT' END);
  RETURN NULL;
END;
$$;

CREATE TRIGGER emp_salary_audit_trg
AFTER UPDATE OF base_salary ON employees
FOR EACH ROW
WHEN (NEW.base_salary <> OLD.base_salary)
EXECUTE FUNCTION emp_salary_audit_trg_fn();
