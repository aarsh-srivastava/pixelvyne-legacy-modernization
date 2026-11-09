-- Literal conversion of samples/oracle/04_functions.sql.
-- NVL -> COALESCE and ROWNUM -> LIMIT were done (they would not compile otherwise),
-- but the behaviour traps are kept (see README.md).

-- Trap kept: SELECT INTO without STRICT, so the NO_DATA_FOUND handler never fires
-- and an unknown employee gets 'G4-STANDARD' instead of NULL (gg-05).
CREATE OR REPLACE FUNCTION get_emp_grade (p_emp_id numeric) RETURNS varchar
LANGUAGE plpgsql
AS $$
DECLARE
  v_salary employees.base_salary%TYPE;
  v_rating varchar(10);
BEGIN
  SELECT base_salary, COALESCE(perf_rating, 'U')
    INTO v_salary, v_rating
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

-- Trap kept: v_list starts NULL, and NULL || anything is NULL in PostgreSQL,
-- so every call returns NULL (Oracle ignores NULL in ||).
CREATE OR REPLACE FUNCTION top_earners (p_dept_id numeric, p_n numeric) RETURNS varchar
LANGUAGE plpgsql
AS $$
DECLARE
  v_list varchar(4000);
  r record;
BEGIN
  FOR r IN SELECT full_name
             FROM employees
            WHERE dept_id = p_dept_id
              AND status = 'ACTIVE'
            ORDER BY base_salary DESC, emp_id
            LIMIT p_n LOOP
    v_list := v_list || CASE WHEN v_list IS NOT NULL THEN ', ' END || r.full_name;
  END LOOP;
  RETURN v_list;
END;
$$;
