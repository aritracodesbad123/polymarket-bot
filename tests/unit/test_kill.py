from app.risk.manager import KillSwitch
from tests.conftest import db


def test_kill_persists(tmp_path):
    _d, repo = db(tmp_path)
    KillSwitch(repo).trigger("test")
    assert repo.state().halted
    assert repo.state().halt_reason == "test"
    repo.resume_paper()
    assert not repo.state().halted
