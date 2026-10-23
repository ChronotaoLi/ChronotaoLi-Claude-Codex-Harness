import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.util import group_alive, run_process


class ProcessStdinTests(unittest.TestCase):
    def test_delayed_large_input_survives_multiple_heartbeat_windows(self):
        payload = ("delayed input: \u9a8c\u6536\U0001f680\r\n" * 32768).encode("utf-8")
        self.assertGreater(len(payload), 256 * 1024)
        script = (
            "import hashlib,json,sys,time; "
            "time.sleep(0.3); "
            "data=sys.stdin.buffer.read(); "
            "print(json.dumps({'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}))"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "delayed"
            progress = io.StringIO()
            pgids = []
            with patch("harness.util.PROCESS_HEARTBEAT_SECONDS", 0.05), \
                    contextlib.redirect_stderr(progress):
                code = run_process([sys.executable, "-c", script], root, log,
                                   stdin=payload.decode("utf-8"), timeout=5, on_start=pgids.append)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(log.with_suffix(".stdout").read_text()), {
                "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
            })
            self.assertGreaterEqual(len(progress.getvalue().splitlines()), 2)
            self.assertEqual(len(pgids), 1)
            self.assertFalse(group_alive(pgids[0]))
            self.assertEqual({path.name for path in root.iterdir()}, {"delayed.stdout", "delayed.stderr"})

    def test_empty_input_reaches_eof_and_preserves_exit_code(self):
        script = "import sys; data=sys.stdin.buffer.read(); print(len(data)); sys.exit(7)"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "empty"
            code = run_process([sys.executable, "-c", script], root, log, timeout=5)
            self.assertEqual(code, 7)
            self.assertEqual(log.with_suffix(".stdout").read_text(), "0\n")

    def test_start_callback_failure_cleans_up_process_and_input(self):
        pgids = []

        def interrupted(pid):
            pgids.append(pid)
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "interrupted"
            with self.assertRaises(KeyboardInterrupt):
                run_process([sys.executable, "-c", "import time; time.sleep(30)"], root, log,
                            stdin="pending input" * 32768, on_start=interrupted)
            self.assertEqual(len(pgids), 1)
            self.assertFalse(group_alive(pgids[0]))
            self.assertEqual({path.name for path in root.iterdir()}, {
                "interrupted.stdout", "interrupted.stderr",
            })


if __name__ == "__main__":
    unittest.main()
