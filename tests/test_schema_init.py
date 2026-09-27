"""init_schema ต้องทนต่อ connection หลุดกลางทาง

Postgres ฝั่ง Render ตัดสายเป็นระยะ ของเดิมยิง schema.sql ทั้งไฟล์ใน transaction เดียว
สายหลุดครั้งเดียวคือ rollback ทั้งก้อน — เคยเกิดจริงตอนเพิ่ม index
"""

from ocrslip.db import SCHEMA_SQL, split_sql


def test_does_not_cut_inside_function_body():
    """body ของ touch_updated_at() มี ';' อยู่ข้างใน ถ้า split ด้วย ';' เฉย ๆ function จะขาด"""
    stmts = split_sql(SCHEMA_SQL.read_text(encoding="utf-8"))
    fn = [s for s in stmts if s.startswith("CREATE OR REPLACE FUNCTION")]
    assert len(fn) == 1
    assert "RETURN NEW;" in fn[0] and "LANGUAGE plpgsql" in fn[0]
    # ไม่มีคำสั่งอื่นที่เหลือ ';' ค้างอยู่ข้างใน
    assert [s for s in stmts if ";" in s] == fn


def test_splits_whole_file_into_runnable_statements():
    stmts = split_sql(SCHEMA_SQL.read_text(encoding="utf-8"))
    assert len(stmts) > 25, "น้อยเกินจริง น่าจะตัดพลาด"
    assert not any(s.strip().startswith("--") for s in stmts), "comment ไม่ควรกลายเป็นคำสั่ง"
    assert any(s.startswith("CREATE SCHEMA") for s in stmts)
    assert any("slips_review_recent_idx" in s for s in stmts)
    assert any(s.startswith("CREATE TRIGGER") for s in stmts)


def test_keeps_semicolons_and_dollar_quotes_inside_strings():
    sql = """
    -- comment; ที่มี semicolon
    SELECT 'ข้อความ; มี semicolon' AS a;
    DO $tag$ BEGIN PERFORM 1; END $tag$;
    SELECT 'escaped '' quote; ok';
    """
    stmts = split_sql(sql)
    assert len(stmts) == 3, stmts
    assert stmts[0] == "SELECT 'ข้อความ; มี semicolon' AS a"
    assert stmts[1].startswith("DO $tag$") and stmts[1].endswith("$tag$")
    assert stmts[2] == "SELECT 'escaped '' quote; ok'"
