"""Restart the paper bot with new cash from the console: the request, the bot's archive, the start balance."""
import json
import os
import types

from xshark.bot import Bot
from xshark.web.server import Console


def test_the_console_asks_only_a_paper_bot_and_checks_the_cash(tmp_path):
    c = Console(str(tmp_path))
    (tmp_path / "state.json").write_text(json.dumps({"mode": "LIVE-ARMED"}))
    assert "error" in c.request_restart({"balance": 100})
    (tmp_path / "state.json").write_text(json.dumps({"mode": "paper"}))
    assert "error" in c.request_restart({"balance": 0.5}) and "error" in c.request_restart({"balance": "x"})
    assert c.request_restart({"balance": 120})["ok"]
    assert json.loads((tmp_path / "reset_request.json").read_text())["balance"] == 120


def test_the_bot_archives_its_run_keeps_the_settings_and_saves_the_cash(tmp_path):
    d = tmp_path / "paper" / "btc"
    (d / "windows").mkdir(parents=True)
    for name in ("fills.jsonl", "windows.jsonl", "open.json", "runs.json", "state.json", "controls.json"):
        (d / name).write_text("{}")
    (d / "reset_request.json").write_text(json.dumps({"balance": 75}))
    o = types.SimpleNamespace(live=False, dir=str(d), asset="BTC")
    o._path = lambda name: os.path.join(str(d), name)
    bal = Bot._take_reset_request(o)
    assert bal == 75 and not (d / "reset_request.json").exists()
    Bot._archive_and_reset(o, bal)
    left = set(os.listdir(d))
    assert left == {"controls.json", "balance.json"}                      # settings kept, the run gone
    arch = tmp_path / "paper-archive"
    (sub,) = os.listdir(arch)
    assert {"fills.jsonl", "windows.jsonl", "windows", "controls.json"} <= set(os.listdir(arch / sub))
    assert json.loads((d / "balance.json").read_text())["balance"] == 75
