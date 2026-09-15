from app.cli import cmd_request_live, main
from app.main import TradingApp
from app.risk.authorization import LIVE_CONFIRMATION_PHRASE
from tests.conftest import settings


def test_cli_status(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = settings(tmp_path)
    monkeypatch.setenv("DB_PATH", s.db_path)
    monkeypatch.setenv("TRADING_MODE", "paper")
    rc = main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "MODE: PAPER" in out


def test_activation_refused_before_7_days(tmp_path):
    app = TradingApp(settings(tmp_path))
    app.repo.mark_paper_started()
    rc = cmd_request_live(app, LIVE_CONFIRMATION_PHRASE)
    assert rc == 1
    assert app.repo.latest_activation() is None
