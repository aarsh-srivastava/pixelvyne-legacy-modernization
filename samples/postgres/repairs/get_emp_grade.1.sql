-- Repair for get_emp_grade: without STRICT the NO_DATA_FOUND handler never fires (gg-05).
CREATE OR REPLACE FUNCTION get_emp_grade (p_emp_id numeric) RETURNS varchar
LANGUAGE plpgsql
AS $$
DECLARE
  v_salary employees.base_salary%TYPE;
  v_rating varchar(10);
BEGIN
  SELECT base_salary, COALESCE(perf_rating, 'U')
    INTO STRICT v_salary, v_rating
    FROM employees
   WHERE emp_id = p_emp_id;

  RETURN
    CASE v_rating
      WHEN 'A' THEN 'G1'
      WHEN 'B' THEN 'G2'
      WHEN 'C' THEN 'G3'
      WHEN 'U' THEN 'UNRATED'
      ELSE 'G4'
    END
    || '-'
    || CASE WHEN v_salary >= 100000 THEN 'SENIOR' ELSE 'STANDARD' END;
EXCEPTION
  WHEN NO_DATA_FOUND THEN
    RETURN NULL;
END;
$$;
