# Prepared repairs for the scripted stand-in converter

Used only by `ScriptedConverter` (backend/app/orchestration/scripted_converter.py)
until the real converter exists. Attempt 1 is the careless conversion that
`scripts/seed_target.py` deploys; each file here is one later attempt, in order:

| Object | Attempt 2 | Attempt 3 |
| --- | --- | --- |
| calc_bonus | `calc_bonus.1.sql`: NULLIF(p_override, ''), SELECT INTO STRICT | |
| get_emp_grade | `get_emp_grade.1.sql`: SELECT INTO STRICT | |
| top_earners | `top_earners.1.sql`: build the list without NULL concatenation | |
| apply_raise | `apply_raise.1.sql`: 100.0 instead of integer 100 | `apply_raise.2.sql`: also SELECT INTO STRICT |

apply_raise is deliberately repaired in two steps so the demo shows the loop going
round more than once. Run `python scripts/seed_target.py` to reset the target database.
