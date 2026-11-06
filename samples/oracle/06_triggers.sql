-- File 6 of 7: triggers.

-- Audit every salary change. Fires for apply_raise and for direct UPDATEs.
CREATE OR REPLACE TRIGGER emp_salary_audit_trg
AFTER UPDATE OF base_salary ON employees
FOR EACH ROW
WHEN (NEW.base_salary <> OLD.base_salary)
BEGIN
  INSERT INTO salary_audit (audit_id, emp_id, old_salary, new_salary, changed_on, change_type)
  VALUES (salary_audit_seq.NEXTVAL,
          :NEW.emp_id,
          :OLD.base_salary,
          :NEW.base_salary,
          TRUNC(SYSDATE),
          CASE WHEN :NEW.base_salary > :OLD.base_salary THEN 'RAISE' ELSE 'CUT' END);
END emp_salary_audit_trg;
/
