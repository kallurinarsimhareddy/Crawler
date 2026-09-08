"""The deployment units say what they mean, and mean something the CLI accepts.

A systemd unit is configuration that is only ever executed on the server, which
makes it exactly the kind of file that drifts: a flag gets renamed in
``crawler.weekly_run`` and the unit keeps naming the old one until a Saturday
crawl fails at 06:00 with nobody watching. So the most valuable test here is not
that the files parse — it is
:meth:`TestExecStartMatchesTheCLI.test_every_flag_is_one_the_cli_accepts`, which
feeds each unit's ``ExecStart`` arguments to the real argument parser.

Nothing here needs Linux, systemd, or a server. The units are read as text and
checked as data, so the suite behaves the same on the Windows machine these were
written on as it will on the host they are for.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from typing import Dict, List, Tuple

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"

SERVICE = DEPLOY / "careercrawler.service"
TIMER = DEPLOY / "careercrawler.timer"
RESUME = DEPLOY / "careercrawler-resume.service"
ENV_EXAMPLE = DEPLOY / "careercrawler.env.example"
INSTALL = DEPLOY / "install.sh"
GUARD = DEPLOY / "careercrawler-should-resume.sh"
README = DEPLOY / "README.deploy.md"

#: The unit files, for the checks that apply to all of them.
UNITS = (SERVICE, TIMER, RESUME)

#: The two that actually launch a crawl.
RUNNERS = (SERVICE, RESUME)


def read_unit(path: Path) -> Dict[str, List[Tuple[str, str]]]:
    """Parse a systemd unit into ``{section: [(key, value), ...]}``.

    Not :mod:`configparser`: a unit may repeat a key — ``Environment=`` is
    normally given several times — and configparser would silently keep only the
    last. Directives continued with a trailing backslash are joined, because
    ``ExecStart`` is written across several lines.

    Args:
        path: The unit file.

    Returns:
        Section name to its directives, in order.
    """
    sections: Dict[str, List[Tuple[str, str]]] = {}
    current = ""
    pending = ""

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()

        if pending:
            pending = pending[:-1].rstrip() + " " + line
            if not pending.endswith("\\"):
                key, _, value = pending.partition("=")
                sections.setdefault(current, []).append((key.strip(), value.strip()))
                pending = ""
            continue

        if not line or line.startswith("#") or line.startswith(";"):
            continue

        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
            continue

        if line.endswith("\\"):
            pending = line
            continue

        key, _, value = line.partition("=")
        sections.setdefault(current, []).append((key.strip(), value.strip()))

    return sections


def directive(path: Path, section: str, key: str) -> List[str]:
    """Every value a unit gives for one directive.

    Args:
        path: The unit file.
        section: Section name, e.g. ``"Service"``.
        key: Directive name, e.g. ``"Environment"``.

    Returns:
        The values, in order. Empty when the directive is absent.
    """
    return [
        value for name, value in read_unit(path).get(section, []) if name == key
    ]


def one(path: Path, section: str, key: str) -> str:
    """The single value a unit gives for one directive.

    Args:
        path: The unit file.
        section: Section name.
        key: Directive name.

    Returns:
        The value, or ``""`` when absent.
    """
    values = directive(path, section, key)
    return values[0] if values else ""


# ---------------------------------------------------------------------------
# 1. The files exist and parse
# ---------------------------------------------------------------------------


class TestTheFilesExist(unittest.TestCase):
    """Everything the README tells an operator to run is actually shipped."""

    def test_every_deployment_file_is_present(self) -> None:
        for path in (SERVICE, TIMER, RESUME, ENV_EXAMPLE, INSTALL, GUARD, README):
            self.assertTrue(path.is_file(), f"missing {path.name}")

    def test_every_unit_parses_into_sections(self) -> None:
        for path in UNITS:
            sections = read_unit(path)
            self.assertIn("Unit", sections, path.name)

    def test_the_shell_scripts_are_bash_with_strict_mode(self) -> None:
        """A deployment script that ignores a failure installs half a service."""
        for path in (INSTALL, GUARD):
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("#!/usr/bin/env bash"), path.name)
            self.assertIn("set -euo pipefail", text, path.name)


# ---------------------------------------------------------------------------
# 2. The service
# ---------------------------------------------------------------------------


class TestTheService(unittest.TestCase):
    """``careercrawler.service`` — the crawl itself."""

    def test_it_is_a_oneshot(self) -> None:
        self.assertEqual(one(SERVICE, "Service", "Type"), "oneshot")

    def test_the_stop_timeout_is_900_seconds(self) -> None:
        self.assertEqual(one(SERVICE, "Service", "TimeoutStopSec"), "900")

    def test_declining_and_interrupting_are_not_failures(self) -> None:
        """75 is "another run holds the database", 130 is "stopped and
        checkpointed". Neither should page anybody."""
        codes = one(SERVICE, "Service", "SuccessExitStatus").split()
        self.assertEqual(sorted(codes), ["0", "130", "75"])

    def test_a_long_crawl_is_not_treated_as_hung(self) -> None:
        """Twenty hours is a normal full roster."""
        self.assertEqual(one(SERVICE, "Service", "TimeoutStartSec"), "infinity")

    def test_stopping_asks_politely_first(self) -> None:
        """SIGTERM is what the crawler turns into "finish this batch"."""
        self.assertEqual(one(SERVICE, "Service", "KillSignal"), "SIGTERM")

    def test_it_does_not_restart_itself(self) -> None:
        """A crawl that failed should be looked at, not relaunched in a loop."""
        self.assertEqual(one(SERVICE, "Service", "Restart"), "no")

    def test_it_waits_for_the_network(self) -> None:
        self.assertIn("network-online.target", one(SERVICE, "Unit", "After"))
        self.assertIn("network-online.target", one(SERVICE, "Unit", "Wants"))

    def test_it_does_not_run_as_root(self) -> None:
        self.assertEqual(one(SERVICE, "Service", "User"), "@CC_USER@")

    def test_state_and_output_are_writable_and_nothing_else_is(self) -> None:
        self.assertEqual(one(SERVICE, "Service", "ProtectSystem"), "strict")
        writable = one(SERVICE, "Service", "ReadWritePaths")
        self.assertIn("state", writable)
        self.assertIn("output", writable)

    def test_it_is_not_enabled_at_boot(self) -> None:
        """Started by its timer, by the resume unit, or by hand. An [Install]
        WantedBy here would crawl at every boot."""
        self.assertEqual(directive(SERVICE, "Install", "WantedBy"), [])


# ---------------------------------------------------------------------------
# 3. The timer
# ---------------------------------------------------------------------------


class TestTheTimer(unittest.TestCase):
    """``careercrawler.timer`` — the only schedule."""

    def test_it_fires_on_saturday_at_six(self) -> None:
        self.assertEqual(one(TIMER, "Timer", "OnCalendar"), "Sat *-*-* 06:00:00")

    def test_a_missed_run_is_caught_up(self) -> None:
        """The setting whose absence on Windows lost a whole week."""
        self.assertEqual(one(TIMER, "Timer", "Persistent"), "true")

    def test_it_drives_the_crawl_unit(self) -> None:
        self.assertEqual(one(TIMER, "Timer", "Unit"), "careercrawler.service")

    def test_it_is_enabled_at_boot(self) -> None:
        self.assertEqual(one(TIMER, "Install", "WantedBy"), "timers.target")

    def test_there_is_exactly_one_schedule(self) -> None:
        """Two mechanisms firing one unit is how a machine crawls twice. The
        run lock would refuse the second, but a refusal is a symptom."""
        self.assertEqual(len(directive(TIMER, "Timer", "OnCalendar")), 1)
        for key in ("OnBootSec", "OnUnitActiveSec", "OnStartupSec"):
            self.assertEqual(directive(TIMER, "Timer", key), [], key)

    def test_no_other_unit_schedules_anything(self) -> None:
        for path in RUNNERS:
            self.assertEqual(read_unit(path).get("Timer", []), [], path.name)


# ---------------------------------------------------------------------------
# 4. The resume unit
# ---------------------------------------------------------------------------


class TestTheResumeUnit(unittest.TestCase):
    """``careercrawler-resume.service`` — and, mostly, when it declines."""

    def test_it_runs_at_boot(self) -> None:
        self.assertEqual(one(RESUME, "Install", "WantedBy"), "multi-user.target")

    def test_it_is_guarded_so_a_reboot_does_not_start_a_full_crawl(self) -> None:
        """Resume is the crawler's default, not a mode: with no checkpoint it
        crawls all 12,377 companies. Without this the unit would mean "crawl at
        every boot"."""
        condition = one(RESUME, "Service", "ExecCondition")
        self.assertTrue(condition, "no ExecCondition: every reboot starts a full crawl")
        self.assertIn("careercrawler-should-resume.sh", condition)

    def test_the_guard_script_it_names_exists(self) -> None:
        named = one(RESUME, "Service", "ExecCondition").rsplit("/", 1)[-1]
        self.assertTrue((DEPLOY / named).is_file(), f"{named} is referenced but missing")

    def test_the_guard_checks_durability_and_the_week(self) -> None:
        """Both are conditions the crawler itself would reject on, and finding
        that out after booting a twenty-hour crawl is too late."""
        text = GUARD.read_text(encoding="utf-8")
        self.assertIn("durable", text)
        self.assertIn("week_start", text)
        self.assertIn("checkpoint", text.lower())

    def test_it_shares_the_services_exit_code_policy(self) -> None:
        """It races the timer by design; 75 is the expected outcome, not a
        failure."""
        codes = one(RESUME, "Service", "SuccessExitStatus").split()
        self.assertEqual(sorted(codes), ["0", "130", "75"])

    def test_it_passes_no_fresh_flag(self) -> None:
        """--fresh would discard the very checkpoint it exists to resume."""
        self.assertNotIn("--fresh", one(RESUME, "Service", "ExecStart"))


# ---------------------------------------------------------------------------
# 5. ExecStart is a command the CLI actually accepts
# ---------------------------------------------------------------------------


class TestExecStartMatchesTheCLI(unittest.TestCase):
    """The check that catches drift between the units and the program.

    A renamed flag would otherwise be discovered by a Saturday 06:00 crawl
    exiting 2 with nobody watching.
    """

    @staticmethod
    def arguments(path: Path) -> List[str]:
        """The crawler's own arguments from a unit's ExecStart.

        Environment references are replaced with plausible values: the parser
        cares about the flag names and the shape of their values, and systemd
        will have substituted the real ones by the time it runs.
        """
        command = one(path, "Service", "ExecStart")
        after = command.split("-m crawler.weekly_run", 1)[1]
        substituted = re.sub(r"\$\{[A-Z_]+\}", "6", after)
        return substituted.split()

    def test_every_unit_invokes_the_module(self) -> None:
        for path in RUNNERS:
            self.assertIn("-m crawler.weekly_run", one(path, "Service", "ExecStart"), path.name)

    def test_every_unit_uses_the_virtualenv_interpreter(self) -> None:
        for path in RUNNERS:
            self.assertIn("/venv/bin/python", one(path, "Service", "ExecStart"), path.name)

    def test_every_flag_is_one_the_cli_accepts(self) -> None:
        from crawler.weekly_run import _parse_args

        for path in RUNNERS:
            with self.subTest(unit=path.name):
                parsed = _parse_args(self.arguments(path))
                self.assertEqual(parsed.workers, 6)

    def test_the_worker_count_is_configurable(self) -> None:
        for path in RUNNERS:
            self.assertIn("${CAREERCRAWLER_WORKERS}", one(path, "Service", "ExecStart"), path.name)

    def test_the_log_path_is_configurable(self) -> None:
        for path in RUNNERS:
            self.assertIn("${CAREERCRAWLER_LOG}", one(path, "Service", "ExecStart"), path.name)

    def test_the_database_is_named_so_the_lock_follows_it(self) -> None:
        """The run lock is derived from the database path, so configuring one
        moves the other."""
        for path in RUNNERS:
            self.assertIn("--database", one(path, "Service", "ExecStart"), path.name)

    def test_no_unit_disables_the_lock(self) -> None:
        """--no-lock in a unit would undo Phase 1 entirely."""
        for path in RUNNERS:
            self.assertNotIn("--no-lock", one(path, "Service", "ExecStart"), path.name)

    def test_no_unit_forces_a_fresh_crawl(self) -> None:
        for path in RUNNERS:
            self.assertNotIn("--fresh", one(path, "Service", "ExecStart"), path.name)

    def test_every_environment_default_is_referenced_or_documented(self) -> None:
        """A default nothing reads is a default that will drift."""
        for path in RUNNERS:
            command = one(path, "Service", "ExecStart")
            for setting in directive(path, "Service", "Environment"):
                name = setting.split("=", 1)[0]
                if name == "CAREERCRAWLER_HOME":
                    continue  # read by the guard script, not by ExecStart
                self.assertIn(f"${{{name}}}", command, f"{path.name}: {name}")


# ---------------------------------------------------------------------------
# 5b. The resume guard actually decides correctly
# ---------------------------------------------------------------------------


class TestTheResumeGuardLogic(unittest.TestCase):
    """The guard's decision table, executed rather than read.

    Asserting that the script mentions "durable" proves nothing about what it
    does with it. The judgement lives in a Python heredoc inside the shell
    script; this extracts that program and runs it against real checkpoint
    files, which is the only way to be sure a reboot will not launch a
    twenty-hour full crawl.
    """

    @staticmethod
    def program() -> str:
        """The Python embedded in the guard script."""
        text = GUARD.read_text(encoding="utf-8")
        marker = chr(60) * 2 + chr(39) + 'PYTHON' + chr(39)
        _, _, after = text.partition(marker)
        body, _, _ = after.partition(chr(10) + 'PYTHON')
        return body

    def decide(self, checkpoint: object) -> int:
        """Run the guard's program against a checkpoint; return its exit code.

        Args:
            checkpoint: What to write to the file, or ``None`` to leave the
                path absent.

        Returns:
            ``0`` to resume, ``1`` to skip.
        """
        import json
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            if checkpoint is not None:
                path.write_text(json.dumps(checkpoint), encoding="utf-8")

            source = Path(directory) / "guard.py"
            source.write_text(self.program(), encoding="utf-8")

            return subprocess.run(
                [sys.executable, str(source), str(path)],
                capture_output=True,
                text=True,
            ).returncode

    @staticmethod
    def this_week() -> str:
        """Monday of the current ISO week, as the checkpoint records it."""
        import datetime

        today = datetime.date.today()
        return (today - datetime.timedelta(days=today.weekday())).isoformat()

    def test_a_durable_checkpoint_from_this_week_resumes(self) -> None:
        code = self.decide(
            {"durable": True, "week_start": self.this_week(),
             "completed": 6400, "total": 12377, "run_id": "2026-W37-x"}
        )
        self.assertEqual(code, 0, "a resumable checkpoint was skipped")

    def test_a_non_durable_checkpoint_is_skipped(self) -> None:
        """The crawler would refuse it and start fresh; booting a twenty-hour
        crawl to discover that is not resuming."""
        code = self.decide(
            {"durable": False, "week_start": self.this_week(),
             "completed": 6400, "total": 12377}
        )
        self.assertEqual(code, 1)

    def test_a_checkpoint_from_another_week_is_skipped(self) -> None:
        """Week-scoped: a stale one would start a full crawl at boot."""
        code = self.decide(
            {"durable": True, "week_start": "2020-01-06",
             "completed": 6400, "total": 12377}
        )
        self.assertEqual(code, 1)

    def test_an_unreadable_checkpoint_is_skipped(self) -> None:
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            path.write_text("{ not json", encoding="utf-8")
            source = Path(directory) / "guard.py"
            source.write_text(self.program(), encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(source), str(path)],
                capture_output=True, text=True,
            )

        self.assertEqual(result.returncode, 1)

    def test_it_says_why_it_decided(self) -> None:
        """journalctl is where an operator finds out what happened at boot."""
        import json
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            path.write_text(
                json.dumps({"durable": True, "week_start": "2020-01-06"}),
                encoding="utf-8",
            )
            source = Path(directory) / "guard.py"
            source.write_text(self.program(), encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(source), str(path)],
                capture_output=True, text=True,
            )

        self.assertIn("2020-01-06", result.stdout)
        self.assertIn("not resuming", result.stdout.lower())


# ---------------------------------------------------------------------------
# 6. Nothing is hardcoded to the machine these were written on
# ---------------------------------------------------------------------------


class TestNothingIsHardcoded(unittest.TestCase):
    """The units are portable configuration, not one developer's layout."""

    #: Anything that would only make sense on the authoring machine.
    FORBIDDEN = (
        "C:\\",
        "c:\\",
        "Users\\",
        "/mnt/c/",
        "Narsimha",
        "CareerCrawler-seamless",
        "venv\\Scripts",
    )

    def test_no_windows_or_personal_paths_anywhere_in_deploy(self) -> None:
        for path in sorted(DEPLOY.iterdir()):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for needle in self.FORBIDDEN:
                self.assertNotIn(needle, text, f"{path.name} names {needle!r}")

    def test_the_units_use_placeholders_rather_than_a_fixed_root(self) -> None:
        """systemd expands variables in ExecStart arguments but not in
        WorkingDirectory, ExecCondition or the executable, so those are
        substituted at install time instead."""
        for path in UNITS:
            text = path.read_text(encoding="utf-8")
            self.assertIn("@CC_HOME@", text, path.name)

    def test_the_installer_substitutes_both_placeholders(self) -> None:
        text = INSTALL.read_text(encoding="utf-8")
        self.assertIn("@CC_HOME@", text)
        self.assertIn("@CC_USER@", text)
        self.assertIn("CC_HOME:-/opt/careercrawler", text)
        self.assertIn("CC_USER:-careercrawler", text)

    def test_the_installer_starts_nothing(self) -> None:
        """Installing is not deploying. A crawl is the operator's decision.

        Checked against executable lines only: the script *prints* the start
        commands as next steps, which is the opposite of running them.
        """
        commands = [
            line.strip()
            for line in INSTALL.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("systemctl")
        ]
        self.assertTrue(commands, "the installer never calls systemctl at all")
        for command in commands:
            self.assertNotIn("systemctl start", command)
            self.assertNotIn("systemctl restart", command)

    def test_no_secret_is_committed(self) -> None:
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("replace-me", text)
        self.assertNotIn("BEGIN PRIVATE KEY", text)
        self.assertNotIn("1LdOhoLx", text, "the real spreadsheet id is in the example")


# ---------------------------------------------------------------------------
# 7. The README documents what an operator has to do
# ---------------------------------------------------------------------------


class TestTheReadme(unittest.TestCase):
    """Documentation that omits a step is a deployment that fails at 06:00."""

    def setUp(self) -> None:
        self.text = README.read_text(encoding="utf-8")

    def test_it_covers_every_required_topic(self) -> None:
        for topic in (
            "playwright",
            "service account",
            "systemctl start",
            "systemctl status",
            "systemctl stop",
            "journalctl",
            "backup",
            "reboot",
            "run lock",
            "venv",
        ):
            self.assertIn(topic.lower(), self.text.lower(), f"README omits {topic!r}")

    def test_it_names_the_exit_codes(self) -> None:
        for code in ("75", "130"):
            self.assertIn(code, self.text)

    def test_it_warns_that_state_must_be_local_disk(self) -> None:
        """SQLite locking is unsafe over NFS, and the run lock lives there."""
        self.assertIn("NFS", self.text)

    def test_it_does_not_tell_anyone_to_raise_the_worker_count(self) -> None:
        """Phase 4's job, after the browser budget and the vendor limiter."""
        self.assertIn("leave at 6", self.text.lower())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
