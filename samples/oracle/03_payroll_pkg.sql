-- File 3 of 7: payroll_pkg. Loaded before the procedures because monthly_payroll
-- reads payroll_pkg.c_allowance (a cross-object dependency for the dependency graph).
-- Each PL/SQL block ends with a line containing only '/'.

CREATE OR REPLACE PACKAGE payroll_pkg AS
  c_allowance CONSTANT NUMBER := 2500;

  FUNCTION net_pay (p_gross IN NUMBER) RETURN NUMBER;

  PROCEDURE close_month (p_pay_month IN DATE, p_total OUT NUMBER);
END payroll_pkg;
/

CREATE OR REPLACE PACKAGE BODY payroll_pkg AS
  g_tax_rate NUMBER := 0.10;   -- package-level state: PostgreSQL has no equivalent

  FUNCTION net_pay (p_gross IN NUMBER) RETURN NUMBER IS
  BEGIN
    RETURN ROUND(NVL(p_gross, 0) * (1 - g_tax_rate), 2);
  END net_pay;

  PROCEDURE close_month (p_pay_month IN DATE, p_total OUT NUMBER) IS
  BEGIN
    SELECT NVL(SUM(net_pay(gross_pay)), 0)
      INTO p_total
      FROM payroll_runs
     WHERE pay_month = TRUNC(p_pay_month, 'MM');
  END close_month;
END payroll_pkg;
/
