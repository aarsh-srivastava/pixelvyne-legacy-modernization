-- Second repair for apply_raise: SELECT INTO STRICT, so an unknown employee
-- reaches the NO_DATA_FOUND handler and returns -1 (ar-03).
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
    INTO STRICT v_old
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
