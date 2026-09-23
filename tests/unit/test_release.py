"""发布批次测试：原子发布、失败保留上一版（AC-8）、幂等、秒级回滚、结束转公开（AC-2）。

全程断网：`Publisher` 与 `VercelClient` 都是注入缝（AC-14）。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from danmu_intel.common import paywall
from danmu_intel.common.matches import set_match_state
from danmu_intel.pipeline import generate_and_publish
from danmu_intel.publish import release
from danmu_intel.publish.release import (
    STATE_FAILED,
    STATE_LIVE,
    STATE_ROLLED_BACK,
    STATE_SUPERSEDED,
    LocalPublisher,
    ReleaseContext,
    ReleaseRefused,
    current_release,
    list_releases,
    publish_site,
    rollback,
    site_version,
    sync_ended,
)
from danmu_intel.publish.site import report_page_path
from danmu_intel.publish.vercel import Deployment, NoDeployClient, VercelAPI, VercelError

GENERATED_AT = 1_790_064_400_000


class FakeVercel:
    """假 Vercel 客户端：维护「线上部署」指针，回滚就是把它拨回去。"""

    def __init__(self, *, refs: bool = True) -> None:
        self.deployments: list[Deployment] = []
        self.current: Deployment | None = None
        self.rollbacks: list[str] = []
        self.refs = refs

    def deploy(self, *, ref: str | None, deployment_id: str) -> Deployment:
        deployment = Deployment(
            id=deployment_id,
            url=f"https://{deployment_id}.vercel.app",
            target="production",
            ref=ref if self.refs else None,
        )
        self.deployments.append(deployment)
        self.current = deployment
        return deployment

    def latest(self) -> Deployment | None:
        return self.current

    def rollback(self, *, deployment_id: str) -> Deployment:
        self.rollbacks.append(deployment_id)
        target = next(item for item in self.deployments if item.id == deployment_id)
        self.current = target
        return target


class FakePublisher:
    """假提交器：记录调用，并在 revert 时把产物目录恢复成提交前的样子（像 git revert）。"""

    def __init__(self, site_root: Path) -> None:
        self.site_root = site_root
        self.commits: list[tuple[int, str]] = []
        self.reverts: list[str | None] = []
        self.snapshots: dict[str, dict[str, str]] = {}

    def publish(self, site_root: Path, *, version: int) -> str:
        ref = f"sha-v{version}"
        self.snapshots[ref] = self._snapshot()
        self.commits.append((version, ref))
        return ref

    def restore(self, ref: str | None) -> bool:
        """`git revert <ref>` 的等价物：把树恢复成 ref 的**父提交**的样子。"""
        self.reverts.append(ref)
        refs = [item for _, item in self.commits]
        if ref is None or ref not in refs:
            return False
        index = refs.index(ref)
        if index == 0:
            return False  # 第一个提交没有父提交，revert 不了
        self._restore(self.snapshots[refs[index - 1]])
        return True

    def _snapshot(self) -> dict[str, str]:
        return {
            path.relative_to(self.site_root).as_posix(): path.read_text(encoding="utf-8")
            for path in self.site_root.rglob("*")
            if path.is_file()
        }

    def _restore(self, snapshot: dict[str, str]) -> None:
        for path in list(self.site_root.rglob("*")):
            if path.is_file():
                path.unlink()
        for rel, text in snapshot.items():
            target = self.site_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")


@pytest.fixture
def ctx(site_root, data_root) -> ReleaseContext:
    """注入假的提交器与 Vercel 客户端（发布器不碰真 git、不碰网络）。"""
    site_root.mkdir(parents=True, exist_ok=True)
    vercel = FakeVercel()

    class CountingPublisher(FakePublisher):
        def publish(self, site_root: Path, *, version: int) -> str:
            ref = super().publish(site_root, version=version)
            vercel.deploy(ref=ref, deployment_id=f"dpl-v{version}")
            return ref

    context = ReleaseContext(
        site_root=site_root,
        data_root=data_root,
        publisher=CountingPublisher(site_root),
        vercel=vercel,
        actor="测试发布器",
    )
    context.vercel.fake = vercel  # type: ignore[attr-defined]
    return context


def publish_reports(
    ledger, *, kinds: tuple[str, ...] = ("live_brief",), brief_games: tuple[int, ...] = (1,)
) -> None:
    for kind in kinds:
        kwargs = {"completed_games": brief_games} if kind == "live_brief" else {}
        generate_and_publish(
            ledger.conn,
            ledger.match_id,
            kind=kind,
            data_root=ledger.data_root,
            generated_at=GENERATED_AT,
            **kwargs,
        )


# —— 原子发布 ——


def test_publish_writes_the_whole_tree_and_records_a_live_batch(ledger, ctx):
    publish_reports(ledger)
    outcome = publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT)

    assert outcome.changed is True
    assert outcome.version == 1
    assert (ctx.site_root / "index.html").exists()
    assert (ctx.site_root / "matches/1/index.html").exists()
    assert not (ctx.site_root / ".staging").exists(), "暂存目录不能留在产物里"
    version = site_version(ctx.site_root)
    assert version is not None
    assert version["version"] == 1
    assert version["tree_digest"] == outcome.release.tree_digest
    assert outcome.release.state == STATE_LIVE
    assert outcome.release.deployment_id == "dpl-v1"
    assert outcome.release.deploy_ref == "sha-v1"
    assert all(item.passed for item in outcome.checks)
    assert [item.version for item in list_releases(ledger.conn)] == [1]


def test_publish_is_idempotent_when_nothing_changed(ledger, ctx):
    publish_reports(ledger)
    first = publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT)
    publisher = ctx.publisher
    second = publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT + 1000)

    assert second.changed is False
    assert second.version == first.version
    assert publisher.commits == [(1, "sha-v1")], "树没变就不该再提交一次"
    assert len(list_releases(ledger.conn)) == 1


def test_another_publish_creates_a_new_version_and_supersedes_the_previous(ledger, ctx):
    publish_reports(ledger)
    first = publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT)
    publish_reports(ledger, kinds=("full",))
    second = publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT + 1000)

    assert second.version == 2 and second.changed
    states = {item.version: item.state for item in list_releases(ledger.conn)}
    assert states == {1: STATE_SUPERSEDED, 2: STATE_LIVE}
    assert current_release(ledger.conn).version == 2
    assert site_version(ctx.site_root)["version"] == 2


# —— AC-8：检查不过 → 线上仍是上一版 ——


def test_failed_checks_keep_the_previous_site_live(ledger, ctx):
    publish_reports(ledger)
    publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT)
    before = {path: path.read_bytes() for path in ctx.site_root.rglob("*") if path.is_file()}

    # 注入缺陷：账本里的报告被删掉一段（旧版行 / 人为改动）
    row = ledger.conn.execute(
        "SELECT id, content_json FROM reports WHERE kind='live_brief' AND state='published'"
    ).fetchone()
    payload = json.loads(row["content_json"])
    payload["segments"] = [item for item in payload["segments"] if item["no"] != 9]
    ledger.conn.execute(
        "UPDATE reports SET content_json=? WHERE id=?",
        (json.dumps(payload, ensure_ascii=False), row["id"]),
    )
    ledger.conn.commit()

    with pytest.raises(ReleaseRefused) as excinfo:
        publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT + 2000)
    assert any(item.key == "report_segments_complete" for item in excinfo.value.failures)

    after = {path: path.read_bytes() for path in ctx.site_root.rglob("*") if path.is_file()}
    assert after == before, "检查不通过时一个条目都不许换"
    assert site_version(ctx.site_root)["version"] == 1

    rows = list_releases(ledger.conn)
    assert rows[0].state == STATE_FAILED and rows[0].id != current_release(ledger.conn).id
    assert current_release(ledger.conn).version == 1
    assert ctx.publisher.commits == [(1, "sha-v1")]

    # 失败要报警（FR-C5-9），而不是静默
    pending = ledger.conn.execute("SELECT kind, severity, payload_json FROM notifications").fetchall()
    assert [row["kind"] for row in pending] == ["release.failed"]
    assert pending[0]["severity"] == "critical"
    assert "report_segments_complete" in pending[0]["payload_json"]
    audit = ledger.conn.execute(
        "SELECT action FROM audit_log WHERE action='release.failed'"
    ).fetchone()
    assert audit is not None


def test_failed_publish_leaves_no_staging_behind(ledger, ctx):
    publish_reports(ledger)
    row = ledger.conn.execute("SELECT id, content_json FROM reports WHERE kind='live_brief'").fetchone()
    payload = json.loads(row["content_json"])
    payload["segments"] = payload["segments"][:-1]
    ledger.conn.execute(
        "UPDATE reports SET content_json=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), row["id"])
    )
    ledger.conn.commit()
    with pytest.raises(ReleaseRefused):
        publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT)
    assert not (ctx.site_root / ".staging").exists()
    assert not (ctx.site_root / "index.html").exists()


def test_operator_files_in_the_site_dir_are_left_alone(ledger, ctx):
    """`site/` 里运维手工放的文件（vercel.json、robots.txt）不是发布器的产物，不该被删。"""
    keep = ctx.site_root / "vercel.json"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text('{"cleanUrls": true}', encoding="utf-8")
    publish_reports(ledger)
    publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT)
    publish_reports(ledger, kinds=("full",))
    publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT + 1000)
    assert keep.read_text(encoding="utf-8") == '{"cleanUrls": true}'


def test_pages_dropped_from_the_tree_are_removed_from_the_live_tree(ledger, ctx):
    publish_reports(ledger)
    publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT)
    dropped = ctx.site_root / report_page_path(ledger.match_id, "live_brief")
    assert dropped.exists()

    # 报告从账本里下线 → 新树里没有这一页 → 发布时把它从线上删掉
    ledger.conn.execute("UPDATE reports SET state='failed' WHERE kind='live_brief'")
    ledger.conn.commit()
    outcome = publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT + 1000)
    assert outcome.version == 2
    assert not dropped.exists()
    assert (ctx.site_root / "index.html").exists()


# —— AC-2：结束转公开 ——


def test_ended_match_flips_every_page_to_public_and_republishes_once(three_game_ledger, ctx):
    """AC-2：比赛进行中页面只向会员提供；状态机转 ended 后全部自动转公开。"""
    ledger = three_game_ledger
    publish_reports(ledger, brief_games=ledger.completed_games)
    published = publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT)
    assert published.paywalled_matches == (ledger.match_id,)
    assert paywall.PAYWALL_MARK in (ctx.site_root / report_page_path(ledger.match_id, "live_brief")).read_text(
        encoding="utf-8"
    )
    assert sync_ended(ledger.conn, ctx=ctx) == (), "比赛没结束就不该有任何再发布"

    # 状态机写入（唯一的人工动作）→ 自动再发布公开版
    set_match_state(ledger.conn, ledger.match_id, state="ended")
    outcomes = sync_ended(ledger.conn, ctx=ctx, generated_at=GENERATED_AT + 3000)
    assert len(outcomes) == 1 and outcomes[0].changed
    assert outcomes[0].paywalled_matches == ()
    for kind in ("live_brief",):
        page = (ctx.site_root / report_page_path(ledger.match_id, kind)).read_text(encoding="utf-8")
        assert paywall.PAYWALL_MARK not in page, f"{kind} 页面应当已转公开"
    assert "会员（付费）" not in (ctx.site_root / f"matches/{ledger.match_id}/index.html").read_text(
        encoding="utf-8"
    )
    assert site_version(ctx.site_root)["version"] == 2

    # 幂等：再跑一次什么都不做（不重复提交、不重复部署）
    assert sync_ended(ledger.conn, ctx=ctx, generated_at=GENERATED_AT + 4000) == ()
    assert [item.version for item in list_releases(ledger.conn)] == [2, 1]
    assert ctx.publisher.commits == [(1, "sha-v1"), (2, "sha-v2")]


def test_sync_ended_without_any_release_does_nothing(ledger, ctx):
    assert sync_ended(ledger.conn, ctx=ctx) == ()


# —— 秒级回滚 ——


def test_rollback_returns_to_the_previous_version(ledger, ctx):
    publish_reports(ledger)
    publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT)
    publish_reports(ledger, kinds=("full",))
    second = publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT + 1000)
    assert site_version(ctx.site_root)["version"] == second.version == 2

    result = rollback(ledger.conn, ctx=ctx, stamp=GENERATED_AT + 2000)

    assert result.release.version == 1
    assert result.previous.version == 2
    assert result.deployment_id == "dpl-v1"
    vercel = ctx.vercel
    assert vercel.current.id == "dpl-v1"  # 线上（Vercel）拨回上一版
    assert vercel.rollbacks == ["dpl-v1"]
    assert current_release(ledger.conn).version == 1  # 账本的 live 指针跟着换
    states = {item.version: item.state for item in list_releases(ledger.conn)}
    assert states == {2: STATE_ROLLED_BACK, 1: STATE_LIVE}
    assert result.ref == "sha-v2" and result.aligned is True
    assert ctx.publisher.reverts == ["sha-v2"]
    assert site_version(ctx.site_root)["version"] == 1  # 产物也拨回去了（git revert 的等价物）
    actions = [row["action"] for row in ledger.conn.execute("SELECT action FROM audit_log")]
    assert "release.rollback" in actions


def test_rollback_to_an_explicit_version(ledger, ctx):
    publish_reports(ledger)
    publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT)
    publish_reports(ledger, kinds=("full",))
    publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT + 1000)
    publish_reports(ledger, kinds=("review",))
    publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT + 2000)

    result = rollback(ledger.conn, ctx=ctx, to_version=1)
    assert result.release.version == 1
    assert ctx.vercel.current.id == "dpl-v1"


def test_rollback_reports_when_git_revert_fails(ledger, ctx):
    publish_reports(ledger)
    publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT)
    publish_reports(ledger, kinds=("full",))
    publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT + 1000)

    class FailingRestore(type(ctx.publisher)):  # type: ignore[misc]
        def restore(self, ref):
            raise RuntimeError("git revert 冲突")

    ctx = replace(ctx, publisher=FailingRestore(ctx.site_root))
    result = rollback(ledger.conn, ctx=ctx)

    assert result.aligned is False
    assert ctx.vercel.current.id == "dpl-v1", "账本对齐失败不回滚已经成功的即时回滚"
    pending = ledger.conn.execute("SELECT kind FROM notifications").fetchall()
    assert [row["kind"] for row in pending] == ["release.reconcile_failed"]


def test_rollback_refuses_when_there_is_nothing_to_go_back_to(ledger, ctx):
    with pytest.raises(VercelError):
        rollback(ledger.conn, ctx=ctx)

    publish_reports(ledger)
    publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT)
    with pytest.raises(VercelError):
        rollback(ledger.conn, ctx=ctx)
    with pytest.raises(VercelError):
        rollback(ledger.conn, ctx=ctx, to_version=99)
    with pytest.raises(VercelError):
        rollback(ledger.conn, ctx=ctx, to_version=1)  # 已经是线上版本


def test_rollback_refuses_without_a_deployment_id(ledger, ctx):
    publish_reports(ledger)
    local = replace(ctx, vercel=NoDeployClient(), deploy_timeout_s=0.0)
    publish_site(ledger.conn, ctx=local, generated_at=GENERATED_AT)
    assert current_release(ledger.conn).deployment_id is None
    with pytest.raises(VercelError):
        rollback(ledger.conn, ctx=local)


# —— 本地模式与部署等待 ——


def test_local_context_publishes_without_git_or_vercel(ledger, site_root, data_root):
    publish_reports(ledger)
    ctx = ReleaseContext.local(actor="本地", site_root=site_root, data_root=data_root)
    outcome = publish_site(ledger.conn, ctx=ctx, generated_at=GENERATED_AT)
    assert isinstance(ctx.publisher, LocalPublisher)
    assert outcome.changed and outcome.release.deployment_id is None
    assert (site_root / "index.html").exists()
    assert site_version(site_root)["version"] == 1


def test_await_deployment_waits_for_the_matching_commit(ledger, site_root, data_root):
    publish_reports(ledger)
    vercel = FakeVercel()
    vercel.deploy(ref="sha-old", deployment_id="dpl-old")
    clock = {"now": 0.0}
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds
        if len(sleeps) == 1:  # 第二次轮询时新部署才出现
            vercel.deploy(ref="sha-v1", deployment_id="dpl-v1")

    ctx = ReleaseContext(
        site_root=site_root,
        data_root=data_root,
        publisher=LocalPublisher(),
        vercel=vercel,
        clock=lambda: clock["now"],
        sleep=fake_sleep,
        deploy_timeout_s=30.0,
        poll_interval_s=3.0,
    )
    assert release.await_deployment(ctx, None).id == "dpl-old"
    assert release.await_deployment(ctx, "sha-v1").id == "dpl-v1"


def test_await_deployment_gives_up_at_the_deadline(ledger, site_root, data_root):
    vercel = FakeVercel()
    clock = {"now": 0.0}

    def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds

    ctx = ReleaseContext(
        site_root=site_root,
        data_root=data_root,
        publisher=LocalPublisher(),
        vercel=vercel,
        clock=lambda: clock["now"],
        sleep=fake_sleep,
        deploy_timeout_s=5.0,
        poll_interval_s=3.0,
    )
    assert release.await_deployment(ctx, "sha-v1") is None


def test_version_written_last_is_the_marker_of_a_complete_batch(ledger, ctx):
    """`release.json` 只在整棵树换完之后出现（读者不会看到半成品发布）。"""
    publish_reports(ledger)
    staging = release.stage_tree(
        release.build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT).tree,
        ctx.site_root,
    )
    assert staging.exists()
    assert not (ctx.site_root / release.VERSION_FILE).exists()
    assert not (ctx.site_root / "index.html").exists()
    release.swap_into_place(staging, ctx.site_root)
    assert (ctx.site_root / "index.html").exists()
    assert not (ctx.site_root / release.VERSION_FILE).exists(), "版本标识由发布流程最后写"
    release.write_version_file(ctx.site_root, {"version": 1, "tree_digest": "x"})
    assert site_version(ctx.site_root) == {"version": 1, "tree_digest": "x"}


def test_site_version_is_none_before_the_first_publish(site_root):
    site_root.mkdir(parents=True, exist_ok=True)
    assert site_version(site_root) is None


# —— 真实客户端的 HTTP 形状（注入假 transport，不联网）——


def test_vercel_client_uses_the_two_documented_endpoints():
    calls: list[tuple[str, str]] = []

    def transport(method: str, path: str):
        calls.append((method, path))
        if method == "GET":
            return {"deployments": [{"uid": "dpl-9", "url": "x.vercel.app", "target": "production",
                                     "createdAt": 1790064000000, "meta": {"githubCommitSha": "sha-9"}}]}
        return {"uid": "dpl-8", "url": "y.vercel.app", "target": "production"}

    api = VercelAPI(token="fake-token", project="danmu-intel", team_id="team-1", transport=transport)
    latest = api.latest()
    assert latest is not None and latest.id == "dpl-9" and latest.ref == "sha-9"
    rolled = api.rollback(deployment_id="dpl-8")
    assert rolled.id == "dpl-8"
    assert calls[0] == (
        "GET",
        "/v6/deployments?projectId=danmu-intel&teamId=team-1&target=production&limit=1",
    )
    assert calls[1] == ("POST", "/v10/projects/danmu-intel/rollback/dpl-8?teamId=team-1")


def test_vercel_client_surfaces_api_failures():
    def transport(method: str, path: str):
        raise VercelError("HTTP 403")

    api = VercelAPI(token="fake-token", project="p", transport=transport)
    with pytest.raises(VercelError):
        api.latest()


def test_vercel_client_requires_credentials():
    with pytest.raises(VercelError):
        VercelAPI(token=None, project="p")
    with pytest.raises(VercelError):
        VercelAPI(token="t", project=None)


def test_no_deploy_client_cannot_rollback():
    client = NoDeployClient()
    assert client.latest() is None
    with pytest.raises(VercelError):
        client.rollback(deployment_id="dpl-1")


def test_deployment_payload_requires_an_identifier():
    with pytest.raises(VercelError):
        Deployment.from_payload({})
    assert Deployment.from_payload({"id": 7}).id == "7"
