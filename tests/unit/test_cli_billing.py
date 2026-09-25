"""命令行：T9 的几条命令（billing / subscribe / orders / members / grant / chain-watch --orders）。"""

from __future__ import annotations

import json

from danmu_intel.billing import members, orders, pricing, settle
from danmu_intel.chain.quota import POLYGONSCAN, QuotaLedger
from danmu_intel.chain.transfer import Transfer
from danmu_intel.cli import main
from danmu_intel.common import audit
from tests.unit.test_billing_xpub import ACCOUNT_XPUB
from tests.unit.test_cli import FakeChainClient, _chain_client

WALLET = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
DAY = 24 * 3600 * 1000


def configure(capsys) -> None:
    assert (
        main(
            [
                "billing",
                "--set",
                f"polygon_xpub={ACCOUNT_XPUB}",
                "--set",
                f"solana_address={WALLET}",
                "--actor",
                "管理员",
            ]
        )
        == 0
    )
    capsys.readouterr()


def test_billing_shows_and_updates_the_receiving_config(conn, capsys):
    assert main(["billing"]) == 0
    out = capsys.readouterr().out
    assert "档位 standard（标准档）：5.00 USDT / 30 天" in out
    assert "档位 trial（试用档）：0.50 USDT / 3 天" in out
    assert "Polygon xpub（watch-only）：未配置" in out

    assert main(["billing", "--set", "grace_ms=3600000", "--set",
                 'tiers=[{"key":"standard","label":"标准档","amount_units":8000000,"days":30}]']) == 0
    out = capsys.readouterr().out
    assert "已更新收款配置（操作者 管理员）" in out
    assert "档位 standard（标准档）：8.00 USDT / 30 天" in out
    assert "宽限期 1 小时" in out
    config = pricing.load_billing_config(conn)
    assert [tier.key for tier in config.tiers] == ["standard"]
    assert audit.entries(conn, action=audit.CONFIG_UPDATE, target_prefix="billing")

    assert main(["billing", "--set", "vip_price"]) == 2
    assert "key=value" in capsys.readouterr().err
    assert main(["billing", "--set", "vip_price=1"]) == 2
    assert "未知的收款配置项" in capsys.readouterr().err


def test_subscribe_prints_the_payment_request(conn, capsys):
    configure(capsys)
    assert main(["subscribe", "--platform", "telegram", "--username", "@payer",
                 "--tier", "standard", "--network", "polygon"]) == 0
    out = capsys.readouterr().out
    assert "应付 5.000001 USDT（已含唯一尾数，请精确转账）" in out
    assert "收款地址 0x022b971dff0c43305e691ded7a14367af19d6407" in out
    assert "领取令牌 " in out and "只显示这一次" in out
    assert "付款到账会自动开通（无需人工）" in out
    [order] = orders.list_orders(conn)
    assert order.status == "pending" and order.memo is None

    # 复用未付订单：不生成新地址，令牌轮换
    assert main(["subscribe", "--platform", "telegram", "--username", "@payer",
                 "--tier", "standard", "--network", "polygon", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["public_ref"] == order.public_ref
    assert payload["claim_token"]
    assert len(orders.list_orders(conn)) == 1


def test_subscribe_needs_receiving_config_and_valid_contact(conn, capsys):
    assert main(["subscribe", "--platform", "telegram", "--username", "@payer",
                 "--tier", "standard", "--network", "polygon"]) == 2
    assert "xpub" in capsys.readouterr().err
    configure(capsys)
    assert main(["subscribe", "--platform", "qq", "--username", "@nope",
                 "--tier", "standard", "--network", "polygon"]) == 2
    assert "QQ 号" in capsys.readouterr().err
    assert main(["subscribe", "--platform", "qq", "--username", "12345678",
                 "--tier", "vip", "--network", "polygon"]) == 2
    assert "未知的档位" in capsys.readouterr().err


def test_orders_command_lists_and_filters(conn, capsys):
    configure(capsys)
    assert main(["orders"]) == 0
    assert "没有订单" in capsys.readouterr().out

    main(["subscribe", "--platform", "telegram", "--username", "@payer",
          "--tier", "standard", "--network", "polygon"])
    capsys.readouterr()
    paid_order, _ = orders.create_order(
        conn, platform="qq", username="12345678", tier="standard", network="solana"
    )
    assert main(["orders", "--status", "short"]) == 0
    assert "没有订单" in capsys.readouterr().out
    assert main(["orders", "--network", "solana"]) == 0
    out = capsys.readouterr().out
    assert paid_order.public_ref in out and "memo" in out and "12345678（qq）" in out
    assert main(["orders", "--status", "cancelled"]) == 2
    assert "未知的订单状态" in capsys.readouterr().err


def test_members_command_lists_and_sweeps(conn, capsys):
    configure(capsys)
    assert main(["members"]) == 0
    assert "没有会员" in capsys.readouterr().out

    order, _ = orders.create_order(
        conn, platform="telegram", username="@payer", tier="standard", network="polygon"
    )
    # 直接开通到「30 天前」的时刻：宽限期已过 → sweep 应把它降级
    expired_now = members.now_ms() - 40 * DAY
    members.grant(
        conn, member_id=order.member_id, tier="standard", tx_ref="0xold", now=expired_now
    )
    assert main(["members", "--status", "active"]) == 0
    out = capsys.readouterr().out
    assert "@payer（telegram）" in out and "active" in out

    assert main(["members", "--sweep"]) == 0
    out = capsys.readouterr().out
    assert "到期降级：会员 #1 → expired" in out
    assert "expired" in out
    assert main(["members", "--status", "vip"]) == 2
    assert "未知的会员状态" in capsys.readouterr().err


def test_grant_command_walks_the_manual_path(conn, capsys):
    configure(capsys)
    order, _ = orders.create_order(
        conn, platform="qq", username="12345678", tier="standard", network="polygon"
    )
    assert main(["grant", "--order-ref", order.public_ref, "--tx-ref", "0xmanual",
                 "--reason", "用户提供了区块浏览器链接"]) == 0
    out = capsys.readouterr().out
    assert "已人工补开通" in out and "操作者 管理员" in out
    assert members.get_member(conn, order.member_id).status == "active"

    assert main(["grant", "--order-ref", order.public_ref, "--tx-ref", "0xmanual",
                 "--reason", "重试"]) == 0
    assert "已是 paid（幂等" in capsys.readouterr().out

    short_order, _ = orders.create_order(
        conn, platform="qq", username="87654321", tier="standard", network="polygon"
    )
    assert main(["grant", "--order-ref", short_order.public_ref, "--tx-ref", "0xsmall",
                 "--units", "1", "--reason", "先补一小笔"]) == 1
    assert "仍差" in capsys.readouterr().out
    assert members.get_member(conn, short_order.member_id).status == "pending"


def test_chain_watch_orders_derives_targets_and_settles(conn, monkeypatch, capsys):
    configure(capsys)
    # 没有待付款订单时，--orders 明说而不是静默扫空
    assert main(["chain-watch", "--orders", "--once"]) == 2
    assert "当前没有待付款的订单" in capsys.readouterr().err

    order, _ = orders.create_order(
        conn, platform="telegram", username="@payer", tier="standard", network="polygon"
    )
    transfer = Transfer(
        network="polygon",
        address=order.address,
        tx_ref="0xorders",
        asset=order.asset,
        units=order.amount_due_units,
        at_ms=members.now_ms(),
        block=100,
    )
    _chain_client(
        monkeypatch, "polygonscan", FakeChainClient(QuotaLedger(conn, POLYGONSCAN), transfers=[transfer])
    )
    assert main(["chain-watch", "--orders", "--once"]) == 0
    out = capsys.readouterr().out
    assert "开始监听" not in out  # --once 只跑一轮
    assert f"入账｜polygon/{order.address}" in out
    assert f"对账｜{order.public_ref}｜已记账" in out
    assert "已开通会员 #1" in out
    assert orders.get_order(conn, order_id=order.id).status == "paid"
    assert members.get_member(conn, order.member_id).status == "active"


def test_chain_watch_orders_reports_grant_failures(conn, monkeypatch, capsys):
    configure(capsys)
    order, _ = orders.create_order(
        conn, platform="telegram", username="@payer", tier="standard", network="polygon"
    )
    transfer = Transfer(
        network="polygon", address=order.address, tx_ref="0xfail", asset=order.asset,
        units=order.amount_due_units, at_ms=members.now_ms(), block=100,
    )
    _chain_client(
        monkeypatch, "polygonscan", FakeChainClient(QuotaLedger(conn, POLYGONSCAN), transfers=[transfer])
    )

    def boom(*args, **kwargs):
        raise RuntimeError("会员表写不进去")

    monkeypatch.setattr(settle.members, "grant", boom)
    assert main(["chain-watch", "--orders", "--once"]) == 1
    captured = capsys.readouterr()
    assert "开通失败｜" in captured.err


def test_serve_command_starts_the_api(conn, monkeypatch, capsys):
    from danmu_intel.billing import api

    seen: dict[str, object] = {}
    monkeypatch.setattr(api, "run", lambda conn, *, host, port: seen.update(host=host, port=port))
    assert main(["serve", "--host", "127.0.0.1", "--port", "9000"]) == 0
    out = capsys.readouterr().out
    assert "收款 API 监听 127.0.0.1:9000" in out
    assert seen == {"host": "127.0.0.1", "port": 9000}
