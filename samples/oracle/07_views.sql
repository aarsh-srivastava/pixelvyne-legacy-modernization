-- File 7 of 7: views. Plain SQL, ends with ';'.

-- Reporting hierarchy from the CEO down, with each person's full reporting line.
CREATE OR REPLACE VIEW org_chart AS
SELECT emp_id,
       full_name,
       manager_id,
       LEVEL                                AS depth,
       SYS_CONNECT_BY_PATH(full_name, ' > ') AS reporting_line
  FROM employees
 START WITH manager_id IS NULL
CONNECT BY PRIOR emp_id = manager_id;
