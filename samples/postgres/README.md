# Hand-written PostgreSQL stand-in

Until the real converter exists, these files play its role so the validation
harness can be built and demoed. They are a deliberately **literal** conversion of
`samples/oracle/`: the kind of output a careless tool produces. They compile and run,
but keep the Oracle traps that `EXPECTED.md` documents:

| Object | Tests that should fail | Trap |
| --- | --- | --- |
| calc_bonus | cb-02, cb-06 | `''` is NULL only in Oracle; no `STRICT` |
| get_emp_grade | gg-05 | no `STRICT`, so the NO_DATA_FOUND handler never fires |
| top_earners | te-01, te-02, te-03, te-05 | `NULL \|\| text` is NULL in PostgreSQL |
| apply_raise | ar-01, ar-03 | integer division; no `STRICT` |

The trigger `emp_salary_audit_trg` is converted correctly, so its side effects match.
The harness should catch every trap above and nothing else. Load or reset them with:

    python scripts/seed_target.py

Remove this folder once the converter writes real output into `target-db`.
