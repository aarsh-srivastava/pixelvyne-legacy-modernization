# Expected behaviour: Employee & Payroll sample system

This file is the "functional responsibility" input for the platform and the golden
answer key for validation. Each object lists what it is supposed to do, golden test
cases with the result Oracle should produce, and the migration trap it carries.

The golden values below were computed by hand from Oracle semantics (decimal
arithmetic, ROUND half away from zero). **The first real Oracle run is the
authority:** if the harness disagrees with this file, trust the Oracle run and fix
the file.

All cases assume the seed data in `02_seed_data.sql`, freshly loaded, and run inside
a transaction that is rolled back afterwards.

---

## 1. `calc_bonus` (procedure)

**Responsibility:** annual bonus for one employee. Rating A = 20% of salary, B = 10%,
C = 5%, unrated = 0. `p_override` replaces the stored rating; NULL or empty string
means "use the stored rating".

**Signature:** `calc_bonus(p_emp_id IN NUMBER, p_override IN VARCHAR2, p_bonus OUT NUMBER)`

| Test | p_emp_id | p_override | Oracle p_bonus | Why it exists |
| --- | --- | --- | --- | --- |
| cb-01 | 102 | NULL | 24000.00 | Happy path, rating A |
| cb-02 | 102 | `''` | 24000.00 | **Trap.** Oracle treats `''` as NULL |
| cb-03 | 103 | `'A'` | 19600.00 | Override applied |
| cb-04 | 106 | NULL | 0 | Unrated employee |
| cb-05 | 105 | NULL | 4250.03 | 4250.025 rounds half up |
| cb-06 | 999 | NULL | error ORA-01403 (no data found) | Unhandled missing row |

**Trap:** in PostgreSQL `''` is a real value, not NULL. A literal conversion runs the
override branch with `v_rating = ''`, which matches no rating and falls to ELSE, so
**cb-02 returns 6000.00 instead of 24000.00**. Also, PL/pgSQL `SELECT INTO` without
`STRICT` does not raise on zero rows, so **cb-06 returns 0 instead of raising**.

---

## 2. `get_emp_grade` (function)

**Responsibility:** grade label `G<n>-<band>`. Rating A/B/C → G1/G2/G3, unrated →
UNRATED. Band SENIOR if salary ≥ 100000, else STANDARD. Unknown employee → NULL.

| Test | p_emp_id | Oracle return | Why it exists |
| --- | --- | --- | --- |
| gg-01 | 101 | `G1-SENIOR` | Happy path |
| gg-02 | 106 | `UNRATED-STANDARD` | NVL on NULL rating |
| gg-03 | 111 | `G3-SENIOR` | Boundary: 101000.75 ≥ 100000 |
| gg-04 | 104 | `G2-STANDARD` | |
| gg-05 | 999 | NULL | NO_DATA_FOUND handler |

**Trap:** `DECODE` and `NVL` need rewriting (`CASE`, `COALESCE`). Without `STRICT`,
**gg-05 returns `G4-STANDARD` instead of NULL** because the handler never fires.

---

## 3. `top_earners` (function)

**Responsibility:** comma-separated names of the top N active earners in a
department, highest first, salary ties broken by lower emp_id. No match → NULL.

| Test | p_dept_id | p_n | Oracle return | Why it exists |
| --- | --- | --- | --- | --- |
| te-01 | 20 | 2 | `Rohan Mehta, Karan Singh` | Tie (104/105 both 85000.50) broken by emp_id |
| te-02 | 30 | 1 | `Ira Kapoor` | Single result |
| te-03 | 30 | 10 | `Ira Kapoor, Priya Nair, Sana Khan, Vikram Rao` | N larger than the department |
| te-04 | 40 | 3 | NULL | Empty department |
| te-05 | 10 | 5 | `Asha Verma` | Inactive employee 110 excluded |

**Trap:** Oracle ignores NULL in `||`; PostgreSQL makes the whole expression NULL.
`v_list` starts NULL and the `CASE` has no ELSE, so a literal conversion **returns
NULL for every test**. `ROWNUM` must also become `LIMIT`.

---

## 4. `apply_raise` (procedure, writes data)

**Responsibility:** raise one employee's salary by a whole-number percentage, round to
2 decimals, return the new salary. Unknown employee → -1, nothing changes. The UPDATE
fires `emp_salary_audit_trg`.

| Test | p_emp_id | p_pct | Oracle p_new_salary | Expected side effects |
| --- | --- | --- | --- | --- |
| ar-01 | 104 | 7 | 90950.54 | employees 104 salary 85000.50 → 90950.54; salary_audit +1 row (`RAISE`) |
| ar-02 | 106 | 0 | 64000 | Salary unchanged; **no** audit row (trigger WHEN clause) |
| ar-03 | 999 | 5 | -1 | No rows changed anywhere |

**Traps:** if `p_pct` becomes a PostgreSQL `integer`, `7 / 100` is integer division
= 0, so **ar-01 returns 85000.50 and writes no audit row**. Without `STRICT`, **ar-03
returns NULL instead of -1**.

---

## 5. `monthly_payroll` (procedure, writes data)

**Responsibility:** one `payroll_runs` row per active employee hired on or before
month end, for the month containing `p_run_date`. Pay = salary / 12, pro-rated by days
worked, rounded to 2 decimals, plus `payroll_pkg.c_allowance` (2500) unless the
employee was hired after `ADD_MONTHS(month_end, -6)` (still on probation). Returns the
row count. `created_at` = `p_run_date`.

**Golden test mp-01:** `p_run_date = DATE '2026-09-15'` → `p_rows_out = 10`, and these
`payroll_runs` rows (all `pay_month = 2026-09-01`; `run_id` values come from a sequence
and are masked when comparing):

| emp_id | days_worked | gross_pay | Note |
| --- | --- | --- | --- |
| 101 | 30 | 22500.00 | |
| 102 | 30 | 12500.00 | |
| 103 | 30 | 10666.67 | |
| 104 | 30 | 9583.38 | 7083.375 rounds half up |
| 105 | 30 | 9583.38 | |
| 106 | 30 | 7833.33 | |
| 107 | 30 | 8500.00 | |
| 108 | 30 | 7166.67 | **Trap row**, see below |
| 109 | 14 | 1944.44 | Joined 17 Sep, on probation |
| 111 | 30 | 10916.73 | |

Employee 110 is INACTIVE and gets no row.

**Traps:**
- `ADD_MONTHS` keeps month-end on month-end: `ADD_MONTHS(30-Sep-2026, -6)` =
  **31-Mar-2026**. PostgreSQL `date '2026-09-30' - interval '6 months'` =
  **30-Mar-2026**. Arjun (108, hired 31-Mar-2026) is off probation in Oracle and on
  probation in a literal conversion, so **his row shows 4666.67 instead of 7166.67**.
- Oracle `DATE - DATE` returns a number of days. If DATE is mapped to `timestamp`,
  PostgreSQL returns an `interval`, and `interval + 1` fails.
- `TRUNC(date, 'MM')`, `LAST_DAY`, `SYSDATE` as a default, `sequence.NEXTVAL`, and the
  reference to `payroll_pkg.c_allowance` all need conversion.

---

## 6. `payroll_pkg` (package)

**Responsibility:** `net_pay(gross)` = gross × (1 − 10% tax), rounded to 2 decimals,
NULL treated as 0. `close_month(month, total OUT)` = sum of `net_pay` over that month's
`payroll_runs` rows. `c_allowance` = 2500, used by `monthly_payroll`.

| Test | Call | Oracle result |
| --- | --- | --- |
| pk-01 | `net_pay(10000)` | 9000.00 |
| pk-02 | `net_pay(NULL)` | 0 |
| pk-03 | `net_pay(9583.38)` | 8625.04 |
| pk-04 | run `monthly_payroll(DATE '2026-09-15')`, then `close_month(DATE '2026-09-15')` | 91075.14 |
| pk-05 | `close_month(DATE '2026-10-01')` with no runs | 0 |

**Trap:** PostgreSQL has no packages. The package becomes a schema of functions, the
constant must be preserved for `monthly_payroll`, and the package variable
`g_tax_rate` has no direct equivalent.

---

## 7. `emp_salary_audit_trg` (trigger)

**Responsibility:** after any salary UPDATE that changes the value, insert one
`salary_audit` row with old/new salary, today's date and `RAISE` or `CUT`.

| Test | Action | Expected salary_audit change |
| --- | --- | --- |
| tr-01 | `UPDATE employees SET base_salary = 70000 WHERE emp_id = 106` | +1 row: 106, 64000 → 70000, `RAISE` |
| tr-02 | `UPDATE employees SET base_salary = 60000 WHERE emp_id = 106` | +1 row: `CUT` |
| tr-03 | `UPDATE employees SET base_salary = base_salary WHERE emp_id = 106` | no row |

`audit_id` and `changed_on` (SYSDATE) are masked when comparing.

**Trap:** PostgreSQL needs a separate trigger function; `:NEW`/`:OLD` become
`NEW`/`OLD`; the WHEN clause and the sequence call must be preserved.

---

## 8. `org_chart` (view)

**Responsibility:** every employee reachable from the CEO, with depth (CEO = 1) and the
full reporting line from the CEO, each name prefixed by `' > '`.

**Golden test oc-01:** `SELECT * FROM org_chart ORDER BY emp_id` returns 11 rows,
including:

| emp_id | depth | reporting_line |
| --- | --- | --- |
| 101 | 1 | ` > Asha Verma` |
| 102 | 2 | ` > Asha Verma > Rohan Mehta` |
| 104 | 3 | ` > Asha Verma > Rohan Mehta > Karan Singh` |
| 108 | 4 | ` > Asha Verma > Rohan Mehta > Karan Singh > Arjun Das` |
| 110 | 2 | ` > Asha Verma > Dev Malhotra` (inactive employees are included) |

**Trap:** `CONNECT BY`, `LEVEL` and `SYS_CONNECT_BY_PATH` have no PostgreSQL
equivalent; the view must become a recursive CTE that reproduces the exact path
string, including the leading `' > '`.

---

## Summary: what a literal conversion gets wrong

| Object | Failing tests | Root cause |
| --- | --- | --- |
| calc_bonus | cb-02, cb-06 | `''` vs NULL; no `STRICT` |
| get_emp_grade | gg-05 | no `STRICT` |
| top_earners | all | NULL in `\|\|` |
| apply_raise | ar-01, ar-03 | integer division; no `STRICT` |
| monthly_payroll | mp-01 (row 108) | `ADD_MONTHS` month-end rule |
| payroll_pkg | pk-04 (via mp-01) | inherits the payroll difference |
| emp_salary_audit_trg | none expected if converted correctly | structural rewrite only |
| org_chart | none expected if converted correctly | structural rewrite only |

Two objects deliberately have no hidden semantic trap, so the demo also shows clean
passes, not only failures.
