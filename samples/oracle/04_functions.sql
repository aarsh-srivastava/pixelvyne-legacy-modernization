
-- File 4 of 7: standalone functions.

-- Grade label for an employee, e.g. 'G1-SENIOR'. Unknown employee -> NULL.
CREATE OR REPLACE FUNCTION get_emp_grade (p_emp_id IN NUMBER) RETURN VARCHAR2 AS
  v_salary employees.base_salary%TYPE;
  v_rating VARCHAR2(10);
BEGIN
  SELECT base_salary, NVL(perf_rating, 'U')
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
END get_emp_grade;
/

-- Comma-separated names of the top N active earners in a department.
-- Ties on salary are broken by emp_id. No employees -> NULL.
CREATE OR REPLACE FUNCTION top_earners (p_dept_id IN NUMBER, p_n IN NUMBER) RETURN VARCHAR2 AS
  v_list VARCHAR2(4000);
BEGIN
  FOR r IN (SELECT full_name
              FROM (SELECT full_name
                      FROM employees
                     WHERE dept_id = p_dept_id
                       AND status = 'ACTIVE'
                     ORDER BY base_salary DESC, emp_id)
             WHERE ROWNUM <= p_n) LOOP
    v_list := v_list || CASE WHEN v_list IS NOT NULL THEN ', ' END || r.full_name;
  END LOOP;
  RETURN v_list;
END top_earners;
/
