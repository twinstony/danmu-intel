"""T6 端到端：真调用点（假 LLM）→ 组装 → 发布检查 → 页面。

覆盖 issue #9 的验收标准：

1. 断网 / API 报错 / 超时三种情形下，报告**仍按时发布**且标注降级；
2. 含"新事实"的假输出被拦下 → 重试 → 仍失败则降级，且页面里不出现那些编造的数字/名字；
3. 达单场 ¥0.3 或当日 ¥10 后，下次调用前即降级（有报警）；
4. 解读段中每个数字都能在事实层找到来源（AC-16）；
5. 凭据零泄漏（AC-12）：页面、库与仓库里都没有密钥。

全程注入假 LLM，不连外网（NFR-GA-4）。
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from danmu_intel.common import paths
from danmu_intel.common.notifications import recent as recent_events
from danmu_intel.pipeline import collect_facts, generate_and_publish
from danmu_intel.report.forms import LLM_STATE_LLM, LLM_STATE_RULE, form_of
from danmu_intel.report.llm import alerts, ledger as llm_ledger
from danmu_intel.report.llm.client import LLMReply, LLMTimeout, LLMUnavailable
from danmu_intel.report.llm.cost import MATCH_LIMIT_CNY
from danmu_intel.report.llm.interpreter import LLMInterpreter
from danmu_intel.report.llm.prompts import load_prompt_set
from danmu_intel.report.llm.verify import verify_text
from danmu_intel.report.publish import load_content
from danmu_intel.report.rule_render import INTERPRETATION_MARK, interpretation_text
from danmu_intel.report.segments import INTERPRETATION_SEGMENTS, SPECS_BY_NO

GENERATED_AT = 1_790_064_400_000
FAKE_KEY = "sk-" + "e2e" * 12
OK_TEXT = "从弹幕看，G1 的讨论最集中（55 条），这只是注意力层面的观察。"


class StepClock:
    """每次调用前进固定秒数的假时钟（用来验证预算而不是浪费真实时间）。"""

    def __init__(self, step_s: float = 4.0) -> None:
        self.value = 0.0
        self.step_s = step_s

    def __call__(self) -> float:
        return self.value

    def tick(self) -> None:
        self.value += self.step_s


@dataclass
class FakeLLM:
    """假 LLM：`mode` 决定它怎么坏，`calls` 记录被调用了几次。"""

    mode: str = "ok"  # ok | timeout | offline | error | hallucination | protocol
    text: str = OK_TEXT
    clock: StepClock | None = None
    calls: int = 0
    model: str = "deepseek-v4-flash"
    scripts: list[str] = field(default_factory=list)

    def complete(self, *, system: str, user: str, timeout_s: float | None = None) -> LLMReply:
        self.calls += 1
        if self.clock is not None:
            self.clock.tick()
        spec_no = int(re.search(r"\*\*(\d+) 号段", user).group(1))  # type: ignore[union-attr]
        if self.mode == "timeout":
            raise LLMTimeout("LLM 调用超时（25 秒）")
        if self.mode == "offline":
            raise LLMUnavailable("LLM 接口不可达（URLError）")
        if self.mode == "error":
            raise LLMUnavailable("LLM 接口返回 HTTP 500")
        if self.mode == "protocol":
            return self._reply("这不是 JSON")
        if self.mode == "hallucination":
            return self._reply(
                json.dumps(
                    {"segments": {str(spec_no): f"第 {spec_no} 段：T1 以 3:0 取胜，Faker 稳定。"}},
                    ensure_ascii=False,
                )
            )
        text = self.scripts.pop(0) if self.scripts else self.text
        return self._reply(
            json.dumps({"segments": {str(spec_no): f"第 {spec_no} 段：{text}"}}, ensure_ascii=False)
        )

    def _reply(self, text: str) -> LLMReply:
        return LLMReply(
            text=text,
            model=self.model,
            prompt_tokens=4000,
            completion_tokens=400,
            cache_hit_tokens=3000,
            latency_ms=1200,
        )


def interpreter(conn, fake: FakeLLM, **kwargs) -> LLMInterpreter:
    return LLMInterpreter(conn=conn, client=fake, prompt_set=load_prompt_set(), **kwargs)


def publish(ledger_obj, fake: FakeLLM, **kwargs):
    return generate_and_publish(
        ledger_obj.conn,
        ledger_obj.match_id,
        kind="full",
        interpreter=interpreter(ledger_obj.conn, fake, **kwargs),
        data_root=ledger_obj.data_root,
        generated_at=GENERATED_AT,
    )


def page_text(site_root, match_id: int) -> str:
    return (site_root / "matches" / str(match_id) / "full.html").read_text(encoding="utf-8")


# —— 1. 断网 / 报错 / 超时三种情形 ——


@pytest.mark.parametrize("mode", ["offline", "error", "timeout"])
def test_failed_calls_still_publish_a_complete_marked_report(ledger, site_root, mode):
    fake = FakeLLM(mode=mode)
    result = publish(ledger, fake)

    assert result.content.llm_state == LLM_STATE_RULE
    assert "解读能力降级" in page_text(site_root, ledger.match_id)
    assert all(segment.body.strip() for segment in result.content.segments)
    assert all(item.passed for item in result.checks if item.blocking)

    # 页面里每一段解读都有正文（规则直出兜底），且都带「解读，非事实」标注
    content = load_content(ledger.conn, ledger.match_id, "full", 1)
    for no in INTERPRETATION_SEGMENTS:
        body = content.segment(no).body
        assert INTERPRETATION_MARK in body and body.strip()
    assert content.meta["llm_note"]

    row = ledger.conn.execute(
        "SELECT llm_state, state FROM reports WHERE match_id=? AND kind='full'", (ledger.match_id,)
    ).fetchone()
    assert row["llm_state"] == LLM_STATE_RULE and row["state"] == "published"


def test_timeouts_do_not_eat_the_deadline(ledger, site_root):
    """超时按失败处理 + 解读阶段预算：整份报告仍在形态时限内完成。"""
    clock = StepClock(step_s=4.0)
    result = publish(ledger, FakeLLM(mode="timeout", clock=clock), clock=clock)
    report = result.timing
    assert report["within_deadline"] is True
    assert report["deadline_ms"] == form_of("full").deadline_ms
    assert report["stages"]["interpretation"] <= 25_000
    assert report["over_budget_stages"] == []


# —— 2. 幻觉 ——


def test_hallucinated_output_is_refused_and_never_reaches_the_page(ledger, site_root):
    fake = FakeLLM(mode="hallucination")
    result = publish(ledger, fake)

    text = page_text(site_root, ledger.match_id)
    assert "Faker" not in text and "3:0" not in text
    assert result.content.llm_state == LLM_STATE_RULE
    # 每段"调用 + 重试 1 次"，连丢三次不合格即全局降级，剩下的段不再白花钱
    assert fake.calls == 4
    assert "含事实层之外的内容" in (result.content.meta["llm_note"] or "")
    assert "全局降级" in (result.content.meta["llm_note"] or "")

    rejected = [row for row in llm_ledger.calls_for_match(ledger.conn, ledger.match_id) if row.outcome == llm_ledger.OUTCOME_REJECTED]
    assert len(rejected) == 4
    assert all("含事实层之外的内容" in (row.reason or "") for row in rejected)


def test_a_hallucinating_call_costs_money_and_says_so(ledger, site_root):
    """被拒的调用照样花钱：账本要记，成本闸才不会形同虚设。"""
    publish(ledger, FakeLLM(mode="hallucination"))
    rejected = [row for row in llm_ledger.calls_for_match(ledger.conn, ledger.match_id) if row.outcome == llm_ledger.OUTCOME_REJECTED]
    assert rejected and all(row.cost_cny > 0 for row in rejected)


def test_llm_text_without_new_facts_is_published_and_marked_llm(ledger, site_root):
    fake = FakeLLM()
    result = publish(ledger, fake)

    assert result.content.llm_state == LLM_STATE_LLM
    text = page_text(site_root, ledger.match_id)
    assert "解读能力降级" not in text
    assert "从弹幕看，G1 的讨论最集中" in text
    assert llm_ledger.calls_for_match(ledger.conn, ledger.match_id)[0].outcome == llm_ledger.OUTCOME_OK


# —— 3. 成本硬闸 ——


def test_cost_gate_degrades_the_next_report_and_alerts(ledger, site_root):
    llm_ledger.record(
        ledger.conn,
        match_id=ledger.match_id,
        segment_no=3,
        model="deepseek-v4-flash",
        prompt_version="v1",
        outcome=llm_ledger.OUTCOME_OK,
        created_at=GENERATED_AT,
        cost_cny=MATCH_LIMIT_CNY,
    )
    fake = FakeLLM()
    result = publish(ledger, fake)

    assert fake.calls == 0  # 调用前就闸住了
    assert result.content.llm_state == LLM_STATE_RULE
    assert "单场成本已达" in str(result.content.meta["llm_note"])

    events = [item for item in recent_events(ledger.conn) if item.kind in alerts.KINDS]
    assert len(events) == 1 and events[0].kind == alerts.COST_GATE
    assert events[0].state == "pending"  # 待 T11 投递
    assert events[0].payload["match_id"] == ledger.match_id

    # 报告照样完整发布（降级不等于不发）
    assert all(segment.body.strip() for segment in result.content.segments)
    assert "解读能力降级" in page_text(site_root, ledger.match_id)


def test_gate_is_checked_before_every_call(ledger, site_root):
    """单场成本在报告中途越过硬闸 → 剩下的段不再调用。"""
    fake = FakeLLM()
    result = publish(ledger, fake, match_limit=0.001)  # 第一段就会超
    assert fake.calls == 1
    assert result.content.llm_state == LLM_STATE_RULE
    assert "单场成本已达" in str(result.content.meta["llm_note"])


# —— 4. AC-16：解读段里的数字都能溯源 ——


def test_every_number_in_the_interpretation_traces_back_to_the_fact_layer(ledger, site_root):
    """发布后的页面里，解读段的任何数字/名称都在事实层里出现（AC-16）。"""
    publish(ledger, FakeLLM())
    facts = collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    content = load_content(ledger.conn, ledger.match_id, "full", 1)

    for segment in content.interpretation_segments:
        assert INTERPRETATION_MARK in segment.body
        interpretation_part = segment.body.split(INTERPRETATION_MARK, 1)[1]
        assert verify_text(interpretation_part, facts) == (), (
            f"第 {segment.no} 段的解读引入了事实层之外的内容"
        )


def test_rule_fallback_output_also_traces_back(ledger, site_root):
    """降级路径同样要能溯源：规则直出也不能拿"反正不是 LLM"当借口。"""
    publish(ledger, FakeLLM(mode="offline"))
    facts = collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    content = load_content(ledger.conn, ledger.match_id, "full", 1)
    for segment in content.interpretation_segments:
        part = segment.body.split(INTERPRETATION_MARK, 1)[1]
        assert verify_text(part, facts) == ()
    assert all(not segment.body.endswith("…") for segment in content.interpretation_segments)


# —— 5. AC-12：凭据零泄漏 ——


def test_api_key_never_reaches_the_page_database_or_repo(ledger, site_root, data_root, monkeypatch):
    target = data_root / ".env"
    target.write_text(f"DEEPSEEK_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    target.chmod(0o600)

    from danmu_intel.report.llm.interpreter import interpreter_for

    real = interpreter_for(ledger.conn)
    assert isinstance(real, LLMInterpreter)
    assert real.client.api_key == FAKE_KEY

    # 用假客户端替掉真客户端后发布：密钥不该出现在任何产物里
    fake = FakeLLM()
    real.client = fake  # type: ignore[assignment]
    result = generate_and_publish(
        ledger.conn,
        ledger.match_id,
        kind="full",
        interpreter=real,
        data_root=ledger.data_root,
        generated_at=GENERATED_AT,
    )
    assert result.content.llm_state == LLM_STATE_LLM

    assert FAKE_KEY not in page_text(site_root, ledger.match_id)
    dumped = "\n".join(
        str(row)
        for table in ("reports", "llm_calls", "notifications")
        for row in ledger.conn.execute(f"SELECT * FROM {table}").fetchall()
    )
    assert FAKE_KEY not in dumped
    assert FAKE_KEY not in json.dumps(result.content.as_dict(), ensure_ascii=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


def test_credential_file_is_outside_the_repository(data_root):
    """凭据只允许在仓库外的数据目录里（ADR-0014 / NFR-S-4）。"""
    assert paths.env_path().parent == data_root
    assert paths.repo_root() not in paths.env_path().parents


# —— CLI：降级原因与解读层报警都要看得见 ——


def test_cli_report_without_credentials_degrades_and_says_why(ledger, site_root, capsys, monkeypatch):
    from danmu_intel.cli import main

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert main(["report", "--match-id", str(ledger.match_id), "--kind", "full"]) == 0
    out = capsys.readouterr().out

    assert "解读层 rule_fallback" in out
    assert "降级原因：未配置 DEEPSEEK_API_KEY" in out
    assert "解读能力降级" in page_text(site_root, ledger.match_id)


def test_cli_events_show_interpretation_alerts(ledger, capsys):
    from danmu_intel.cli import main

    alerts.alert(
        ledger.conn,
        alerts.COST_GATE,
        match_id=ledger.match_id,
        severity="warning",
        detail={"limit_kind": "match", "spent_match_cny": 0.31},
    )
    assert main(["events", "--match-id", str(ledger.match_id)]) == 0
    out = capsys.readouterr().out
    assert "llm_cost_gate（warning，pending）" in out
    assert "解读层" in out and "0.31" in out


# —— 真 HTTP 路径：本地桩服务器（不连外网，只连 127.0.0.1）——


class _StubDeepSeek:
    """本机桩：模拟 DeepSeek 的 `/chat/completions`，并把收到的请求记下来。"""

    def __init__(self, *, text: str = OK_TEXT) -> None:
        self.requests: list[dict[str, object]] = []
        self.text = text
        self._server = HTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> "_StubDeepSeek":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server 的约定名
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                stub.requests.append(
                    {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
                )
                spec_no = re.search(r"\*\*(\d+) 号段", body["messages"][1]["content"]).group(1)
                payload = json.dumps(
                    {"segments": {spec_no: f"第 {spec_no} 段：{stub.text}"}}, ensure_ascii=False
                )
                reply = json.dumps(
                    {
                        "model": body["model"],
                        "choices": [{"message": {"role": "assistant", "content": payload}}],
                        "usage": {
                            "prompt_tokens": 4200,
                            "completion_tokens": 380,
                            "prompt_cache_hit_tokens": 3000,
                        },
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args) -> None:  # 静音访问日志
                pass

        return Handler


def test_real_http_client_publishes_an_llm_report(ledger, site_root):
    """走真 `urllib` 传输层（本机桩）：请求形状、账本、页面三者对得上。"""
    from danmu_intel.report.llm.client import DeepSeekClient

    stub = _StubDeepSeek().start()
    try:
        client = DeepSeekClient(api_key=FAKE_KEY, base_url=stub.base_url, model="deepseek-v4-flash")
        interp = LLMInterpreter(conn=ledger.conn, client=client, prompt_set=load_prompt_set())
        result = generate_and_publish(
            ledger.conn,
            ledger.match_id,
            kind="full",
            interpreter=interp,
            data_root=ledger.data_root,
            generated_at=GENERATED_AT,
        )
    finally:
        stub.stop()

    assert result.content.llm_state == LLM_STATE_LLM
    assert interp.calls == len(INTERPRETATION_SEGMENTS) == len(stub.requests)
    first = stub.requests[0]
    assert first["path"] == "/chat/completions"
    assert first["auth"] == f"Bearer {FAKE_KEY}"
    assert first["body"]["response_format"] == {"type": "json_object"}
    assert first["body"]["stream"] is False

    rows = llm_ledger.calls_for_match(ledger.conn, ledger.match_id)
    assert {row.outcome for row in rows} == {llm_ledger.OUTCOME_OK}
    assert all(row.prompt_tokens == 4200 and row.cache_hit_tokens == 3000 for row in rows)
    assert interp.spent_cny > 0 and interp.spent_cny < MATCH_LIMIT_CNY
    assert "从弹幕看，G1 的讨论最集中" in page_text(site_root, ledger.match_id)
