"""Run the real shell daemon against temporary hwmon files, never hardware."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


DAEMON = Path(__file__).resolve().parents[1] / "system/bin/hypr-util-fancurve"


class FanChannelsTest(unittest.TestCase):
    def run_daemon(self, dual, fail_second=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hp = root / "hwmon0"
            cpu = root / "hwmon1"
            hp.mkdir()
            cpu.mkdir()
            (hp / "name").write_text("hp\n")
            (cpu / "name").write_text("k10temp\n")
            (cpu / "temp1_input").write_text("60000\n")
            (hp / "pwm1").write_text("200\n")
            (hp / "pwm1_enable").write_text("2\n")
            if dual:
                if fail_second:
                    (hp / "pwm2").mkdir()  # Opening this output must fail.
                else:
                    (hp / "pwm2").write_text("0\n")
            (root / "override").write_text("255\n")
            script = root / "daemon"
            source = DAEMON.read_text().replace("/sys/class/hwmon", str(root))
            # End after one tick, with a failure to exercise the EXIT cleanup.
            source = source.replace("INTERVAL=5", "sleep() { exit 7; }\nINTERVAL=5")
            source = source.replace("powerprofilesctl get 2>/dev/null || echo \"balanced\"", "echo balanced")
            script.write_text(source)
            result = subprocess.run(
                ["bash", str(script)],
                env={**os.environ, "HYPR_UTIL_CONFIG_DIR": str(root)},
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 1 if fail_second else 7)
            self.assertEqual((hp / "pwm1").read_text().strip(), "240")
            self.assertEqual((hp / "pwm1_enable").read_text().strip(), "2")
            if dual and not fail_second:
                self.assertEqual((hp / "pwm2").read_text().strip(), "240")
            self.assertIn(f"controlling {2 if dual else 1} PWM channel(s)", result.stdout)

    def test_legacy_shared_channel(self):
        self.run_daemon(dual=False)

    def test_independent_channels(self):
        self.run_daemon(dual=True)

    def test_output_failure_restores_firmware(self):
        self.run_daemon(dual=True, fail_second=True)


if __name__ == "__main__":
    unittest.main()
