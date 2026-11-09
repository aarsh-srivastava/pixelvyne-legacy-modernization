# Hand-written PostgreSQL stand-in

Until the real converter exists, these files play its role so the validation
harness can be built and demoed. They are a deliberately **literal** conversion of
`samples/oracle/`: the kind of output a careless tool produces. They compile and run,
but keep two Oracle traps that `EXPECTED.md` documents:

* cb-02: `''` is NULL in Oracle but a real value in PostgreSQL.
* cb-06: PL/pgSQL `SELECT INTO` without `STRICT` does not raise on zero rows.

The harness should catch both. Load or reset them with:

    python scripts/seed_target.py

Remove this folder once the converter writes real output into `target-db`.
