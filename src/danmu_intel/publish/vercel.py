"""Vercel 部署查询与**即时回滚**（设计 §11.3，ADR-0015 决策 6 / 决策 8）。

两个动作，仅此两个（缝越小越好）：

| 动作 | Vercel REST |
|---|---|
| `latest()` | `GET /v6/deployments?projectId=…&target=production&limit=1` |
| `rollback(deployment_id)` | `POST /v10/projects/{project}/rollback/{deploymentId}` |

- 客户端**一次配好**（项目、团队、token），因此调用方只有两个方法；API 形状变化只动这一处。
- **凭据只在仓库外 `.env`**（`VERCEL_TOKEN` / `VERCEL_PROJECT_ID` / 可选 `VERCEL_TEAM_ID`、
  `VERCEL_API_BASE`，0600，永不入 git —— AC-12 / NFR-S-4）；异常消息里只有状态码与响应片段，
  绝不回显 token。
- HTTP 走 stdlib `urllib.request`（不新增依赖），`transport` 可注入 → **断网可跑**（AC-14）。
- `--no-deploy` 的本地模式用 `NoDeployClient`：查不到部署、也不能回滚（如实报错，不假装成功）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from danmu_intel.common import credentials

DEFAULT_API_BASE = "https://api.vercel.com"
TOKEN_KEY = "VERCEL_TOKEN"
PROJECT_KEY = "VERCEL_PROJECT_ID"
TEAM_KEY = "VERCEL_TEAM_ID"
BASE_KEY = "VERCEL_API_BASE"


class VercelError(RuntimeError):
    """与 Vercel 交互失败（配置缺失 / 网络 / 非 2xx）。消息里不含任何凭据。"""


@dataclass(frozen=True, slots=True)
class Deployment:
    """一次部署的可验证标识（回滚后靠它说「线上是哪一版」）。"""

    id: str
    url: str | None = None
    target: str | None = None
    created_at: int | None = None
    ref: str | None = None  # git 提交（Vercel 的 meta.githubCommitSha）

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, fallback_id: str | None = None
    ) -> "Deployment":
        deployment_id = payload.get("uid") or payload.get("id") or fallback_id
        if not deployment_id:
            raise VercelError("Vercel 响应里没有部署标识（uid/id）")
        meta = payload.get("meta")
        commit = meta.get("githubCommitSha") if isinstance(meta, Mapping) else None
        return cls(
            id=str(deployment_id),
            url=str(payload["url"]) if payload.get("url") else None,
            target=str(payload["target"]) if payload.get("target") else None,
            created_at=int(payload["createdAt"]) if payload.get("createdAt") else None,
            ref=str(commit) if commit else None,
        )


class VercelClient(Protocol):
    def latest(self) -> Deployment | None: ...

    def rollback(self, *, deployment_id: str) -> Deployment: ...


#: `(method, path) -> JSON`；缺省实现用 urllib 带 token 发请求。
Transport = Callable[[str, str], Mapping[str, Any]]


class VercelAPI:
    """真实客户端。`transport` 可注入（测试注入假 transport，因此不需要网络）。"""

    def __init__(
        self,
        *,
        token: str | None = None,
        project: str | None = None,
        team_id: str | None = None,
        base_url: str = DEFAULT_API_BASE,
        transport: Transport | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not token:
            raise VercelError(
                f"缺少凭据 {TOKEN_KEY}：请写入仓库外 .env（chmod 600）或设进进程环境；"
                "只想本地出产物请用 --no-deploy"
            )
        if not project:
            raise VercelError(f"缺少配置 {PROJECT_KEY}：请写入仓库外 .env（chmod 600）或设进进程环境")
        self._token = token
        self._project = project
        self._team_id = team_id
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport or self._http

    @property
    def project(self) -> str:
        return self._project

    @property
    def base_url(self) -> str:
        return self._base_url

    def _scoped(self, query: dict[str, str]) -> str:
        if self._team_id:
            query["teamId"] = self._team_id
        return urllib.parse.urlencode(query)

    def _http(self, method: str, path: str) -> Mapping[str, Any]:
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            method=method,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                body = response.read().decode("utf-8")
                status = response.status
        except urllib.error.HTTPError as exc:  # 4xx/5xx：消息里只有状态码与响应片段
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise VercelError(f"Vercel {method} {path} 失败：HTTP {exc.code}｜{detail}") from None
        except urllib.error.URLError as exc:
            raise VercelError(f"Vercel {method} {path} 连不上：{exc.reason}") from None
        if status >= 400:
            raise VercelError(f"Vercel {method} {path} 失败：HTTP {status}")
        if not body.strip():
            return {}
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise VercelError(f"Vercel {method} {path} 返回的不是 JSON（{len(body)} 字节）") from None
        if not isinstance(payload, Mapping):
            raise VercelError(f"Vercel {method} {path} 返回了非对象 JSON")
        return payload

    def latest(self) -> Deployment | None:
        """当前线上（production）部署；从未部署过则返回 `None`。"""
        query = self._scoped({"projectId": self._project}) + "&target=production&limit=1"
        payload = self._transport("GET", f"/v6/deployments?{query}")
        deployments = payload.get("deployments") or []
        if not isinstance(deployments, list) or not deployments:
            return None
        return Deployment.from_payload(deployments[0])

    def rollback(self, *, deployment_id: str) -> Deployment:
        """即时回滚到指定部署（秒级，设计 §11.3 第 5 步的①）。"""
        project = urllib.parse.quote(self._project)
        target = urllib.parse.quote(deployment_id)
        path = f"/v10/projects/{project}/rollback/{target}"
        if self._team_id:
            path += f"?{urllib.parse.urlencode({'teamId': self._team_id})}"
        payload = self._transport("POST", path)
        return Deployment.from_payload(payload, fallback_id=deployment_id)


class NoDeployClient:
    """`--no-deploy` 的本地模式：查不到部署，也不能回滚（如实报错，不假装成功）。"""

    def latest(self) -> Deployment | None:
        return None

    def rollback(self, *, deployment_id: str) -> Deployment:
        raise VercelError(
            "本地模式（--no-deploy）没有 Vercel 部署可回滚：请用 --deploy 走真实部署，"
            "或以「重新发布上一版产物」的方式对齐"
        )


def client_from_credentials(*, path=None, environ: Mapping[str, str] | None = None) -> VercelAPI:
    """按仓库外 `.env` 造真实客户端（缺 `VERCEL_TOKEN` / `VERCEL_PROJECT_ID` 即报错）。"""
    return VercelAPI(
        token=credentials.require_secret(TOKEN_KEY, path=path),
        project=credentials.require_secret(PROJECT_KEY, path=path),
        team_id=credentials.get_secret(TEAM_KEY, path=path, environ=environ),
        base_url=credentials.get_secret(BASE_KEY, path=path, environ=environ) or DEFAULT_API_BASE,
    )
