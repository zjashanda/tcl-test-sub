"""regen_audio.py 的离线验证（使用 stub lstts_core，不访问网络）。"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

DELIVERABLES = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "regen_audio.py"
STUB = Path(__file__).resolve().parent / "stub"


def run(args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(STUB)
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env, check=False)


class RegenAudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.items = self.root / "items.csv"
        with self.items.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["case_id", "text", "voice"])
            writer.writeheader()
            writer.writerow({"case_id": "wakeup-001", "text": "小T小T", "voice": ""})
            writer.writerow({"case_id": "cmd-001", "text": "打开风扇", "voice": ""})
            writer.writerow({"case_id": "cmd-002", "text": "关闭风扇", "voice": "小宁"})

    def tearDown(self):
        self.temp.cleanup()

    def test_plan_needs_no_lstts_core(self):
        result = run(["plan", "--items", str(self.items), "--out-dir", str(self.root / "out")])
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["count"], 3)
        self.assertTrue(all(item["output"].endswith(".wav") for item in payload["items"]))
        self.assertFalse((self.root / "out").exists(), "plan 不应创建输出目录")

    def test_build_writes_manifest_and_csv(self):
        out = self.root / "out"
        result = run(["build", "--items", str(self.items), "--out-dir", str(out)])
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((out / "regen_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["count"], 3)
        self.assertEqual(manifest["passed"], 3)
        self.assertEqual(len(list(out.glob("*.wav"))), 3)
        self.assertTrue(all(item["sha256"] for item in manifest["items"]))
        self.assertEqual(manifest["items"][2]["voice"], "小宁")
        with (out / "regen_manifest.csv").open(encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["status"], "PASS")

    def test_limit_and_corpus_guard(self):
        corpus = self.root / "corpus"
        corpus.mkdir()
        blocked = run(["build", "--items", str(self.items), "--out-dir", str(corpus / "regen"),
                       "--corpus-dir", str(corpus)])
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("禁止", blocked.stderr)

        out = self.root / "limited"
        ok = run(["build", "--items", str(self.items), "--out-dir", str(out), "--limit", "2"])
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(len(list(out.glob("*.wav"))), 2)

    def test_missing_text_column_fails_cleanly(self):
        bad = self.root / "bad.csv"
        bad.write_text("case_id,nothing\nu1,x\n", encoding="utf-8")
        result = run(["plan", "--items", str(bad), "--out-dir", str(self.root / "o")])
        self.assertEqual(result.returncode, 1)
        self.assertIn("缺少文本列", result.stderr)


if __name__ == "__main__":
    unittest.main()
