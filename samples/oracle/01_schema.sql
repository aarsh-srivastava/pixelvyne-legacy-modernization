-- PixelVyne sample legacy system: "Employee & Payroll" (Oracle)
-- File 1 of 7: tables and sequences. Plain SQL, each statement ends with ';'.

CREATE TABLE departments (
  dept_id     NUMBER(6)     PRIMARY KEY,
  dept_name   VARCHAR2(60)  NOT NULL
);

CREATE TABLE employees (
  emp_id       NUMBER(6)     PRIMARY KEY,
  full_name    VARCHAR2(100) NOT NULL,
  email        VARCHAR2(100),
  dept_id      NUMBER(6)     REFERENCES departments (dept_id),
  manager_id   NUMBER(6)     REFERENCES employees (emp_id),
  hire_date    DATE          NOT NULL,
  base_salary  NUMBER(10,2)  NOT NULL,
  perf_rating  VARCHAR2(1),                 -- 'A' | 'B' | 'C' | NULL = not yet rated
  status       VARCHAR2(10)  DEFAULT 'ACTIVE' NOT NULL
);

CREATE TABLE payroll_runs (
  run_id       NUMBER        PRIMARY KEY,
  emp_id       NUMBER(6)     REFERENCES employees (emp_id),
  pay_month    DATE          NOT NULL,      -- always the 1st of the month
  gross_pay    NUMBER(12,2),
  days_worked  NUMBER(3),
  created_at   DATE
);

CREATE TABLE salary_audit (
  audit_id     NUMBER        PRIMARY KEY,
  emp_id       NUMBER(6),
  old_salary   NUMBER(10,2),
  new_salary   NUMBER(10,2),
  changed_on   DATE,
  change_type  VARCHAR2(10)
);

CREATE SEQUENCE payroll_run_seq START WITH 1000;

CREATE SEQUENCE salary_audit_seq START WITH 1;
