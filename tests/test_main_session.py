from __future__ import annotations

import unittest
from unittest.mock import patch

import main


class SessionRunnerTests(unittest.TestCase):
    def test_browsers_close_on_interrupt_during_interval(self):
        args = main.parse_args(["--runs", "2"])
        with patch.object(main, "UpworkBrowserSession") as session, \
             patch.object(main, "LeadEngine") as engine, \
             patch.object(main.time, "sleep", side_effect=KeyboardInterrupt):
            self.assertEqual(main.run_session(args), 130)
        engine.return_value.close.assert_called_once()
        session.return_value.close.assert_called_once()

    def test_session_defaults_to_ten_runs_and_five_seconds(self) -> None:
        args = main.parse_args([])

        self.assertEqual(args.runs, 10)
        self.assertEqual(args.interval, 5.0)
        self.assertFalse(args.continuous)

    def test_forced_catch_up_applies_only_to_first_session_run(self) -> None:
        args = main.parse_args(
            ["--runs", "3", "--interval", "0", "--catch-up"]
        )
        force_values: list[bool] = []
        closed: list[bool] = []

        class EngineFake:
            def __init__(self, config, *, upwork_session=None) -> None:
                force_values.append(config.force_catch_up)

            def run(self) -> list:
                return []

            def close(self) -> None:
                closed.append(True)

        with (
            patch.object(main, "LeadEngine", EngineFake),
            patch.object(main.time, "sleep") as sleep,
        ):
            exit_code = main.run_session(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(force_values, [True, False, False])
        self.assertEqual(len(closed), 3)
        self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
