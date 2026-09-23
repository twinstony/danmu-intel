"""付费墙判定（需求 §6.7 / §6.10、AC-2、ADR-0009 / ADR-0015）。

判定**只由比赛状态机驱动**：`state == ended` → 公开（所有人可见）；否则付费（只向会员提供）。
文件名、路径、报告形态、发布时刻**都不参与判定** —— 蓝图曾按文件名正则匹配 Pro 页面，
造成「已结束的比赛仍被锁」；本模块从接口上就不给这条路：`visibility()` 只接受状态。

配套的两件事也在这里，因为它们是同一条判定：

- `may_read_content()` / `require_access()`：谁能读到报告正文（会员或已结束的比赛）；
- `PAYWALL_MARK`：付费页面上必须出现的标记（发布检查第 3 项认它，见
  `publish/checks.py`）。

付费正文**不进静态产物**：报告页在付费时一个段正文都不写（`report/html.py`），
正文只经 `publish/access.py` 的 `paid_report_content()` 出口返回。
"""

from __future__ import annotations

VISIBILITY_PUBLIC = "public"
VISIBILITY_PAID = "paid"
VISIBILITIES: tuple[str, ...] = (VISIBILITY_PUBLIC, VISIBILITY_PAID)

#: 比赛状态机里「已结束」的取值（`common/matches.py` 的 `MATCH_STATES` 之一）。
MATCH_STATE_ENDED = "ended"

#: 付费页面上必须出现的标记：读者知道为什么看不到正文，发布检查也据此判定付费墙齐全。
PAYWALL_MARK = "本页正文为会员内容"


class PaidAccessDenied(RuntimeError):
    """读者既不是会员、比赛也还没结束 —— 拿不到付费正文（HTTP 层在本票之外）。"""


def visibility(match_state: str) -> str:
    """比赛状态 → 页面可见性。**唯一输入是状态机**（需求 §6.7）。"""
    return VISIBILITY_PUBLIC if match_state == MATCH_STATE_ENDED else VISIBILITY_PAID


def is_paid(match_state: str) -> bool:
    return visibility(match_state) == VISIBILITY_PAID


def may_read_content(*, match_state: str, credential_verified: bool) -> bool:
    """能否读到报告正文：比赛已结束 → 所有人；否则只有凭据校验通过的人（会员）。"""
    return visibility(match_state) == VISIBILITY_PUBLIC or credential_verified


def require_access(*, match_state: str, credential_verified: bool) -> None:
    if not may_read_content(match_state=match_state, credential_verified=credential_verified):
        raise PaidAccessDenied(
            f"比赛状态 {match_state} 的正文只向会员提供：请凭凭据访问（比赛结束后自动转公开）"
        )
