-- File 5 of 7: standalone procedures.

-- Annual bonus for one employee.
-- p_override: optional rating override; NULL or '' means "use the stored rating".
CREATE OR REPLACE PROCEDURE calc_bonus (
  p_emp_id   IN  NUMBER,
  p_override IN  VARCHAR2,
  p_bonus    OUT NUMBER
) AS
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
    p_bonus := 0;                       -- not yet rated
  ELSE
    p_bonus := ROUND(v_salary * 0.05, 2);
  END IF;
END calc_bonus;
/

-- Give one employee a whole-number percentage raise; returns the new salary.
-- Unknown employee -> p_new_salary = -1 and nothing changes.
-- The salary UPDATE fires emp_salary_audit_trg.
CREATE OR REPLACE PROCEDURE apply_raise (
  p_emp_id     IN  NUMBER,
  p_pct        IN  INTEGER,
  p_new_salary OUT NUMBER
) AS
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
END apply_raise;
/

-- Write one payroll_runs row per active employee for the month containing p_run_date.
-- Pay is pro-rated for mid-month joiners. Employees still in their 6-month probation
-- at month end get no allowance. Returns the number of rows written.
CREATE OR REPLACE PROCEDURE monthly_payroll (
  p_run_date IN  DATE DEFAULT SYSDATE,
  p_rows_out OUT NUMBER
) AS
  v_month_start DATE   := TRUNC(p_run_date, 'MM');
  v_month_end   DATE   := LAST_DAY(TRUNC(p_run_date));
  v_days_in_m   NUMBER := v_month_end - v_month_start + 1;
  v_days        NUMBER;
  v_allow       NUMBER;
  v_gross       NUMBER(12,2);
BEGIN
  p_rows_out := 0;
  FOR e IN (SELECT emp_id, hire_date, base_salary
              FROM employees
             WHERE status = 'ACTIVE'
               AND hire_date <= v_month_end
             ORDER BY emp_id) LOOP
    v_days  := v_month_end - GREATEST(TRUNC(e.hire_date), v_month_start) + 1;
    v_allow := CASE
                 WHEN e.hire_date > ADD_MONTHS(v_month_end, -6) THEN 0
                 ELSE payroll_pkg.c_allowance
               END;
    v_gross := ROUND(e.base_salary / 12 * v_days / v_days_in_m, 2) + v_allow;

    INSERT INTO payroll_runs (run_id, emp_id, pay_month, gross_pay, days_worked, created_at)
    VALUES (payroll_run_seq.NEXTVAL, e.emp_id, v_month_start, v_gross, v_days, p_run_date);

    p_rows_out := p_rows_out + 1;
  END LOOP;
END monthly_payroll;
/
