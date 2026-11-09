-- Literal conversion of samples/oracle/01_schema.sql.
-- NUMBER -> numeric, VARCHAR2 -> varchar, Oracle DATE (has a time part) -> timestamp(0).

CREATE TABLE departments (
  dept_id     numeric(6)    PRIMARY KEY,
  dept_name   varchar(60)   NOT NULL
);

CREATE TABLE employees (
  emp_id       numeric(6)    PRIMARY KEY,
  full_name    varchar(100)  NOT NULL,
  email        varchar(100),
  dept_id      numeric(6)    REFERENCES departments (dept_id),
  manager_id   numeric(6)    REFERENCES employees (emp_id),
  hire_date    timestamp(0)  NOT NULL,
  base_salary  numeric(10,2) NOT NULL,
  perf_rating  varchar(1),
  status       varchar(10)   DEFAULT 'ACTIVE' NOT NULL
);

CREATE TABLE payroll_runs (
  run_id       numeric       PRIMARY KEY,
  emp_id       numeric(6)    REFERENCES employees (emp_id),
  pay_month    timestamp(0)  NOT NULL,
  gross_pay    numeric(12,2),
  days_worked  numeric(3),
  created_at   timestamp(0)
);

CREATE TABLE salary_audit (
  audit_id     numeric       PRIMARY KEY,
  emp_id       numeric(6),
  old_salary   numeric(10,2),
  new_salary   numeric(10,2),
  changed_on   timestamp(0),
  change_type  varchar(10)
);

CREATE SEQUENCE payroll_run_seq START WITH 1000;
CREATE SEQUENCE salary_audit_seq START WITH 1;
