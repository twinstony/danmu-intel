"""xpub 派生（T9）的公开测试向量：keccak、secp256k1、base58check、BIP32 CKDpub、ETH 地址。

**向量全部来自本文件之外**，不是「用自己的实现证明自己」：

| 向量 | 出处 |
|---|---|
| keccak-256 空串 / `abc` | 广泛公开的 Keccak-256 常量（Ethereum 用的不是 SHA3-256） |
| keccak-256 长输入（135/136/200 字节） | 参考实现（pycryptodome 的 `keccak`）现算后写死，用来钉住填充与多块吸收 |
| secp256k1 标量 1/2/3 的公钥点 | 曲线基点与倍点（k=1 即 G，公开已知） |
| BIP32 官方向量 1（种子 `000102…0f`）的主 xpub | BIP-0032 规范附录的 test vector 1 |
| 账户层 xpub → `0/i` 地址链 | 参考实现（`bip32utils` CKDpub + `eth_keys`）独立算出后写死 |
| EIP-55 校验和地址 | EIP-55 原文的四条测试向量 |

xprv（扩展**私钥**）在测试里一律**拼接构造**：本文件自己也要能通过
`tools/check_no_secrets.py`（防线开了洞就不算防线）。
"""

from __future__ import annotations

import pytest

from danmu_intel.billing import crypto, xpub
from danmu_intel.billing.crypto import Base58Error, CurveError
from danmu_intel.billing.xpub import XpubError

# BIP-0032 test vector 1 的种子对应的**主**扩展公钥（规范原文的值）。
VECTOR1_MASTER_XPUB = (
    "xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gZ29ESFjqJoCu1Rupje8Y"
    "tGqsefD265TMg7usUDFdp6W1EGMcet8"
)
VECTOR1_MASTER_CHAIN_CODE = "873dff81c02f525623fd1fe5167eac3a55a049de3d314bb42ee227ffed37d508"
VECTOR1_MASTER_PUBLIC_KEY = "0339a36013301597daef41fbe593a02cc513d0b55527ec2df1050e2e8ff49c85c2"

# 同一个种子在 `m/44'/60'/0'`（账户层，depth=3）的 xpub —— 参考实现算出，
# 下面的地址链与 BIP32 官方向量在同一棵树上。
ACCOUNT_XPUB = (
    "xpub6CeDpm2b5qtk96oy8yvM572W6cLZSvU5vnpKmKPypbfFwXo86SyT7VtfwWtMZAgZ5eKVMU9NnUL"
    "t91HBFw9j62wJrcoc1ZRWiNvoorwBRXL"
)
BRANCH_XPUB = (
    "xpub6DZ3xpo1ixWwwNDQ7KFTamRVM46FQtgcDxsmAyeBpTHEo79E1n1LuWiZSMSRhqMQmrHaqJpek2Tb"
    "tTzbAdNWJm9AhGdv7iJUpDjA6oJD84b"
)
BRANCH_FINGERPRINT = "60a150d5"

ADDRESSES = (
    "0x022b971dff0c43305e691ded7a14367af19d6407",
    "0xbb7a182240010703dc81d6b1eff630ca02a169fd",
    "0xecf722a6a8ee18f5a9d3c00d168be3d0d068732b",
    "0x23fcfba6579abdcf799c65fe87e7b2668eb78ed8",
)
CHECKSUM_ADDRESSES = (
    "0x022b971dFF0C43305e691DEd7a14367AF19D6407",
    "0xbb7A182240010703dc81D6b1EFf630CA02a169FD",
    "0xECf722a6a8EE18F5A9D3C00D168be3D0d068732b",
    "0x23FcfBa6579ABdCf799c65fE87e7b2668Eb78Ed8",
)
CHAIN_CODES = (
    "dac0c414d5006b7350e3b7750e5b535af7ecd9b5a2ad00648d427349885f4358",
    "dc380fe4c131d35f48a7ba188d7435cdc8c42612ecf41ffe0a5503ff28d2bfc6",
    "71fca1457d97e6cae3db558ce00b7a2b0794bd642d58036ea258c425537f1af2",
    "e9a1b014b6e23dd6d3cbaa69396cc4a007c171a09246fd6fd233c0aeb461b503",
)
PUBLIC_KEYS = (
    "03844a5d329470697de9926c9c98839ea33b6dd9507a896194ae2b91d71faa16d6",
    "03170acbbcb89dd0e364ce51c96770fa24d7be16e486183c390f9d0bcb520df8e6",
    "03d992010dcf66879a1c02838d3fba382592f11761b15b92df44540bc4c9cb2352",
    "0328d902242ef19a6a7c7b70bcc1909a412e3278a11283e6c822a36e993d12f630",
)

def fake_extended_private_key() -> str:
    """构造一个**格式合法**的扩展私钥文本（内容不是任何真实钱包的私钥）。

    它只用来验证「xprv 传进来必被拒绝」；构造而不写死，本文件才扫得过
    `tools/check_no_secrets.py`（防线开了洞就不算防线）。
    """
    private_material = bytes(range(1, 33))
    raw = (
        bytes.fromhex("0488ade4")  # 扩展私钥版本
        + b"\x00"  # depth
        + b"\x00" * 4  # 父指纹
        + b"\x00" * 4  # 索引
        + bytes.fromhex(VECTOR1_MASTER_CHAIN_CODE)
        + b"\x00"  # 私钥材料的首字节标记
        + private_material
    )
    return crypto.b58encode(raw)


def test_keccak256_matches_published_constants():
    assert crypto.keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert crypto.keccak256(b"abc").hex() == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"


@pytest.mark.parametrize(
    "length, expected",
    [
        (135, "cbdfd9dee5faad3818d6b06f95a219fd290b0e1706f6a82e5a595b9ce9faca62"),
        (136, "7ce759f1ab7f9ce437719970c26b0a66ff11fe3e38e17df89cf5d29c7d7f807e"),
    ],
)
def test_keccak256_padding_boundaries(length, expected):
    """速率边界两侧（135/136 字节）各自与参考实现一致 —— 钉住 Keccak 的填充（`0x01`）。"""
    assert crypto.keccak256(bytes(range(length))).hex() == expected


def test_keccak256_handles_multiple_blocks_and_unicode():
    assert crypto.keccak256(b"a" * 200).hex() == (
        "96ea54061def936c4be90b518992fdc6f12f535068a256229aca54267b4d084d"
    )
    assert crypto.keccak256("弹幕情报库".encode()).hex() == (
        "9b288ea3c0cf81b9c524742b5b97cf794b0a7662025e6fdab4442b7b22037cf6"
    )


@pytest.mark.parametrize(
    "scalar, compressed",
    [
        (1, "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"),
        (2, "02c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5"),
        (3, "02f9308a019258c31049344f85f89d5229b531c845836f99b08601f113bce036f9"),
        (0xDEADBEEF, "0276d2fdf1302d1fa9556f4df94ec84cefba6d482e54f47c6c2a238c1baa560f0e"),
        (12345678901234567890, "0299c126da20397558f23658764c3a7c583db7ff706e93981cc170e27ca8336201"),
    ],
)
def test_secp256k1_scalar_mult_matches_reference_points(scalar, compressed):
    point = crypto.scalar_mult(scalar)
    assert crypto.compress(point).hex() == compressed
    assert crypto.decompress(bytes.fromhex(compressed)) == point  # 压缩/解压互逆


def test_secp256k1_edge_cases():
    assert crypto.scalar_mult(0) is None
    assert crypto.scalar_mult(crypto.curve_order()) is None
    assert crypto.point_add(None, None) is None
    generator = crypto.scalar_mult(1)
    assert crypto.point_add(generator, None) == generator
    assert crypto.point_add(generator, generator) == crypto.scalar_mult(2)  # 倍点路径
    assert crypto.point_add(generator, crypto.negate(generator)) is None  # 互为逆元
    assert crypto.on_curve(generator)

    with pytest.raises(CurveError, match="33 字节"):
        crypto.decompress(b"\x02" * 32)
    with pytest.raises(CurveError, match="02/03"):
        crypto.decompress(b"\x04" + b"\x01" * 32)
    with pytest.raises(CurveError, match="x 超出域范围"):
        crypto.decompress(b"\x02" + b"\xff" * 32)
    # x 在域内但不在曲线上（x=0 时 y²=7 不是二次剩余）
    with pytest.raises(CurveError, match="不在 secp256k1 曲线上"):
        crypto.decompress(b"\x02" + (0).to_bytes(32, "big"))


def test_base58check_roundtrip_and_guards():
    payload = bytes(range(40))
    assert crypto.b58decode(crypto.b58encode(payload)) == payload
    assert crypto.b58decode(crypto.b58encode(b"\x00\x00\x01")) == b"\x00\x00\x01"  # 前导零不丢
    with pytest.raises(Base58Error, match="非法字符"):
        crypto.b58decode("xpub0OIl")
    with pytest.raises(Base58Error, match="过短"):
        crypto.b58decode("abc")
    bad = VECTOR1_MASTER_XPUB[:-1] + ("A" if VECTOR1_MASTER_XPUB[-1] != "A" else "B")
    with pytest.raises((Base58Error, XpubError)):
        xpub.parse_xpub(bad)


def test_bip32_vector1_master_xpub_parses_as_published():
    key = xpub.parse_xpub(VECTOR1_MASTER_XPUB)
    assert key.depth == 0
    assert key.index == 0
    assert key.chain_code.hex() == VECTOR1_MASTER_CHAIN_CODE
    assert key.public_key.hex() == VECTOR1_MASTER_PUBLIC_KEY
    assert key.fingerprint.hex() == "3442193e"
    assert key.serialize() == VECTOR1_MASTER_XPUB  # 编码/解码互逆


def test_ckdpub_walks_the_reference_chain_step_by_step():
    """账户层 xpub → `0` → `i`：链码、父指纹、公钥逐项与参考实现一致（BIP32 CKDpub）。"""
    account = xpub.parse_xpub(ACCOUNT_XPUB)
    assert account.depth == 3
    branch = account.child(0)
    assert branch.serialize() == BRANCH_XPUB  # 派生出的子公钥可回写成 xpub 文本
    assert branch.fingerprint.hex() == BRANCH_FINGERPRINT
    assert branch.parent_fingerprint.hex() == account.fingerprint.hex()

    for i, (chain_code, public_key, address) in enumerate(zip(CHAIN_CODES, PUBLIC_KEYS, ADDRESSES)):
        child = branch.child(i)
        assert child.chain_code.hex() == chain_code
        assert child.public_key.hex() == public_key
        assert child.parent_fingerprint.hex() == BRANCH_FINGERPRINT
        assert child.depth == 5 and child.index == i
        assert child.address() == address


def test_derive_address_uses_bip44_path_and_checksum_display():
    for i, (plain, checksum) in enumerate(zip(ADDRESSES, CHECKSUM_ADDRESSES)):
        assert xpub.derive_address(ACCOUNT_XPUB, i) == plain
        assert xpub.derive_checksum_address(ACCOUNT_XPUB, i) == checksum
    assert xpub.DERIVATION_PATH == "m/44'/60'/0'/0/i"


def test_extension_keys_with_private_material_are_refused():
    """AC-12 / FR-C6-19：系统永不持有可动用资产的凭据 —— 连传进来都不行。"""
    with pytest.raises(XpubError, match="拒绝 xprv"):
        xpub.parse_xpub(fake_extended_private_key())
    with pytest.raises(XpubError, match="硬化派生需要私钥"):
        xpub.parse_xpub(ACCOUNT_XPUB).child(0, hardened=True)
    with pytest.raises(XpubError, match="硬化派生需要私钥"):
        xpub.parse_xpub(ACCOUNT_XPUB).child(2**31)


@pytest.mark.parametrize(
    "text, match",
    [
        ("", "为空"),
        ("not-a-key", "不是合法的 xpub"),
        ("xpub" + "1" * 78, "校验和"),
        (VECTOR1_MASTER_XPUB, "depth=3"),  # 主 xpub：层级不对
    ],
)
def test_bad_xpub_texts_are_refused(text, match):
    with pytest.raises(XpubError, match=match):
        xpub.derive_address(text, 0)


def test_other_extended_key_prefixes_get_a_precise_error():
    raw = crypto.b58decode(VECTOR1_MASTER_XPUB)
    for version, label in ((0x049D7CB2, "ypub"), (0x04B24746, "zpub"), (0x043587CF, "tpub")):
        shifted = crypto.b58encode(version.to_bytes(4, "big") + raw[4:])
        with pytest.raises(XpubError, match=label):
            xpub.parse_xpub(shifted)
    unknown = crypto.b58encode(b"\x01\x02\x03\x04" + raw[4:])
    with pytest.raises(XpubError, match="未知的扩展密钥版本"):
        xpub.parse_xpub(unknown)


def test_index_bounds_are_enforced():
    with pytest.raises(XpubError, match="索引应在"):
        xpub.derive_address(ACCOUNT_XPUB, -1)
    with pytest.raises(XpubError, match="索引应在"):
        xpub.derive_address(ACCOUNT_XPUB, xpub.MAX_INDEX + 1)
    with pytest.raises(XpubError, match="索引不得为负"):
        xpub.parse_xpub(ACCOUNT_XPUB).child(-1)


def test_checksum_address_follows_eip55_and_refuses_garbage():
    for vector in (
        "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
        "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
        "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
        "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
    ):
        assert xpub.to_checksum_address(vector) == vector
    with pytest.raises(XpubError, match="不是合法的以太坊地址"):
        xpub.to_checksum_address("0x1234")
    with pytest.raises(XpubError, match="不是合法的以太坊地址"):
        xpub.to_checksum_address("0x" + "z" * 40)
