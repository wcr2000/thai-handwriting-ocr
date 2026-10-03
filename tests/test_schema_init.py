"""init_schema has to survive the connection dropping partway through.

Postgres on Render cuts the line periodically. The previous version ran the whole of schema.sql
in a single transaction, so one dropped connection rolled back everything — which happened for
real while adding an index.
"""

from ocrslip.db import SCHEMA_SQL, split_sql


def test_does_not_cut_inside_function_body():
    """The body of touch_updated_at() contains a ';', so splitting naively on ';' cuts the function in half"""
    stmts = split_sql(SCHEMA_SQL.read_text(encoding="utf-8"))
    fn = [s for s in stmts if s.startswith("CREATE OR REPLACE FUNCTION")]
    assert len(fn) == 1
    assert "RETURN NEW;" in fn[0] and "LANGUAGE plpgsql" in fn[0]
    # No other statement is left with a stray ';' inside it
    assert [s for s in stmts if ";" in s] == fn


def test_splits_whole_file_into_runnable_statements():
    stmts = split_sql(SCHEMA_SQL.read_text(encoding="utf-8"))
    assert len(stmts) > 25, "implausibly few — the split probably went wrong"
    assert not any(s.strip().startswith("--") for s in stmts), "a comment must not become a statement"
    assert any(s.startswith("CREATE SCHEMA") for s in stmts)
    assert any("slips_review_recent_idx" in s for s in stmts)
    assert any(s.startswith("CREATE TRIGGER") for s in stmts)


def test_keeps_semicolons_and_dollar_quotes_inside_strings():
    sql = """
    -- a comment; containing a semicolon
    SELECT 'ข้อความ; มี semicolon' AS a;
    DO $tag$ BEGIN PERFORM 1; END $tag$;
    SELECT 'escaped '' quote; ok';
    """
    stmts = split_sql(sql)
    assert len(stmts) == 3, stmts
    assert stmts[0] == "SELECT 'ข้อความ; มี semicolon' AS a"
    assert stmts[1].startswith("DO $tag$") and stmts[1].endswith("$tag$")
    assert stmts[2] == "SELECT 'escaped '' quote; ok'"
