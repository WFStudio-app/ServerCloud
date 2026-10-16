"""«Генерация ССЛ» (SSLGEN) compute-mode tests."""
import os

from servercloud.modules import compute as cm


def test_parse_spec_named_and_positional():
    p = cm.parse_sslgen_spec("len=16 && charset=mixed && lines=1000000")
    assert p["length"] == 16 and p["charset"] == "mixed"
    assert p["lines"] == 1_000_000
    q = cm.parse_sslgen_spec("8 && letters && 42")
    assert q["length"] == 8 and q["charset"] == "letters" and q["lines"] == 42


def test_parse_spec_rejects_bad_input():
    for bad in ("", "len=5", "len=5 && charset=mixed",
                "len=0 && charset=mixed && lines=5",
                "len=5 && charset=mixed && lines=0",
                "len=5 && charset=mixed && lines=1000000000001",
                "len=5 && charset=symbols && lines=1"):
        try:
            cm.parse_sslgen_spec(bad)
            raise AssertionError(f"should reject: {bad!r}")
        except ValueError:
            pass


def test_trillion_bound_accepted():
    p = cm.parse_sslgen_spec("len=1 && charset=digits && lines=1000000000000")
    assert p["lines"] == 1_000_000_000_000


def test_generate_digits_only(tmp_path):
    eng = cm.ComputeEngine(data_dir=str(tmp_path))
    res = eng.sslgen_generate("len=12 && charset=digits && lines=3000",
                              preview=3)
    assert res["generated"] == 3000 and not res["aborted"]
    with open(res["path"], encoding="utf-8") as f:
        lines = f.read().splitlines()
    assert len(lines) == 3000
    assert all(len(l) == 12 and l.isdigit() for l in lines)
    assert len(res["preview"]) == 3


def test_generate_letters_and_mixed(tmp_path):
    eng = cm.ComputeEngine(data_dir=str(tmp_path))
    r1 = eng.sslgen_generate("len=20 && charset=letters && lines=50")
    with open(r1["path"], encoding="utf-8") as f:
        assert all(l.isalpha() and len(l) == 20 for l in f.read().splitlines())
    r2 = eng.sslgen_generate("len=10 && charset=mixed && lines=500")
    with open(r2["path"], encoding="utf-8") as f:
        ls = f.read().splitlines()
    assert all(len(l) == 10 and (l.isalnum()) for l in ls)


def test_sslgen_stats_and_journal(tmp_path):
    eng = cm.ComputeEngine(data_dir=str(tmp_path))
    eng.sslgen_generate("len=8 && charset=mixed && lines=100")
    s = eng.snapshot()
    assert s["sslgen_runs"] == 1 and s["sslgen_lines"] == 100
    j = eng.recent(5)
    assert any("[SSLGEN]" in line for line in j)
    assert "sslgen" in cm.DEFAULT_MODES and "SSLGEN" in str(cm.MODE_INFO)
