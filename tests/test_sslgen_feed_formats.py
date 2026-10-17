"""SSLGEN live [OK]/[!] verification feed + output file formats."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from servercloud.modules import compute as cm


def _engine(tmp):
    return cm.ComputeEngine(data_dir=tmp)


def test_spec_accepts_format_and_file():
    p = cm.parse_sslgen_spec("len=8 && charset=digits && lines=10 "
                             "&& format=json && file=/tmp/x.json")
    assert p["fmt"] == "json" and p["path"] == "/tmp/x.json"
    # positional shorthand still works, format bare token allowed
    p2 = cm.parse_sslgen_spec("6 && letters && 5 && csv")
    assert p2["fmt"] == "csv" and p2["length"] == 6
    try:
        cm.parse_sslgen_spec("len=8 && charset=digits && lines=10 "
                             "&& format=parquet")
        raise AssertionError("bad format must be rejected")
    except ValueError:
        pass


def test_live_feed_ok_lines_have_flag_code_path_line():
    with tempfile.TemporaryDirectory() as tmp:
        eng = _engine(tmp)
        feed = []
        res = eng.sslgen_generate(
            "len=13 && charset=mixed && lines=7",
            line_logger=feed.append)
        assert res["generated"] == 7 and res["rejected"] == 0
        oks = [f for f in feed if f.startswith("[OK]")]
        assert len(oks) == 7
        for i, ln in enumerate(oks, 1):
            code = ln.split()[1]
            assert len(code) == 13
            assert ln.endswith(f"{res['path']}:{i}") or f":{i}" in ln
        assert not any(f.startswith("[!]") for f in feed)


def test_all_formats_produce_valid_files():
    fmts = ["txt", "csv", "tsv", "json", "jsonl", "yaml", "xml", "html",
            "md", "sql", "ini", "env", "b64", "hexdump", "sha256", "kv"]
    with tempfile.TemporaryDirectory() as tmp:
        eng = _engine(tmp)
        for fmt in fmts:
            dst = os.path.join(tmp, f"out.{fmt}")
            res = eng.sslgen_generate(
                f"len=10 && charset=lower && lines=4 && format={fmt}",
                dst=dst)
            assert res["format"] == fmt
            body = open(dst, encoding="utf-8").read()
            assert body.strip(), fmt
            if fmt == "json":
                data = json.loads(body)
                assert isinstance(data, list) and len(data) == 4
                assert all(len(d["value"]) == 10 for d in data)
            elif fmt == "jsonl":
                rows = [json.loads(l) for l in body.splitlines()]
                assert len(rows) == 4
            elif fmt == "xml":
                import xml.etree.ElementTree as ET
                root = ET.fromstring(body)
                assert len(root.findall("line")) == 4
            elif fmt == "html":
                assert "<ul>" in body and body.count("<li>") == 4 \
                    and "</html>" in body
            elif fmt == "csv":
                assert body.splitlines()[0].startswith("1,")
            elif fmt == "b64":
                import base64
                first = body.splitlines()[0]
                assert base64.b64decode(first).decode() .isalpha()
            elif fmt == "sha256":
                assert len(body.split()[0]) == 64


def test_writer_selfcheck_rejects_bad_line(monkeypatch=None):
    """Force a corrupt generation to prove the [!] branch works."""
    with tempfile.TemporaryDirectory() as tmp:
        eng = _engine(tmp)
        orig = cm.secrets.choice
        calls = {"n": 0}

        def bad_choice(seq):
            calls["n"] += 1
            if calls["n"] % 17 == 0:
                return "@"          # not in alphabet -> self-check fails
            return orig(seq)
        cm.secrets.choice = bad_choice
        try:
            feed = []
            res = eng.sslgen_generate(
                "len=8 && charset=lower && lines=40",
                line_logger=feed.append)
        finally:
            cm.secrets.choice = orig
        assert res["rejected"] >= 1
        bad = [f for f in feed if f.startswith("[!]")]
        assert bad and all("skipped" in b for b in bad)
        assert res["generated"] + res["rejected"] == res["attempted"]
        assert res["generated"] == len(feed) - len(bad)
        # every written line is exactly 8 lowercase letters
        with open(res["path"], encoding="utf-8") as f:
            for ln in f:
                s = ln.rstrip("\n")
                assert len(s) == 8 and s.islower()


if __name__ == "__main__":
    test_spec_accepts_format_and_file()
    test_live_feed_ok_lines_have_flag_code_path_line()
    test_all_formats_produce_valid_files()
    test_writer_selfcheck_rejects_bad_line()
    print("all sslgen feed/format tests passed")
