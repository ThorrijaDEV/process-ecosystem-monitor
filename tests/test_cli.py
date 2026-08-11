"""CLI tests: parser, exit codes, headless runs, exports."""

from __future__ import annotations

import json
import os
import sys

import pytest

from process_ecosystem.cli import build_parser, main


class TestParser:
    def test_default_command_monitor(self):
        args = build_parser().parse_args([])
        assert args.command == "monitor"

    def test_explicit_commands(self):
        assert build_parser().parse_args(["demo"]).command == "demo"
        assert build_parser().parse_args(["monitor"]).command == "monitor"

    def test_invalid_command(self, capsys):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["explode"])
        assert exc.value.code == 2

    def test_flags_anywhere(self):
        p = build_parser()
        a = p.parse_args(["demo", "--seed", "3", "--rate", "2"])
        assert (a.seed, a.rate) == (3, 2.0)
        b = p.parse_args(["--rate", "2", "demo", "--seed", "3"])
        assert (b.seed, b.rate) == (3, 2.0)

    def test_defaults(self):
        a = build_parser().parse_args([])
        assert a.rate == 5.0
        assert a.budget == 40
        assert a.history == 10
        assert a.show_kernel is False
        assert a.unicode is None

    def test_no_unicode_flag(self):
        a = build_parser().parse_args(["--no-unicode"])
        assert a.unicode is False

    def test_version_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["--version"])
        assert exc.value.code == 0
        assert "process-ecosystem" in capsys.readouterr().out

    def test_headless_hidden_ok(self):
        a = build_parser().parse_args(["demo", "--headless", "5"])
        assert a.headless == 5


class TestMain:
    def test_demo_headless_runs(self, capsys):
        rc = main(["demo", "--headless", "10"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "ticks 10" in out
        assert "alive" in out

    def test_demo_headless_deterministic(self, capsys):
        main(["demo", "--headless", "20", "--seed", "7"])
        out1 = capsys.readouterr().out
        main(["demo", "--headless", "20", "--seed", "7"])
        out2 = capsys.readouterr().out
        assert out1 == out2

    def test_demo_headless_export_json(self, tmp_path, capsys):
        out = tmp_path / "h.json"
        rc = main(["demo", "--headless", "5", "--export", str(out)])
        assert rc == 0
        data = json.loads(out.read_text())
        assert len(data) == 5
        assert "births" in data[0]

    def test_demo_headless_export_csv(self, tmp_path, capsys):
        out = tmp_path / "h.csv"
        rc = main(["demo", "--headless", "5", "--export-csv", str(out)])
        assert rc == 0
        lines = out.read_text().splitlines()
        assert lines[0].startswith("t,cpu_pct")
        assert len(lines) == 6

    def test_rate_bounds(self, tmp_path, capsys):
        # rate below 0.5 clamps to 0.5; headless still runs
        rc = main(["demo", "--headless", "2", "--rate", "1000"])
        assert rc == 0

    def test_non_tty_exits_2(self, capsys):
        # headless=0 and no tty in CI → exit 2 with a hint
        rc = main(["demo"])
        err = capsys.readouterr().err
        assert rc == 2
        assert "terminal" in err

    def test_proc_root_fixture(self, tmp_path, capsys):
        (tmp_path / "1").mkdir()
        (tmp_path / "1" / "stat").write_text(
            "1 (systemd) S 0 1 1 0 -1 100 0 0 0 10 10 0 0 20 0 1 0 500 100 10 1 0 0 0 0 0 0 0 0 0 0 0 17 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
        )
        (tmp_path / "stat").write_text("cpu  100 0 0 900 0 0 0 0 0 0\n")
        (tmp_path / "meminfo").write_text("MemTotal:       1024 kB\nMemAvailable:   512 kB\n")
        (tmp_path / "loadavg").write_text("0.1 0.2 0.3 1/1 1\n")
        rc = main(["monitor", "--headless", "3", "--proc-root", str(tmp_path)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "alive 1" in out

    def test_main_demo_with_duration_flag_accepted(self, capsys):
        rc = main(["demo", "--headless", "3", "--duration", "10", "--speed", "2"])
        assert rc == 0

    def test_demo_headless_duration_stops_early(self, capsys):
        # 1.0 sim-second at rate 5 (0.2 s/tick) = 5 ticks, not the requested 100
        rc = main(["demo", "--headless", "100", "--duration", "1"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "ticks 5" in out

    def test_demo_headless_duration_not_reached_uses_ticks(self, capsys):
        rc = main(["demo", "--headless", "3", "--duration", "100"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "ticks 3" in out

    def test_demo_headless_duration_speed_compression(self, capsys):
        # speed 4 → 0.05 s/tick → 2.0 sim-seconds = 40 ticks
        rc = main(["demo", "--headless", "500", "--duration", "2", "--speed", "4"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "ticks 40" in out

    def test_demo_headless_duration_exact_boundary(self, capsys):
        # 1.0 sim-second at 0.2 s/tick lands exactly on tick 5
        rc = main(["demo", "--headless", "100", "--duration", "1.0"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "ticks 5" in out

    def test_demo_headless_zero_duration(self, capsys):
        rc = main(["demo", "--headless", "50", "--duration", "0"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "ticks 0" in out

    def test_monitor_ignores_duration_semantics(self, capsys, tmp_path):
        # monitor mode: --duration is a demo flag, headless tick count rules
        (tmp_path / "1").mkdir()
        (tmp_path / "1" / "stat").write_text(
            "1 (systemd) S 0 1 1 0 -1 100 0 0 0 10 10 0 0 20 0 1 0 500 100 10 1 0 0 0 0 0 0 0 0 0 0 0 17 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
        )
        (tmp_path / "stat").write_text("cpu  100 0 0 900 0 0 0 0 0 0\n")
        (tmp_path / "meminfo").write_text("MemTotal:       1024 kB\nMemAvailable:   512 kB\n")
        (tmp_path / "loadavg").write_text("0.1 0.2 0.3 1/1 1\n")
        rc = main(["monitor", "--headless", "4", "--proc-root", str(tmp_path)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "ticks 4" in out


class TestModuleEntry:
    def test_version_module(self):
        import subprocess

        r = subprocess.run(
            [sys.executable, "-m", "process_ecosystem", "--version"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        assert r.returncode == 0
        assert "process-ecosystem" in r.stdout
