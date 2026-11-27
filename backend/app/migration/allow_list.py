"""The allow-list behind feature detection (PR 1, G3).

Code takes the rules route only if every construct in it is known. This module says what
"known" means:

* ``ALLOWED_FUNCTIONS`` and ``ALLOWED_WORDS``: built-in functions and reserved words that
  the deterministic converter handles. Each entry lists its evidence, and every piece of
  evidence says what kind it is (Aarsh, 19 Nov):

  - ``SAMPLE``: the construct is used in the sample corpus (samples/oracle). It shows the
    converter has met the construct; it does not by itself claim that Oracle and
    PostgreSQL give the same results.
  - ``TRANSLATION_TEST``: a named test asserts the deterministic SQL the converter emits
    for code that uses the construct. It pins the translation; it is not evidence that
    Oracle and PostgreSQL behave the same.
  - ``BEHAVIORAL_TEST``: a test that runs equivalent cases on Oracle and on PostgreSQL and
    compares the results. None exist yet: PR 1b adds them from Oracle-recorded cases.

  test_input_safety_g3.py checks every citation: a SAMPLE construct must occur in the
  samples; a TRANSLATION_TEST must name an existing test that is not skipped, asserts,
  uses the construct, and passes when run. BEHAVIORAL_TEST cannot be claimed until PR 1b.
* ``UNRESOLVED``: allowed entries with a known Oracle/PostgreSQL behaviour difference.
  They are not approved for unrestricted deterministic conversion: routing is unchanged,
  but every use is reported by ``features.semantic_risks`` and the converter flags the
  object for human sign-off with one warning per use (Aarsh, 19 Nov).
* ``ORACLE_RESERVED``: Oracle's reserved words. A reserved word cannot be an unquoted
  identifier, so one that is not allowed is always an Oracle construct, never a column name.
* ``RECOGNISED``: Oracle features we know but have no tested rule for. They go to review
  (Aarsh, 18 Nov: unknown and unsupported code goes to a person first, not to Gemini).

Adding a rule means adding a test and then an entry here, never loosening the check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Evidence kinds (Aarsh, 19 Nov). Translation evidence is never semantic-equivalence evidence.
SAMPLE = "SAMPLE"
TRANSLATION_TEST = "TRANSLATION_TEST"
BEHAVIORAL_TEST = "BEHAVIORAL_TEST"
EVIDENCE_KINDS = (SAMPLE, TRANSLATION_TEST, BEHAVIORAL_TEST)


@dataclass(frozen=True)
class Evidence:
    """One piece of evidence: its kind and where it is.

    ``ref`` is ``"samples/oracle"`` for SAMPLE, ``"tests/<file>.py::<test>"`` for tests.
    """

    kind: str
    ref: str


IN_SAMPLES = Evidence(SAMPLE, "samples/oracle")


def _translation(ref: str) -> Evidence:
    return Evidence(TRANSLATION_TEST, ref)


_PLAIN_SQL = _translation("tests/test_allow_list_evidence.py::test_plain_sql_translates_unchanged")
_SELECT_INTO = _translation("tests/test_plsql_translator.py::test_select_into_translation")

#: Built-in functions the rules convert (Stage 1, R1-R13 plus SQLGlot), with evidence.
ALLOWED_FUNCTIONS: dict[str, tuple[Evidence, ...]] = {
    "ADD_MONTHS": (IN_SAMPLES,),
    "AVG": (_SELECT_INTO,),
    "COALESCE": (_PLAIN_SQL,),
    "COUNT": (_SELECT_INTO, _PLAIN_SQL),
    "DECODE": (_translation("tests/test_sql_translator.py::test_decode_translates_to_case"),),
    # Unresolved NULL semantics: see UNRESOLVED below.
    "GREATEST": (IN_SAMPLES, _translation("tests/test_sql_translator.py::test_date_minus_date_and_date_plus_days")),
    "LAST_DAY": (IN_SAMPLES,),
    "LOWER": (_PLAIN_SQL,),
    "MAX": (_SELECT_INTO,),
    "MIN": (_SELECT_INTO,),
    "NVL": (IN_SAMPLES, _translation("tests/test_sql_translator.py::test_nvl_translates_to_coalesce")),
    "ROUND": (IN_SAMPLES,),
    "SUM": (IN_SAMPLES,),
    "TRUNC": (IN_SAMPLES, _translation("tests/test_sql_translator.py::test_trunc_on_dates_numbers_and_unknowns")),
    "UPPER": (_PLAIN_SQL,),
}

#: Reserved words the rules handle, with evidence.
ALLOWED_WORDS: dict[str, tuple[Evidence, ...]] = {
    **dict.fromkeys(
        (
            "AND AS BEGIN BY CASE CONNECT CREATE DATE DEFAULT DESC ELSE END EXCEPTION FOR FROM "
            "FUNCTION IF IN INSERT INTEGER INTO IS LEVEL NOT NULL NUMBER OF ON OR ORDER PRIOR "
            "PROCEDURE ROW ROWNUM SELECT SET START SYSDATE THEN TRIGGER TYPE UPDATE VALUES "
            "VARCHAR2 VIEW WHEN WHERE WITH"
        ).split(),
        (IN_SAMPLES,),
    ),
    **dict.fromkeys("ALL ANY ASC BETWEEN DISTINCT EXISTS GROUP HAVING LIKE UNION".split(), (_PLAIN_SQL,)),
    "CHAR": (_translation("tests/test_plsql_translator.py::test_translate_type"),),
    "DECLARE": (_translation("tests/test_plsql_translator.py::test_loops_case_and_nested_blocks"),),
    "DELETE": (
        _translation("tests/test_plsql_translator.py::test_comments_in_into_targets_and_trigger_events_are_dropped"),
    ),
}

#: Allowed entries whose Oracle and PostgreSQL behaviour is known to differ: name -> the
#: difference, as shown in the warning. They are not approved for unrestricted
#: deterministic conversion; each use is flagged for human sign-off. An entry leaves this
#: list only with a fixed rule and BEHAVIORAL_TEST evidence.
UNRESOLVED: dict[str, str] = {
    # The evidence covers usage and translation output only. Unresolved until a NULL-safe
    # rule (PR 1b, R14) and behavioural tests are added.
    "GREATEST": "Oracle returns NULL when any argument is NULL; PostgreSQL ignores NULL arguments.",
}

#: Oracle SQL and PL/SQL reserved words (V$RESERVED_WORDS with RESERVED = 'Y', plus the
#: PL/SQL reserved words). None of them can be an unquoted identifier.
ORACLE_RESERVED: frozenset[str] = frozenset(
    (
        "ACCESS ADD ALL ALTER AND ANY AS ASC AT AUDIT BEGIN BETWEEN BY CASE CHAR CHECK CLUSTER "
        "CLUSTERS COLAUTH COLUMN COLUMNS COMMENT COMPRESS CONNECT CRASH CREATE CURRENT CURSOR "
        "DATE DECIMAL DECLARE DEFAULT DELETE DESC DISTINCT DROP ELSE END EXCEPTION EXCLUSIVE "
        "EXISTS FETCH FILE FLOAT FOR FROM FUNCTION GOTO GRANT GROUP HAVING IDENTIFIED IF "
        "IMMEDIATE IN INCREMENT INDEX INDEXES INITIAL INSERT INTEGER INTERSECT INTO IS LEVEL "
        "LIKE LOCK LONG MAXEXTENTS MINUS MLSLABEL MODE MODIFY NOAUDIT NOCOMPRESS NOT NOWAIT "
        "NULL NUMBER OF OFFLINE ON ONLINE OPTION OR ORDER OVERLAPS PCTFREE PRIOR PROCEDURE "
        "PUBLIC RAW RENAME RESOURCE REVOKE ROW ROWID ROWNUM ROWS SELECT SESSION SET SHARE "
        "SIZE SMALLINT SQL START SUBTYPE SUCCESSFUL SYNONYM SYSDATE TABAUTH TABLE THEN TO "
        "TRIGGER TYPE UID UNION UNIQUE UPDATE USER VALIDATE VALUES VARCHAR VARCHAR2 VIEW "
        "VIEWS WHEN WHENEVER WHERE WITH"
    ).split()
)

#: Words that are syntax, not calls, when followed by "(" (non-reserved keywords included).
SYNTAX_WORDS: frozenset[str] = ORACLE_RESERVED | frozenset(
    (
        "OVER PARTITION USING RETURN RETURNING EXIT LOOP WHILE ELSIF OUT NOCOPY REPLACE EACH "
        "BEFORE AFTER INSTEAD REFERENCING NEW OLD RAISE PRAGMA OPEN CLOSE LIMIT BULK COLLECT "
        "FORALL CONTINUE MERGE MATCHED KEY PRIMARY FOREIGN REFERENCES CONSTRAINT CHAR "
        "NVARCHAR2 NCHAR CLOB BLOB TIMESTAMP INTERVAL BOOLEAN PLS_INTEGER BINARY_INTEGER "
        "NATURAL POSITIVE SIMPLE_INTEGER BINARY_FLOAT BINARY_DOUBLE RECORD VARRAY CAST TABLE "
        "JOIN INNER OUTER LEFT RIGHT FULL CROSS LATERAL FETCH FIRST NEXT ONLY OFFSET NULLS "
        "LAST WITHIN"
    ).split()
)

#: Oracle features we recognise but have no tested rule for: (feature, pattern, detail).
RECOGNISED: list[tuple[str, re.Pattern, str]] = [
    ("PIVOT", re.compile(r"\b(?:UN)?PIVOT\s*(?:XML\s*|(?:INCLUDE|EXCLUDE)\s+NULLS\s*)?\(", re.I),
     "PIVOT / UNPIVOT has no tested rule"),
    ("MODEL", re.compile(
        r"\bMODEL\s+(?:RETURN\s+(?:UPDATED|ALL)\s+ROWS|PARTITION\s+BY|DIMENSION\s+BY|MEASURES|"
        r"(?:IGNORE|KEEP)\s+NAV|UNIQUE\s+(?:DIMENSION|SINGLE\s+REFERENCE)|REFERENCE)\b", re.I),
     "MODEL clause has no tested rule"),
    ("FLASHBACK", re.compile(r"\bAS\s+OF\s+(?:TIMESTAMP|SCN)\b|\bVERSIONS\s+BETWEEN\b", re.I),
     "flashback query (AS OF / VERSIONS BETWEEN) has no tested rule"),
    ("PIPELINED", re.compile(r"\bPIPELINED\b|\bPIPE\s+ROW\b", re.I),
     "pipelined function (PIPELINED / PIPE ROW) has no tested rule"),
    ("XML", re.compile(
        r"\bXMLTYPE\b|\bXML(?:ELEMENT|AGG|FOREST|ATTRIBUTES|SERIALIZE|QUERY|TABLE|EXISTS|CAST|PARSE|"
        r"ROOT|CONCAT|SEQUENCE)\s*\(|\bEXTRACTVALUE\s*\(", re.I),
     "XML types and functions have no tested rule"),
    ("GOTO", re.compile(r"\bGOTO\b", re.I), "GOTO has no tested rule"),
    ("ROWID", re.compile(r"\bU?ROWID\b", re.I), "ROWID has no tested rule (no PostgreSQL equivalent)"),
    ("IMPLICIT_CURSOR_ATTRIBUTE", re.compile(
        r"\bSQL\s*%\s*(?:ROWCOUNT|FOUND|NOTFOUND|ISOPEN|BULK_ROWCOUNT|BULK_EXCEPTIONS)\b", re.I),
     "SQL% cursor attribute has no tested rule"),
    ("MINUS", re.compile(r"\bMINUS\b", re.I), "MINUS (EXCEPT in PostgreSQL) has no tested rule"),
    ("INTERSECT", re.compile(r"\bINTERSECT\b", re.I), "INTERSECT has no tested rule"),
    ("MATCH_RECOGNIZE", re.compile(r"\bMATCH_RECOGNIZE\s*\(", re.I), "MATCH_RECOGNIZE has no tested rule"),
    ("KEEP_DENSE_RANK", re.compile(r"\bKEEP\s*\(\s*DENSE_RANK\b", re.I), "KEEP (DENSE_RANK ...) has no tested rule"),
    ("LOG_ERRORS", re.compile(r"\bLOG\s+ERRORS\b", re.I), "DML error logging (LOG ERRORS) has no tested rule"),
]
