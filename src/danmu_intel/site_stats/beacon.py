"""站点统计的上报口径（C7 / AC-9，设计 §13、ADR-0018）。

浏览器（或任何客户端）打过来一条轻量请求：`page` 是站点树里的页面路径。这一层把它变成
`stats_events` 里的一行 —— 同时把三件事定死：

1. **独立访客口径是 `sha256(每日盐 + IP + UA)`**（设计 §13）。盐是 64 位十六进制定长串，
   因此「拼接」没有歧义；IP 与 UA **只在内存里活一次**，进库的只有哈希，库表里没有
   IP / UA / 联系方式字段（AC-9：统计答不了「具体是谁」）。
2. **每日换盐**：盐按本地日历日生成（`secrets.token_hex`），只保留当天那一行 ——
   新的一天第一次上报就把旧盐删掉，跨日旧哈希再也算不回来（AC-9 后半段）。
   因此「每日换盐」不需要 cron：没有请求就没有数据，有请求的那一刻盐已经是当天的。
3. **付费页分类随事件落盘**：`paid` 列记的是「这位访客当时看到的页面是不是付费页」，
   判定只由比赛状态机给出（`paywall.visibility(matches.state)`，ADR-0009）。比赛当天
   结束后再回填汇总，也不会把「昨天的付费页访问」改写成公开页访问。

域名/路径从来不是判据：`matches/<id>/index.html` 与 `matches/<id>/<形态>.html` 里的
`<id>` 只用来找到**哪场比赛**，付费与否看那场比赛的状态。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from datetime import datetime

from danmu_intel.common import paywall
from danmu_intel.report.forms import FORM_KINDS

#: 页面路径的长度上限（上报里出现超长路径说明对方不是我们的页面）。
MAX_PAGE_LEN = 200

#: 只认站点树里真实存在的两类比赛页（比赛页与报告页）；其余路径一律按公开页计。
MATCH_PAGE_RE = re.compile(r"^matches/(?P<match_id>\d+)/(?P<leaf>[A-Za-z0-9_.-]+)\.html$")
MATCH_PAGE_LEAVES = ("index",) + FORM_KINDS


def now_ms() -> int:
    return int(time.time() * 1000)


def day_of(ts_ms: int) -> str:
    """本地日历日 `YYYY-MM-DD`（单机部署，与 `chain.quota.day_key` 同一口径）。"""
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")


def normalize_page(page: object) -> str:
    """把上报的页面路径收成站点树里的相对路径；不像站内路径就报错。

    只做形状检查（去协议与两端斜杠、丢查询串与锚点、拒绝上跳与超长），不做「页面是否存在」
    的存在性检查 —— 统计不该因为一次发布删了页面就丢历史路径。
    """
    text = str(page or "").strip()
    if not text:
        raise ValueError("页面路径不能为空")
    if "://" in text or text.startswith("//"):
        raise ValueError("页面路径应为站内相对路径，不带协议")
    text = text.split("#", 1)[0].split("?", 1)[0].strip("/")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"非法的页面路径：{page!r}")
    path = "/".join(parts)
    if len(path) > MAX_PAGE_LEN:
        raise ValueError(f"页面路径过长（上限 {MAX_PAGE_LEN}）：{path[:60]}…")
    return path


def current_salt(conn: sqlite3.Connection, *, at_ms: int) -> tuple[str, str]:
    """当天盐：`(day, salt)`。换日即换盐，旧盐**即时删除**（只留一行）。"""
    day = day_of(at_ms)
    row = conn.execute("SELECT day, salt FROM stats_salt WHERE day=?", (day,)).fetchone()
    if row is not None:
        return day, str(row["salt"])
    salt = secrets.token_hex(32)
    conn.execute("DELETE FROM stats_salt")
    conn.execute("INSERT INTO stats_salt(day, salt) VALUES(?, ?)", (day, salt))
    conn.commit()
    return day, salt


def visitor_hash(salt: str, ip: str, user_agent: str) -> str:
    """独立访客口径：`sha256(每日盐 + IP + UA)`（盐定长，拼接无歧义）。"""
    return hashlib.sha256(f"{salt}{ip}{user_agent}".encode("utf-8")).hexdigest()


def page_visibility(conn: sqlite3.Connection, page: str) -> str:
    """页面可见性：比赛页/报告页看那场比赛的状态机，其余页面看公开。"""
    match = MATCH_PAGE_RE.match(page)
    if match is None or match["leaf"] not in MATCH_PAGE_LEAVES:
        return paywall.VISIBILITY_PUBLIC
    row = conn.execute(
        "SELECT state FROM matches WHERE id=?", (int(match["match_id"]),)
    ).fetchone()
    if row is None:
        return paywall.VISIBILITY_PUBLIC
    return paywall.visibility(str(row["state"]))


def is_paid_page(conn: sqlite3.Connection, page: str) -> bool:
    return page_visibility(conn, page) == paywall.VISIBILITY_PAID


#: 上报路径（挂在对外 API 基址下：`<api_base>/api/stats/beacon`）。
BEACON_PATH = "/api/stats/beacon"


def _js_string(value: str) -> str:
    """嵌进 `<script>` 的字符串：JSON 转义 + 断掉 `</script>` 这条路。"""
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


def snippet(page_path: str, api_base: str) -> str:
    """页面上的自建 beacon：一行内联脚本，向**自己的** API 报一次页面访问。

    - 没配 API 基址就**不写脚本**：宁可不统计，也不往不知道的地址发请求，
      静态站因此保持零脚本（NFR-A-2）；
    - `navigator.sendBeacon` 送的是 `text/plain`（跨源也免预检），响应被丢弃
      —— 统计不需要回执，也不给页面任何可读的返回；
    - 脚本里只有自己的地址与**页面路径**：不带 referrer、不带 cookie 之外的信息，
      更没有任何第三方地址（FR-C7-4 / NFR-P-3）。
    """
    if not api_base.strip():
        return ""
    target = f"{api_base.rstrip('/')}{BEACON_PATH}"
    payload = "{" + f"page:{_js_string(page_path)}" + "}"
    return f"<script>navigator.sendBeacon({_js_string(target)}, JSON.stringify({payload}));</script>"


def record(
    conn: sqlite3.Connection,
    *,
    page: object,
    ip: str,
    user_agent: str = "",
    member_id: int | None = None,
    ts: int | None = None,
) -> str:
    """落一条明细，返回访客哈希（调用方不回给客户端，只用于日志/测试）。

    `member_id` 只在**凭据校验通过**时由调用方给出（`api` 从 cookie 解出），
    因此它记的是「自愿留资或付费的访问者归属于可识别个体」这件事本身（FR-C7-6），
    而不是客户端自称的身份。
    """
    stamp = now_ms() if ts is None else int(ts)
    path = normalize_page(page)
    day, salt = current_salt(conn, at_ms=stamp)
    digest = visitor_hash(salt, str(ip or ""), str(user_agent or ""))
    conn.execute(
        "INSERT INTO stats_events(day, ts, page, visitor_hash, paid, member_id) VALUES(?,?,?,?,?,?)",
        (
            day,
            stamp,
            path,
            digest,
            1 if is_paid_page(conn, path) else 0,
            int(member_id) if member_id is not None else None,
        ),
    )
    conn.commit()
    return digest
