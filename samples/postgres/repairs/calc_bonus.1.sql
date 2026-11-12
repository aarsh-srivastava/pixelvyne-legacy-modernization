-- Repair for calc_bonus: '' is NULL in Oracle (cb-02), and an unknown employee
-- must raise no_data_found (cb-06).
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
    INTO STRICT v_salary, v_rating
    FROM employees
   WHERE emp_id = p_emp_id;

  IF NULLIF(p_override, '') IS NOT NULL THEN
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
