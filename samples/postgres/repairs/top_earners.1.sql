-- Repair for top_earners: NULL || text is NULL in PostgreSQL (te-01/02/03/05),
-- so start the list explicitly instead of concatenating onto NULL.
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
    v_list := CASE WHEN v_list IS NULL THEN r.full_name
                   ELSE v_list || ', ' || r.full_name END;
  END LOOP;
  RETURN v_list;
END;
$$;
