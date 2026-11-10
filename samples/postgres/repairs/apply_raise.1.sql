-- First repair for apply_raise: p_pct / 100 was integer division (ar-01).
-- The unknown-employee case (ar-03) is still wrong; apply_raise.2.sql fixes it.
CREATE OR REPLACE PROCEDURE apply_raise (
  p_emp_id   numeric,
  p_pct      integer,
  OUT p_new_salary numeric
)
LANGUAGE plpgsql
AS $$
DECLARE
  v_old employees.base_salary%TYPE;
BEGIN
  SELECT base_salary
    INTO v_old
    FROM employees
   WHERE emp_id = p_emp_id
     FOR UPDATE;

  p_new_salary := ROUND(v_old * (1 + p_pct / 100.0), 2);

  UPDATE employees
     SET base_salary = p_new_salary
   WHERE emp_id = p_emp_id;
EXCEPTION
  WHEN NO_DATA_FOUND THEN
    p_new_salary := -1;
END;
$$;
