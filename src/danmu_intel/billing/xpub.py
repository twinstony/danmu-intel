"""从 xpub 派生 watch-only 收款地址（ADR-0004；需求 FR-C6-4 / FR-C6-17..20）。

每位待付款用户拿一个**专属收款地址**（FR-C6-4）：按 BIP44 路径
`m/44'/60'/0'/0/i` 从运营者的 **xpub**（账户级扩展公钥，depth=3）做**非硬化**派生 `0/i`。
xpub 是 watch-only 公开信息：拿到它只能算出地址，**动不了钱**（FR-C6-17/18）。
派生索引只前进不回退：已展示给某人的地址永不分配给他人（防串单，设计 §12.2 ⑧）。

三条防线写在这里，而不是写在调用点：

1. **拒绝 xprv**：扩展**私钥**（xprv/yprv/zprv/tprv/uprv/vprv）一出现就报错 ——
   系统里不该有它，也不该有人把它传进来（AC-12 / FR-C6-19）。
2. **只做非硬化派生**：硬化派生需要私钥，系统没有也不会有，因此索引必须 < 2³¹。
3. **账户级 xpub 才准派生**：depth 不是 3（`m/44'/60'/0'`）就当配置错误拒绝 ——
   拿错一个层级的 xpub 会算出**别人的地址**，钱就收不到了。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from danmu_intel.billing import crypto

#: BIP32 扩展公钥 / 扩展私钥的版本字节。
XPUB_VERSION = 0x0488B21E
XPRV_VERSION = 0x0488ADE4

#: 私钥版本（一旦出现即拒绝）：主网与常见分层前缀都列上，宁严不宽。
PRIVATE_VERSIONS = {
    0x0488ADE4: "xprv",
    0x049D7878: "yprv",
    0x04B2430C: "zprv",
    0x04358394: "tprv",
    0x04292A21: "uprv",
    0x045F18BC: "vprv",
}
#: 其它扩展**公钥**版本：本系统只收 xpub（Ethereum 主网账户层），
#: 其余前缀认得出来是为了给出准确错误，而不是「也能用」。
OTHER_PUBLIC_VERSIONS = {
    0x049D7CB2: "ypub",
    0x04B24746: "zpub",
    0x043587CF: "tpub",
    0x04292AB0: "upub",
    0x045F1CF6: "vpub",
}

#: 账户级 xpub 的深度：`m/44'/60'/0'`（BIP44 的账户层）。
ACCOUNT_DEPTH = 3
#: BIP44 里换地址链（0 = 对外收款地址）。
CHANGE = 0
#: 派生路径（写进文档与错误消息，避免"我们派生的到底是哪条路径"这种问题）。
DERIVATION_PATH = "m/44'/60'/0'/0/i"
#: 非硬化索引上限：`1 << 31` 及以上是硬化派生（要私钥，系统没有）。
HARDENED = 1 << 31
MAX_INDEX = HARDENED - 1


class XpubError(ValueError):
    """xpub 不可用（格式、层级、索引或派生越界）。消息里只有公开信息。"""


def _fingerprint(public_key: bytes) -> bytes:
    digest = hashlib.new("ripemd160", hashlib.sha256(public_key).digest()).digest()
    return digest[:4]


@dataclass(frozen=True, slots=True)
class ExtendedPublicKey:
    """一个 BIP32 扩展公钥（CKDpub 派生的输入与结果同形）。"""

    version: int
    depth: int
    parent_fingerprint: bytes
    index: int
    chain_code: bytes
    public_key: bytes  # 33 字节压缩公钥

    def __post_init__(self) -> None:
        if len(self.chain_code) != 32:
            raise XpubError("链码必须是 32 字节")
        if len(self.parent_fingerprint) != 4:
            raise XpubError("父指纹必须是 4 字节")
        crypto.decompress(self.public_key)  # 不在曲线上的公钥立即报错

    @property
    def fingerprint(self) -> bytes:
        return _fingerprint(self.public_key)

    def child(self, index: int, *, hardened: bool = False) -> "ExtendedPublicKey":
        """按索引派生一个子公钥（CKDpub）。`hardened=True` 直接拒绝。"""
        if hardened or index >= HARDENED:
            raise XpubError(
                "硬化派生需要私钥，本系统只有 xpub（watch-only）——硬化子密钥派生不了"
            )
        if index < 0:
            raise XpubError(f"派生索引不得为负：{index}")
        digest = hmac.new(
            self.chain_code,
            self.public_key + index.to_bytes(4, "big"),
            hashlib.sha512,
        ).digest()
        left, right = digest[:32], digest[32:]
        tweak = int.from_bytes(left, "big")
        if tweak >= crypto.curve_order():
            raise XpubError("派生的标量越出曲线阶（概率 < 2⁻¹²⁷）：请换一个索引")
        parent = crypto.decompress(self.public_key)
        point = crypto.point_add(crypto.scalar_mult(tweak), parent)
        if point is None:
            raise XpubError("派生出了无穷远点（概率 < 2⁻¹²⁷）：请换一个索引")
        return ExtendedPublicKey(
            version=self.version,
            depth=self.depth + 1,
            parent_fingerprint=self.fingerprint,
            index=index,
            chain_code=right,
            public_key=crypto.compress(point),
        )

    def address(self) -> str:
        """Ethereum 地址（小写 `0x…`）：keccak256(未压缩公钥 64 字节) 的后 20 字节。"""
        digest = crypto.keccak256(crypto.uncompressed(crypto.decompress(self.public_key)))
        return "0x" + digest[-20:].hex()

    def checksum_address(self) -> str:
        return to_checksum_address(self.address())

    def serialize(self) -> str:
        """回写成 `xpub…` 文本（格式自检与排查用；不含任何私钥材料）。"""
        raw = (
            self.version.to_bytes(4, "big")
            + bytes([self.depth])
            + self.parent_fingerprint
            + self.index.to_bytes(4, "big")
            + self.chain_code
            + self.public_key
        )
        return crypto.b58encode(raw)


def parse_xpub(text: str) -> ExtendedPublicKey:
    """解析 `xpub…` 文本；扩展私钥（xprv 等）一律拒绝。"""
    candidate = (text or "").strip()
    if not candidate:
        raise XpubError("xpub 为空")
    try:
        raw = crypto.b58decode(candidate)
    except crypto.Base58Error as exc:
        raise XpubError(f"不是合法的 xpub 文本：{exc}") from exc
    if len(raw) != 78:
        raise XpubError(f"xpub 应为 78 字节，收到 {len(raw)} 字节")
    version = int.from_bytes(raw[:4], "big")
    if version in PRIVATE_VERSIONS:
        raise XpubError(
            f"拒绝 {PRIVATE_VERSIONS[version]}（扩展**私钥**）：系统永不持有可动用资产的凭据，"
            "请只提供 xpub（watch-only 公开信息）"
        )
    if version in OTHER_PUBLIC_VERSIONS:
        raise XpubError(
            f"只接受 xpub（主网账户级扩展公钥），收到 {OTHER_PUBLIC_VERSIONS[version]}"
        )
    if version != XPUB_VERSION:
        raise XpubError(f"未知的扩展密钥版本：{version:08x}")
    depth = raw[4]
    parent_fingerprint = raw[5:9]
    index = int.from_bytes(raw[9:13], "big")
    chain_code = raw[13:45]
    public_key = raw[45:78]
    if public_key[0] == 0:
        raise XpubError("扩展密钥里裹着私钥（首字节为 0）——拒绝")
    return ExtendedPublicKey(
        version=version,
        depth=depth,
        parent_fingerprint=parent_fingerprint,
        index=index,
        chain_code=chain_code,
        public_key=public_key,
    )


def account_key(xpub: str) -> ExtendedPublicKey:
    """取账户级扩展公钥；层级不对就拒绝（拿错层级会算出别人的地址）。"""
    key = parse_xpub(xpub)
    if key.depth != ACCOUNT_DEPTH:
        raise XpubError(
            f"只接受账户级 xpub（{DERIVATION_PATH.split('/i')[0]}，depth={ACCOUNT_DEPTH}），"
            f"收到 depth={key.depth}：层级不对会派生出别人的地址，钱就收不到了"
        )
    return key


def derive_address(xpub: str, index: int) -> str:
    """派生第 `index` 个专属收款地址（路径 `m/44'/60'/0'/0/index`，小写 `0x…`）。"""
    if index < 0 or index > MAX_INDEX:
        raise XpubError(f"派生索引应在 0..{MAX_INDEX}，收到 {index}")
    branch = account_key(xpub).child(CHANGE)
    return branch.child(index).address()


def derive_checksum_address(xpub: str, index: int) -> str:
    return to_checksum_address(derive_address(xpub, index))


def to_checksum_address(address: str) -> str:
    """EIP-55 校验和地址（只影响显示大小写；比较一律用小写）。"""
    plain = address.lower()
    if len(plain) != 42 or not plain.startswith("0x"):
        raise XpubError(f"不是合法的以太坊地址：{address}")
    try:
        int(plain[2:], 16)
    except ValueError as exc:
        raise XpubError(f"不是合法的以太坊地址：{address}") from exc
    digest = crypto.keccak256(plain[2:].encode("ascii")).hex()
    return "0x" + "".join(
        char.upper() if char.isalpha() and int(digest[i], 16) >= 8 else char
        for i, char in enumerate(plain[2:])
    )
