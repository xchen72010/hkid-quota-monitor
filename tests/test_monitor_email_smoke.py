"""本次日期窗口/邮件路径的离线冒烟；不运行原有完整测试套件。"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from quota_monitor import notify as N
from quota_monitor.diff import diff_snapshots


def event(date="2026-10-18", office="RHK", etype="quota_open"):
    return {"type": etype, "office": office, "date": date, "session": "R",
            "from": "r", "to": "y"}


@contextlib.contextmanager
def sandbox(events, admin="recipient@example.invalid", dry="1"):
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        data = base / "data"
        data.mkdir()
        (base / "config.json").write_bytes((ROOT / "config.json").read_bytes())
        (data / "events.json").write_text(json.dumps({"events": events}), encoding="utf-8")
        original_cwd = Path.cwd()
        try:
            os.chdir(base)
            with patch.object(N, "DATA", data), patch.object(N, "STATE_PATH", data / "notify_state.json"), \
                    patch.dict(os.environ, {"ADMIN_EMAIL": admin, "DRY_RUN": dry,
                                          "QQ_SMTP_USER": "", "QQ_SMTP_PASS": "",
                                          "SUBSCRIBER_KEY": "", "NOTIFY_COOLDOWN_MIN": "360"}), \
                    patch.object(N, "send_feishu", side_effect=AssertionError("Feishu must stay inactive")) as feishu, \
                    patch.object(N.smtplib, "SMTP", side_effect=AssertionError("No real SMTP in smoke")):
                yield data
                feishu.assert_not_called()
        finally:
            os.chdir(original_cwd)


class MonitorEmailSmoke(unittest.TestCase):
    def test_config_and_independent_date_boundaries(self):
        cfg = N.load_alert_cfg(str(ROOT / "config.json"))
        self.assertEqual(cfg["monitor_from"], "2026-10-18")
        self.assertEqual(cfg["monitor_before"], "2026-11-24")
        self.assertFalse(N.in_monitor_window("2026-10-17", cfg))
        for date in ("2026-10-18", "2026-11-02", "2026-11-23"):
            self.assertTrue(N.in_monitor_window(date, cfg))
        self.assertFalse(N.in_monitor_window("2026-11-24", cfg))
        self.assertFalse(N.in_monitor_window("2027-01-01", cfg))
        no_upper = {k: v for k, v in cfg.items() if k != "monitor_before"}
        self.assertTrue(N.in_monitor_window("2027-01-01", no_upper))
        upper = {**cfg, "monitor_before": "2026-11-10"}
        self.assertTrue(N.in_monitor_window("2026-11-09", upper))
        self.assertFalse(N.in_monitor_window("2026-11-10", upper))
        self.assertTrue(N.in_monitor_window("2026-10-17", {"monitor_before": "2026-11-10"}))
        self.assertTrue(N.in_monitor_window("2027-01-01", {}))
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.json"
            p.write_text('{"monitor_from":"10/18/2026","monitor_before":null}', encoding="utf-8")
            self.assertEqual(N.load_alert_cfg(str(p)), {})

    def test_tier_boundaries_and_original_cooldown(self):
        cfg = N.load_alert_cfg(str(ROOT / "config.json"))
        for date, expected in (("2026-10-18", "urgent"), ("2026-10-26", "notice"),
                               ("2026-11-02", "info"), ("2027-01-01", "info")):
            self.assertEqual(N.tier_of(date, cfg), expected)
        now = datetime(2026, 10, 9, 12, tzinfo=N.HKT)
        state = {}
        self.assertEqual(len(N.filter_events([event()], state, 360, now)), 1)
        self.assertEqual(N.filter_events([event()], state, 360, now), [])
        self.assertEqual(len(N.filter_events([event()], state, 360, now + timedelta(minutes=361))), 1)

    def test_all_six_offices_and_new_reopened_events_email_dry_run(self):
        offices = list(N.OFFICE_NAMES)
        dates = ["2026-10-17", "2026-10-18", "2026-11-23", "2026-11-24", "2027-01-01"]
        old = {"dates": dates[:2], "quota": {o: {d: {"R": "r", "K": "x"}
                                               for d in dates[:2]} for o in offices}}
        new = {"dates": dates, "quota": {o: {d: {"R": "y", "K": "x"}
                                            for d in dates} for o in offices}}
        events = diff_snapshots(old, new) + [event(etype="quota_gone")]
        self.assertEqual({e["type"] for e in events}, {"quota_open", "new_date", "quota_gone"})
        with sandbox(events) as data, contextlib.redirect_stdout(io.StringIO()) as output:
            original_send = N.send_emails
            with patch.object(N, "send_emails", wraps=original_send) as send:
                N.main()
                payloads, dry = send.call_args.args
                self.assertTrue(dry)
                self.assertEqual(len(payloads), 1)
                self.assertEqual(payloads[0][0], "recipient@example.invalid")
                body = payloads[0][2]
                self.assertNotIn("10/17", body)
                self.assertIn("10/18", body)
                self.assertIn("11/23", body)
                self.assertNotIn("11/24", body)
                self.assertNotIn("01/01", body)
                for office in N.OFFICE_NAMES.values():
                    self.assertIn(office, body)
            cells = json.loads((data / "notify_state.json").read_text(encoding="utf-8"))["cell_last_notified"]
            self.assertEqual(len(cells), 12)
            self.assertEqual({k.split("|")[0] for k in cells}, set(offices))
            self.assertIn("[DRY] email", output.getvalue())

    def test_missing_smtp_restores_cooldown_and_no_recipient_does_not_consume_it(self):
        with sandbox([event()], dry="0") as data, contextlib.redirect_stdout(io.StringIO()):
            before = {"cell_last_notified": {"RKO|2026-10-19|R": "2026-10-08T12:00:00+08:00"}}
            (data / "notify_state.json").write_text(json.dumps(before), encoding="utf-8")
            N.main()
            self.assertEqual(json.loads((data / "notify_state.json").read_text(encoding="utf-8")), before)
        with sandbox([event()], admin="") as data, contextlib.redirect_stdout(io.StringIO()):
            N.main()
            self.assertFalse((data / "notify_state.json").exists())

    def test_all_mail_rejected_is_failure(self):
        with patch.dict(os.environ, {"QQ_SMTP_USER": "sender@example.invalid", "QQ_SMTP_PASS": "test-only"}), \
                patch.object(N.smtplib, "SMTP") as smtp, contextlib.redirect_stdout(io.StringIO()):
            smtp.return_value.__enter__.return_value.sendmail.side_effect = N.smtplib.SMTPRecipientsRefused({})
            with self.assertRaises(RuntimeError):
                N.send_emails([("recipient@example.invalid", "subject", "<p>smoke</p>")], dry=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
