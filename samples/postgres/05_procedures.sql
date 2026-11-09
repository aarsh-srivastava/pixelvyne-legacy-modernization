-- Literal conversion of calc_bonus from samples/oracle/05_procedures.sql.
-- Line for line the same logic, so it keeps both traps (see README.md).

CREATE OR REPLACE PROCEDURE calc_bonus (
  p_emp_id   numeric,
  p_override varchar,
  OUT p_bonus numeric
)
LANGUAGE plpgsql
AS $$
DECLARE
  v_salary employees.base_salary%TYPE;
  v_rating employees.perf_rating%TYPE;
BEGIN
  SELECT base_salary, perf_rating
    INTO v_salary, v_rating
    FROM employees
   WHERE emp_id = p_emp_id;

  IF p_override IS NOT NULL THEN
    v_rating := p_override;
  END IF;

  IF v_rating = 'A' THEN
    p_bonus := ROUND(v_salary * 0.20, 2);
  ELSIF v_rating = 'B' THEN
    p_bonus := ROUND(v_salary * 0.10, 2);
  ELSIF v_rating IS NULL THEN
    p_bonus := 0;
  ELSE
    p_bonus := ROUND(v_salary * 0.05, 2);
  END IF;
END;
$$;

-- Literal conversion of apply_raise. Traps kept:
-- * p_pct is integer, so p_pct / 100 is integer division (7 / 100 = 0): ar-01.
-- * SELECT INTO without STRICT, so the NO_DATA_FOUND handler never fires and an
--   unknown employee gets NULL instead of -1: ar-03.
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

  p_new_salary := ROUND(v_old * (1 + p_pct / 100), 2);

  UPDATE employees
     SET base_salary = p_new_salary
   WHERE emp_id = p_emp_id;
EXCEPTION
  WHEN NO_DATA_FOUND THEN
    p_new_salary := -1;
END;
$$;
