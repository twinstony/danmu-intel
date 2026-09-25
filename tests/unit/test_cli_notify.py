"""`notify` / `alerts` / `notify-config` 三条命令（不连外网：没有待投递事件时不发请求）。"""

from __future__ import annotations

import pytest

from danmu_intel.cli import main
from danmu_intel.common.notifications import emit
from danmu_intel.notify.config import load_notify_config
from danmu_intel.notify.suppression import admit, alert_key, note_sent, resolve

from conftest import BASE_TS

QQ_ENV = {
    "QQ_BOT_APP_ID": "app",
    "QQ_BOT_APP_SECRET": "secret",
    "QQ_BOT_OPENID": "openid-1",
}


@pytest.fixture
def no_ambient_channel_credentials(monkeypatch):
    """真实环境里可能有 QQ/TG 凭据；这些用例必须自己决定"配了没有"。"""
    for key in (*QQ_ENV, "QQ_BOT_GROUP_OPENID", "TG_BOT_TOKEN", "TG_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_notify_without_any_channel_is_a_loud_failure(data_root, capsys, no_ambient_channel_credentials):
    assert main(["notify"]) == 2
    assert "没有可用的通知通道" in capsys.readouterr().err


def test_notify_once_reports_the_empty_queue(data_root, capsys, no_ambient_channel_credentials):
    no_ambient_channel_credentials.setenv("QQ_BOT_APP_ID", "app")
    no_ambient_channel_credentials.setenv("QQ_BOT_APP_SECRET", "secret")
    no_ambient_channel_credentials.setenv("QQ_BOT_OPENID", "openid-1")

    assert main(["notify"]) == 0

    out = capsys.readouterr().out
    assert "没有待投递事件" in out
    assert "通知队列：pending 0｜delivered 0｜suppressed 0｜dropped_expired 0｜failed 0" in out


def test_notify_loop_with_zero_seconds_runs_one_pass(data_root, capsys, no_ambient_channel_credentials):
    no_ambient_channel_credentials.setenv("TG_BOT_TOKEN", "bot-token")
    no_ambient_channel_credentials.setenv("TG_CHAT_ID", "42")

    assert main(["notify", "--loop", "--seconds", "0", "--interval", "5"]) == 0

    out = capsys.readouterr().out
    assert "投递循环结束：1 轮（间隔 5 秒）" in out


def test_alerts_reports_the_ledger_and_the_queue(data_root, capsys):
    from danmu_intel.common.db import open_db
    from danmu_intel.common import paths

    conn = open_db(paths.db_path())
    try:
        key = alert_key("disk_low", {"match_id": 1, "platform": "huya", "room_id": "660000"})
        admit(conn, "disk_low", key=key, at=BASE_TS, cooldown_ms=900_000)
        note_sent(conn, key, at=BASE_TS + 100)
        resolve(conn, "disk_low", identity={"match_id": 1, "platform": "huya", "room_id": "660000"}, timestamp=BASE_TS + 200)
    finally:
        conn.close()
    capsys.readouterr()

    assert main(["alerts"]) == 0

    out = capsys.readouterr().out
    assert "通知队列：pending 1｜delivered 0" in out
    assert "disk_low:" in out and "｜resolved｜发生 1 次" in out
    assert "最后送达" in out and "恢复" in out


def test_alerts_reports_an_empty_ledger(data_root, capsys):
    assert main(["alerts"]) == 0
    assert "没有告警台账记录" in capsys.readouterr().out


def test_alerts_filters_by_state(data_root, capsys):
    from danmu_intel.common import paths
    from danmu_intel.common.db import open_db

    conn = open_db(paths.db_path())
    try:
        admit(conn, "disk_low", key="k1", at=BASE_TS, cooldown_ms=900_000)
    finally:
        conn.close()
    capsys.readouterr()

    assert main(["alerts", "--state", "resolved"]) == 0
    assert "没有告警台账记录" in capsys.readouterr().out


def test_notify_config_prints_and_updates_with_audit(data_root, capsys):
    assert main(["notify-config"]) == 0
    out = capsys.readouterr().out
    assert "gate_ms = 300000" in out and "cooldown_ms = 900000" in out
    assert "max_attempts = 3" in out

    assert main(["notify-config", "--set", "cooldown_ms=60000", "--actor", "tony"]) == 0
    assert "已更新通知门槛（操作者 tony）" in capsys.readouterr().out

    from danmu_intel.common import paths
    from danmu_intel.common.db import open_db

    conn = open_db(paths.db_path())
    try:
        assert load_notify_config(conn).cooldown_ms == 60_000
    finally:
        conn.close()


def test_notify_config_refuses_unknown_keys(data_root, capsys):
    assert main(["notify-config", "--set", "cooldown_s=60"]) == 2
    assert "未知的通知配置项" in capsys.readouterr().err


def test_events_shows_terminal_states_too(data_root, capsys):
    from danmu_intel.common import paths
    from danmu_intel.common.db import open_db
    from danmu_intel.common.notifications import mark_dropped_expired

    conn = open_db(paths.db_path())
    try:
        event_id = emit(conn, "disk_low", severity="critical", payload={"match_id": 1}, timestamp=BASE_TS)
        mark_dropped_expired(conn, event_id)
    finally:
        conn.close()
    capsys.readouterr()

    assert main(["events"]) == 0
    assert "disk_low（critical，dropped_expired）" in capsys.readouterr().out
