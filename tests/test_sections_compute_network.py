"""Tests for the «Вычисления» and «Сеть» sections + live monitor wiring."""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from servercloud.core.jobs import Manager                     # noqa: E402
from servercloud.modules.compute import ComputeEngine         # noqa: E402
from servercloud.modules.netkit import NetworkKit             # noqa: E402
from servercloud.modules.monitor import LiveMonitor           # noqa: E402
from servercloud.modules.keycipher import mask_key, MASK_LEN  # noqa: E402


def test_quantize_roundtrip(tmp_path):
    eng = ComputeEngine(data_dir=str(tmp_path))
    src = tmp_path / "payload.bin"
    data = bytes(range(256)) * 8
    src.write_bytes(data)
    line = eng.quantize_file(str(src), fmt="q8_0")
    assert "QUANTIZE" in line
    qf = str(src) + ".q8_0"
    assert os.path.exists(qf)
    eng.dequantize_file(qf)
    out = open(str(src).rsplit(".", 1)[0] + ".deq", "rb").read()
    assert len(out) == len(data)
    # q8_0 is near-lossless: mean abs error must be tiny
    mae = sum(abs(a - b) for a, b in zip(data, out)) / len(data)
    assert mae < 3.0
    s = eng.snapshot()
    assert s["quantized"] == 1 and s["jobs"] == 2


def test_compress_roundtrip_lossless(tmp_path):
    eng = ComputeEngine(data_dir=str(tmp_path))
    src = tmp_path / "log.txt"
    src.write_bytes(b"aaaaabbbbbccccc" * 500)
    eng.compress_file(str(src))
    dst = str(src) + ".trkz"
    assert os.path.exists(dst)
    assert os.path.getsize(dst) < os.path.getsize(str(src))
    eng.decompress_file(dst, dst=str(tmp_path / "restored.txt"))
    assert open(str(tmp_path / "restored.txt"), "rb").read() == \
        src.read_bytes()


def test_compute_modes_and_journal(tmp_path):
    eng = ComputeEngine(data_dir=str(tmp_path))
    assert "turbo" in eng.modes() and "cluster" in eng.modes()
    src = tmp_path / "x.bin"
    src.write_bytes(b"\x00\x01" * 100)
    eng.compress_file(str(src), mode="turbo")
    j = eng.recent(5)
    assert j and "mode=turbo" in j[-1]
    assert eng._device_for("gpu") in ("gpu", "cpu (gpu disabled in BIOS)")


def test_netkit_dns_proxy_vault(tmp_path):
    nk = NetworkKit(str(tmp_path))
    nk.dns_add("ollama.internal", "10.0.0.5", "A", 60)
    rec = nk.dns_lookup("ollama.internal")
    assert rec and rec["value"] == "10.0.0.5"
    nk.proxy_add(8800, "10.0.0.9", 11434, "")
    assert nk.proxy_check(8800, "anyone") is True
    bad_fp = "TRK-" + "X" * 31
    nk.proxy_del(8800)
    nk.proxy_add(8801, "h", 80, bad_fp)
    assert nk.proxy_check(8801, "intruder") is False
    assert nk.proxy_check(8801, bad_fp) is True
    f = tmp_path / "model.part"
    f.write_bytes(b"z" * 4096)
    line = nk.vault_store(str(f), server="srv1")
    assert "VAULT" in line and "sha=" in line
    assert nk.vault_verify(str(f)) is True
    f.write_bytes(b"tampered!")
    assert nk.vault_verify(str(f)) is False
    snap = nk.snapshot()
    assert snap["dns"] == 1 and snap["vault"] == 1
    assert len(snap["identity"]) == MASK_LEN   # node key shown masked/35


def test_key_cipher_masks_to_35():
    fp = mask_key("sk-supersecret-openai-style-key-12345")
    assert len(fp) == MASK_LEN == 35
    assert "supersecret" not in fp
    # deterministic for same input, different for different keys
    assert mask_key("abc") == mask_key("abc")
    assert mask_key("abc") != mask_key("abd")


def test_monitor_token_hook_and_panes(tmp_path):
    mon = LiveMonitor(str(tmp_path))
    got = []
    mon.note_token("#1 llama3", "Hel")
    mon.note_token("#1 llama3", "lo ")
    mon.note_token("#2 mixtral", "world")
    left, right = mon.snapshot(8)
    joined_left = "".join(left)
    assert "COMPUTE" in joined_left and "token stream" in joined_left
    assert "API AUDIT" in "".join(right)
    # api audit rows are 35-char masked fingerprints
    mon.note_remote_key(1, "sk-live-secret-key-value", 200)
    _l, r = mon.snapshot(8)
    txt = "".join(r)
    assert "sk-live" not in txt
    rendered = mon.render()
    assert "TTY1" in rendered and "TTY2" in rendered
    assert got == []


def test_jobs_manager_fires_token_hook(tmp_path):
    seen = []
    def runner(model, prompt, tps, on_token, stop_flag, number=None):
        for w in ["o", "n", "e"]:
            on_token(w)
        return "one", 3, 0.01
    mgr = Manager(runner)
    mgr.token_hook = lambda label, tok: seen.append((label, tok))
    job = mgr.submit(7, "test-model", "hi", tps=1000)
    for _ in range(100):
        if job.state in ("done", "error"):
            break
        threading.Event().wait(0.02)
    assert job.state == "done"
    assert [t for (_l, t) in seen] == ["o", "n", "e"]
    assert seen[0][0].startswith("#7")


def test_hook_failure_does_not_break_generation(tmp_path):
    def boom(label, tok):
        raise RuntimeError("logging down")
    def runner(model, prompt, tps, on_token, stop_flag, number=None):
        on_token("x")
        return "x", 1, 0.0
    mgr = Manager(runner)
    mgr.token_hook = boom
    job = mgr.submit(1, "m", "q", tps=1000)
    for _ in range(100):
        if job.state in ("done", "error"):
            break
        threading.Event().wait(0.02)
    assert job.state == "done" and job.answer == "x"
