"""发布批次：原子替换 + 账本 + 幂等 + 秒级回滚 + 结束转公开（设计 §11.3，ADR-0015 决策 4–7）。

一次发布的顺序（任一步失败都不上线）：

1. `build_site` 从库里汇总出整棵站点树（不碰线上产物）；
2. `run_checks` 跑 7 项检查，任一不通过 → 写 `releases(state='failed')` + 一条待投递报警，
   **一个条目都不换**（AC-8：线上仍是上一版且可用）；
3. 树指纹与上一批 `live` 相同 → 直接返回「无变化」（幂等：不重复提交、不重复部署）；
4. 生成到 `site/.staging/`，然后逐条目 `os.replace` 换进 `site/`（同分区、单条目瞬时），
   删掉线上多出来的条目，**最后**写 `release.json`（这一批已完整上线的标记）；
5. 提交并推送（`Publisher`）→ 记 Vercel 部署（`VercelClient`）→ 写 `releases(state='live')`，
   把上一批标成 `superseded`。

回滚两步（ADR-0015 决策 6）：① Vercel 即时回滚到目标批次的部署（秒级）；② `git revert`
掉当前坏版本的提交，让「仓库 = 线上」重新一致 —— ② 失败不回滚①（线上已经好了），只报警。

结束转公开（决策 7）：可见性只由 `matches.state` 派生，`sync_ended()` 检查上一批里**曾经
付费**的比赛，一旦转 `ended` 就自动再发布公开版；没有需要翻转的比赛就什么都不做（幂等）。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from danmu_intel.common import audit, notifications, paths
from danmu_intel.common.matches import get_match
from danmu_intel.publish.checks import failures, run_checks
from danmu_intel.publish.site import SiteBuild, SiteTree, build_site
from danmu_intel.publish.vercel import Deployment, VercelClient, VercelError
from danmu_intel.report.publish import CheckResult

STAGING_DIR = ".staging"
VERSION_FILE = "release.json"

STATE_LIVE = "live"
STATE_SUPERSEDED = "superseded"
STATE_ROLLED_BACK = "rolled_back"
STATE_FAILED = "failed"

ACTION_PUBLISH = "release.publish"
ACTION_ROLLBACK = "release.rollback"
ACTION_PUBLISH_FAILED = "release.failed"
KIND_PUBLISH_FAILED = "release.failed"
KIND_RECONCILE_FAILED = "release.reconcile_failed"


class ReleaseRefused(RuntimeError):
    """发布检查未通过 —— 站点保持上一版，一个条目都不换。"""

    def __init__(self, failed: tuple[CheckResult, ...]) -> None:
        self.failures = failed
        detail = "；".join(f"{item.label}：{item.detail}" for item in failed)
        super().__init__(f"发布被拒绝（{len(failed)} 项检查未通过）：{detail}")


def now_ms() -> int:
    return int(time.time() * 1000)


# —— 发布批次账本 ——


@dataclass(frozen=True, slots=True)
class Release:
    id: int
    version: int
    tree_digest: str
    state: str
    deployment_id: str | None
    deploy_ref: str | None
    paywalled_matches: tuple[int, ...]
    pages: tuple[str, ...]
    checks: tuple[dict[str, object], ...]
    created_at: int

    @property
    def deployed(self) -> bool:
        return bool(self.deployment_id)


def _to_release(row: sqlite3.Row) -> Release:
    return Release(
        id=int(row["id"]),
        version=int(row["version"]),
        tree_digest=row["tree_digest"],
        state=row["state"],
        deployment_id=row["deployment_id"],
        deploy_ref=row["deploy_ref"],
        paywalled_matches=tuple(json.loads(row["paywalled_matches"])),
        pages=tuple(json.loads(row["pages_json"])),
        checks=tuple(json.loads(row["checks_json"])),
        created_at=int(row["created_at"]),
    )


def list_releases(conn: sqlite3.Connection, *, limit: int | None = None) -> list[Release]:
    sql = "SELECT * FROM releases ORDER BY version DESC"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return [_to_release(row) for row in conn.execute(sql)]


def current_release(conn: sqlite3.Connection) -> Release | None:
    """当前线上批次（`state='live'`）——「线上是哪一版」的唯一真相源。"""
    row = conn.execute("SELECT * FROM releases WHERE state=? ORDER BY version DESC", (STATE_LIVE,)).fetchone()
    return None if row is None else _to_release(row)


def next_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS v FROM releases").fetchone()
    return int(row["v"] or 0) + 1


def _insert_release(
    conn: sqlite3.Connection,
    *,
    version: int,
    tree_digest: str,
    state: str,
    deployment_id: str | None,
    deploy_ref: str | None,
    paywalled_matches: Sequence[int],
    pages: Sequence[str],
    checks: Sequence[CheckResult],
    created_at: int,
) -> Release:
    cursor = conn.execute(
        """
        INSERT INTO releases(version, tree_digest, state, deployment_id, deploy_ref,
                             paywalled_matches, pages_json, checks_json, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version,
            tree_digest,
            state,
            deployment_id,
            deploy_ref,
            json.dumps(list(paywalled_matches)),
            json.dumps(list(pages), ensure_ascii=False),
            json.dumps([item.as_dict() for item in checks], ensure_ascii=False),
            created_at,
        ),
    )
    conn.commit()
    return _to_release(
        conn.execute("SELECT * FROM releases WHERE id=?", (int(cursor.lastrowid),)).fetchone()
    )


# —— 提交/推送与产物的落地 ——


class Publisher(Protocol):
    """把产物落地到版本库（`publish`）并把账本拨回去（`restore`）。"""

    def publish(self, site_root: Path, *, version: int) -> str | None:
        """提交并推送，返回可回滚的版本引用（git 提交号）；不适用时返回 `None`。"""
        ...

    def restore(self, ref: str | None) -> bool:
        """回滚跟进：把版本库拨回上一版（`git revert`）。返回账本是否已对齐。"""
        ...


class GitPublisher:
    """真实发布：`git add site && git commit && git push`；回滚跟进 `git revert`。"""

    def __init__(self, repo_root: Path, *, remote: str = "origin", branch: str = "master") -> None:
        self.repo_root = repo_root
        self.remote = remote
        self.branch = branch

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} 失败：{completed.stderr.strip() or completed.stdout.strip()}"
            )
        return completed.stdout.strip()

    def publish(self, site_root: Path, *, version: int) -> str:
        rel = site_root.resolve().relative_to(self.repo_root.resolve()).as_posix()
        self._git("add", rel)
        self._git("commit", "-m", f"release: 站点产物 v{version}（{rel}）")
        ref = self._git("rev-parse", "HEAD")
        self._git("push", self.remote, f"HEAD:{self.branch}")
        return ref

    def restore(self, ref: str | None) -> bool:
        if not ref:
            return False
        self._git("revert", "--no-edit", ref)
        self._git("push", self.remote, f"HEAD:{self.branch}")
        return True


class LocalPublisher:
    """`--no-deploy`：产物只落本地，不提交、不推送（因此没有可回滚的版本引用）。"""

    def __init__(self) -> None:
        self.calls: list[Path] = []

    def publish(self, site_root: Path, *, version: int) -> None:
        self.calls.append(site_root)
        return None

    def restore(self, ref: str | None) -> bool:
        return False


def stage_tree(tree: SiteTree, site_root: Path) -> Path:
    """把整棵树写进 `site/.staging/`（不触碰线上产物）。"""
    staging = site_root / STAGING_DIR
    if staging.exists():
        shutil.rmtree(staging)
    for rel_path, html in tree.files().items():
        target = staging / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")
    return staging


def _live_entries(site_root: Path) -> set[str]:
    return {
        path.relative_to(site_root).as_posix()
        for path in site_root.rglob("*")
        if path.is_file() and STAGING_DIR not in path.relative_to(site_root).parts
    }


def _prune_empty_dirs(site_root: Path) -> None:
    for path in sorted(site_root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir() and path.name != STAGING_DIR and not any(path.iterdir()):
            path.rmdir()


def swap_into_place(staging: Path, site_root: Path) -> None:
    """逐条目原子替换：先换页面，再删线上多出来的，最后写 `release.json`。"""
    staged = {
        path.relative_to(staging).as_posix() for path in staging.rglob("*") if path.is_file()
    }
    stale = _live_entries(site_root) - staged
    for rel_path in sorted(staged):
        target = site_root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging / rel_path, target)
    for rel_path in sorted(stale, reverse=True):
        (site_root / rel_path).unlink()
    shutil.rmtree(staging, ignore_errors=True)
    _prune_empty_dirs(site_root)


def write_version_file(site_root: Path, payload: Mapping[str, object]) -> None:
    """版本标识文件**最后**写（原子写）：它是「这一批已完整上线」的标记。

    内容只放**产物自己就能验证**的东西（`version` + `tree_digest` + 页面清单）：
    git 提交号与 Vercel 部署号在账本里（提交号只有提交之后才知道，写进产物会自指）。
    """
    target = site_root / VERSION_FILE
    tmp = site_root / f"{VERSION_FILE}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)


def site_version(site_root: Path) -> dict[str, object] | None:
    """读线上的版本标识（发布器写的 `release.json`）；没有则返回 `None`。"""
    target = site_root / VERSION_FILE
    if not target.exists():
        return None
    return json.loads(target.read_text(encoding="utf-8"))


# —— 发布上下文（注入缝）——


@dataclass(frozen=True, slots=True)
class ReleaseContext:
    """一次发布的外部依赖：产物目录、数据目录、提交器、Vercel 客户端。

    全部可注入，因此测试不需要网络、不需要 git、也不需要真的 Vercel 项目（AC-14）。
    """

    site_root: Path
    data_root: Path
    publisher: Publisher
    vercel: VercelClient
    actor: str = "release"
    deploy_timeout_s: float = 90.0
    poll_interval_s: float = 3.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    @classmethod
    def local(
        cls,
        *,
        actor: str = "release",
        site_root: Path | None = None,
        data_root: Path | None = None,
    ) -> "ReleaseContext":
        """`--no-deploy`：只出产物（不推 git、不调 Vercel，因此也不等部署）。"""
        from danmu_intel.publish.vercel import NoDeployClient

        return cls(
            site_root=site_root or paths.site_dir(),
            data_root=data_root or paths.data_dir(),
            publisher=LocalPublisher(),
            vercel=NoDeployClient(),
            actor=actor,
            deploy_timeout_s=0.0,
        )


@dataclass(frozen=True, slots=True)
class ReleaseOutcome:
    changed: bool
    release: Release
    checks: tuple[CheckResult, ...]
    paywalled_matches: tuple[int, ...]

    @property
    def version(self) -> int:
        return self.release.version


@dataclass(frozen=True, slots=True)
class RollbackResult:
    """回滚结果：线上批次 + 部署标识 + 账本是否已对齐。"""

    previous: Release  # 被回滚掉的那一版
    release: Release  # 现在线上的那一版
    deployment_id: str
    ref: str | None
    aligned: bool


def _seals(conn: sqlite3.Connection) -> dict[str, str]:
    """落盘文件 → 采集时封存的 SHA256（发布检查的加固项与来源锚点）。"""
    rows = conn.execute("SELECT rel_path, sha256 FROM danmu_segments").fetchall()
    return {row["rel_path"]: row["sha256"] for row in rows}


def await_deployment(ctx: ReleaseContext, ref: str | None) -> Deployment | None:
    """等 Vercel 出现**本次提交**对应的 production 部署（超时返回 `None`，不假装成功）。

    Vercel 由 git 集成在推送后异步构建，`latest()` 立刻拿到的是上一版；因此按提交号
    （`meta.githubCommitSha`）比对，最多等 `deploy_timeout_s`（本地模式为 0）。
    """
    deadline = ctx.clock() + ctx.deploy_timeout_s
    while True:
        deployment = ctx.vercel.latest()
        if deployment is not None and (ref is None or deployment.ref == ref):
            return deployment
        if ctx.clock() >= deadline:
            return None
        ctx.sleep(min(ctx.poll_interval_s, max(0.0, deadline - ctx.clock())))


def _paywalled(build: SiteBuild) -> tuple[int, ...]:
    from danmu_intel.common import paywall

    return tuple(
        match.id
        for match in build.facts.matches
        if paywall.visibility(match.state) == paywall.VISIBILITY_PAID
    )


# —— 发布 ——


def publish_site(
    conn: sqlite3.Connection,
    *,
    ctx: ReleaseContext,
    generated_at: int | None = None,
    reason: str = "manual",
) -> ReleaseOutcome:
    """原子发布一次：构建 → 检查 → 换产物 → 提交/部署 → 记账（幂等）。"""
    stamp = now_ms() if generated_at is None else generated_at
    build = build_site(conn, data_root=ctx.data_root, generated_at=stamp)
    checks = run_checks(build, data_root=ctx.data_root, seals=_seals(conn))
    failed = failures(checks)
    digest = build.tree.digest()
    paywalled = _paywalled(build)

    if failed:
        record = _insert_release(
            conn,
            version=next_version(conn),
            tree_digest=digest,
            state=STATE_FAILED,
            deployment_id=None,
            deploy_ref=None,
            paywalled_matches=paywalled,
            pages=build.tree.paths,
            checks=checks,
            created_at=stamp,
        )
        notifications.emit(
            conn,
            KIND_PUBLISH_FAILED,
            severity="critical",
            payload={
                "release": record.version,
                "reason": reason,
                "failed_checks": [item.key for item in failed],
                "detail": "；".join(f"{item.label}：{item.detail}" for item in failed),
                "site_kept": str(ctx.site_root),
            },
            timestamp=stamp,
        )
        audit.record(
            conn,
            actor=ctx.actor,
            action=ACTION_PUBLISH_FAILED,
            target=str(record.version),
            detail={"failed": [item.as_dict() for item in failed], "reason": reason},
            ts=stamp,
        )
        raise ReleaseRefused(failed)

    live = current_release(conn)
    if live is not None and live.tree_digest == digest:
        return ReleaseOutcome(
            changed=False,
            release=live,
            checks=checks,
            paywalled_matches=live.paywalled_matches,
        )

    version = next_version(conn)
    staging = stage_tree(build.tree, ctx.site_root)
    swap_into_place(staging, ctx.site_root)
    write_version_file(
        ctx.site_root,
        {
            "version": version,
            "tree_digest": digest,
            "reason": reason,
            "published_at": stamp,
            "pages": list(build.tree.paths),
            "paywalled_matches": list(paywalled),
        },
    )
    deploy_ref = ctx.publisher.publish(ctx.site_root, version=version)
    deployment = await_deployment(ctx, deploy_ref)
    if live is not None:
        conn.execute("UPDATE releases SET state=? WHERE id=?", (STATE_SUPERSEDED, live.id))
    release = _insert_release(
        conn,
        version=version,
        tree_digest=digest,
        state=STATE_LIVE,
        deployment_id=deployment.id if deployment else None,
        deploy_ref=deploy_ref,
        paywalled_matches=paywalled,
        pages=build.tree.paths,
        checks=checks,
        created_at=stamp,
    )
    audit.record(
        conn,
        actor=ctx.actor,
        action=ACTION_PUBLISH,
        target=str(version),
        detail={
            "reason": reason,
            "tree_digest": digest,
            "pages": len(build.tree.paths),
            "paywalled_matches": list(paywalled),
            "deployment_id": release.deployment_id,
            "deploy_ref": deploy_ref,
        },
        ts=stamp,
    )
    return ReleaseOutcome(
        changed=True, release=release, checks=checks, paywalled_matches=paywalled
    )


def sync_ended(
    conn: sqlite3.Connection, *, ctx: ReleaseContext, generated_at: int | None = None
) -> tuple[ReleaseOutcome, ...]:
    """比赛转 `ended` 后**自动**再发布公开版（FR-C5-10 / AC-2，幂等）。

    只在上一次发布里**曾经付费**的比赛真的转成 `ended` 时才发布；没有需要翻转的就什么都不做
    （因此反复调用不会产生新批次、也不会重复部署）。
    """
    live = current_release(conn)
    if live is None:
        return ()
    flipped = tuple(
        match_id
        for match_id in live.paywalled_matches
        if get_match(conn, match_id).state == "ended"
    )
    if not flipped:
        return ()
    return (
        publish_site(
            conn,
            ctx=ctx,
            generated_at=generated_at,
            reason=f"状态机转公开：比赛 {','.join(f'#{item}' for item in flipped)}",
        ),
    )


def rollback(
    conn: sqlite3.Connection,
    *,
    ctx: ReleaseContext,
    to_version: int | None = None,
    stamp: int | None = None,
) -> RollbackResult:
    """秒级回滚：① Vercel 即时回滚 ② `git revert` 跟进（失败只报警，不回滚①）。"""
    live = current_release(conn)
    if live is None:
        raise VercelError("没有任何线上发布批次，无法回滚")
    if not live.deployment_id:
        raise VercelError(
            f"线上批次 v{live.version} 没有部署标识（本地发布？）：无法即时回滚，"
            "请以「重新发布上一版产物」的方式对齐"
        )
    target = _rollback_target(conn, live, to_version)
    deployment = ctx.vercel.rollback(deployment_id=target.deployment_id or "")
    moment = now_ms() if stamp is None else stamp
    conn.execute("UPDATE releases SET state=? WHERE id=?", (STATE_ROLLED_BACK, live.id))
    conn.execute("UPDATE releases SET state=? WHERE id=?", (STATE_LIVE, target.id))
    conn.commit()
    aligned = False
    try:
        aligned = ctx.publisher.restore(live.deploy_ref)
    except Exception as exc:  # 账本对齐失败不回滚已经成功的即时回滚，只报警
        notifications.emit(
            conn,
            KIND_RECONCILE_FAILED,
            severity="warning",
            payload={
                "release": live.version,
                "error": str(exc),
                "hint": "线上已即时回滚，但版本库还没拨回去；请手工 git revert",
            },
            timestamp=moment,
        )
    audit.record(
        conn,
        actor=ctx.actor,
        action=ACTION_ROLLBACK,
        target=str(target.version),
        detail={
            "from": live.version,
            "to": target.version,
            "deployment_id": deployment.id,
            "aligned": aligned,
        },
        ts=moment,
    )
    return RollbackResult(
        previous=live,
        release=replace(target, state=STATE_LIVE),
        deployment_id=deployment.id,
        ref=live.deploy_ref,
        aligned=aligned,
    )


def _rollback_target(
    conn: sqlite3.Connection, live: Release, to_version: int | None
) -> Release:
    if to_version is not None:
        row = conn.execute("SELECT * FROM releases WHERE version=?", (to_version,)).fetchone()
        if row is None:
            raise VercelError(f"没有发布批次 v{to_version}")
        target = _to_release(row)
    else:
        row = conn.execute(
            "SELECT * FROM releases WHERE version < ? AND state IN (?, ?) ORDER BY version DESC",
            (live.version, STATE_SUPERSEDED, STATE_ROLLED_BACK),
        ).fetchone()
        if row is None:
            raise VercelError(f"线上批次 v{live.version} 之前没有可回滚的批次")
        target = _to_release(row)
    if not target.deployment_id:
        raise VercelError(f"批次 v{target.version} 没有部署标识，无法回滚到它")
    return target
