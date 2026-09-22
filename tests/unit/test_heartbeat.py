"""心跳文件、磁盘检查与监督接力棒的契约（不连网络）。"""

from __future__ import annotations

import json

import pytest

from danmu_intel.collect import heartbeat
from danmu_intel.collect.heartbeat import (
    DISK_FREE_MIN_BYTES,
    HEARTBEAT_STALE_S,
    Heartbeat,
    Supervision,
    disk_low,
    heartbeat_age_ms,
    heartbeat_path,
    read_heartbeat,
    supervision_env,
    supervision_state,
    write_heartbeat,
)

from conftest import BASE_TS


def make_beat(**overrides) -> Heartbeat:
    payload = dict(
        pid=4321,
        session_id=7,
        platform="huya",
        room_id="660000",
        state="running",
        started_at=BASE_TS,
        last_msg_at=BASE_TS + 1_000,
        msg_count=12,
        reconnects=1,
        restart_count=2,
        written_at=BASE_TS + 2_000,
    )
    payload.update(overrides)
    return Heartbeat(**payload)


def test_write_then_read_roundtrip(data_root):
    path = write_heartbeat(make_beat(), data_root=data_root)
    assert path == data_root.joinpath(*heartbeat.HEARTBEAT_DIR, "huya-660000.json")
    assert read_heartbeat("huya", "660000", data_root=data_root) == make_beat()


def test_path_is_keyed_by_room(data_root):
    assert heartbeat_path("huya", "660000", data_root=data_root).name == "huya-660000.json"


def test_write_leaves_no_temp_file_and_overwrites(data_root):
    write_heartbeat(make_beat(), data_root=data_root)
    write_heartbeat(make_beat(state="stalled", msg_count=99), data_root=data_root)
    beats = list(data_root.joinpath(*heartbeat.HEARTBEAT_DIR).iterdir())
    assert [beat.name for beat in beats] == ["huya-660000.json"], "临时文件必须被 os.replace 吃掉"
    beat = read_heartbeat("huya", "660000", data_root=data_root)
    assert beat is not None and (beat.state, beat.msg_count) == ("stalled", 99)


@pytest.mark.parametrize("payload", ["{半截", '{"pid": 1}', "[]", ""])
def test_read_tolerates_broken_heartbeat(data_root, payload):
    """残缺/非法心跳一律当没有——不崩，让 supervisor 按「僵死」处理。"""
    path = heartbeat_path("huya", "660000", data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    assert read_heartbeat("huya", "660000", data_root=data_root) is None


def test_read_missing_returns_none(data_root):
    assert read_heartbeat("huya", "660000", data_root=data_root) is None


def test_heartbeat_age_uses_spawn_time_when_no_heartbeat():
    beat = make_beat(written_at=BASE_TS)
    assert heartbeat_age_ms(beat, BASE_TS + 3_000, spawned_at_ms=BASE_TS) == 3_000
    assert heartbeat_age_ms(None, BASE_TS + 20_000, spawned_at_ms=BASE_TS) == 20_000
    assert HEARTBEAT_STALE_S == 15.0


def test_disk_low_compares_against_threshold(data_root, monkeypatch):
    monkeypatch.setattr(heartbeat, "free_bytes", lambda path: 1024)
    assert disk_low(data_root) is True
    monkeypatch.setattr(heartbeat, "free_bytes", lambda path: DISK_FREE_MIN_BYTES)
    assert disk_low(data_root) is False


def test_free_bytes_falls_back_to_nearest_existing_parent(data_root):
    assert heartbeat.free_bytes(data_root / "还没有" / "这一层") > 0


def test_supervision_env_roundtrip(monkeypatch):
    env = supervision_env(Supervision(restart_count=3, reconnects=5))
    assert json.loads(env[heartbeat.SUPERVISION_ENV]) == {"restart_count": 3, "reconnects": 5}
    assert supervision_state(env) == Supervision(3, 5)
    assert supervision_state({}) == Supervision(), "无监督启动即全 0"
    assert supervision_state(env | {"DANMU_INTEL_SUPERVISION": '{"restart_count": 1}'}) == Supervision(1, 0)


def test_supervision_state_rejects_garbage():
    with pytest.raises(ValueError):
        supervision_state({heartbeat.SUPERVISION_ENV: "[]"})
    with pytest.raises(json.JSONDecodeError):
        supervision_state({heartbeat.SUPERVISION_ENV: "{半截"})
