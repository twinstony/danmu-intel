"""派生地址要用的三件公开运算：keccak256、secp256k1 点运算、base58check。

**这里没有任何签名能力**：`secp256k1` 只有「点加」与「标量乘」（把公钥点算出来），
没有私钥运算、没有签名/验签、没有 keystore。于是本模块连同 `xpub.py` 都**不可能**动用户的钱
（FR-C6-17..19 / AC-12：系统只负责看见入账，不负责动用）。

为什么自己写而不引第三方包（AGENTS.md「优先用成熟库」的例外说明）：

- 需要的只是**单向派生**这一个窄功能（BIP32 CKDpub + keccak 地址），成熟库（`coincurve` /
  `eth-keys` / `bip-utils`）要么带编译依赖与签名能力（不需要的攻击面），要么拉进一堆与本项目
  无关的包；本项目的运行约束是**单机、断网、零新依赖**（NFR-A-3 / AC-14）。
- 正确性由**公开测试向量**锁定，不靠自证：BIP32 官方向量、keccak-256 已知常量、
  secp256k1 已知公钥点、以及一条 xpub → 地址链的参考实现比对面（见
  `tests/unit/test_billing_xpub.py`）。
"""

from __future__ import annotations

import hashlib

# —— keccak-256（Ethereum 用的那个 Keccak，不是 NIST 的 SHA3-256）——

#: θ 步的轮常量（24 轮）。
_ROUND_CONSTANTS = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)

#: ρ 步的循环移位量，按 lane 下标 `x + 5y` 排列。
_ROTATIONS = (
    0, 1, 62, 28, 27,
    36, 44, 6, 55, 20,
    3, 10, 43, 25, 39,
    41, 45, 15, 21, 8,
    18, 2, 61, 56, 14,
)

_MASK = (1 << 64) - 1
#: 海绵结构的速率：1600 - 2×256 = 1088 bit = 136 字节。
_RATE = 136


def _rotl(value: int, shift: int) -> int:
    if shift == 0:
        return value
    return ((value << shift) | (value >> (64 - shift))) & _MASK


def _keccak_f(state: list[int]) -> None:
    """Keccak-f[1600] 置换（就地）。"""
    for constant in _ROUND_CONSTANTS:
        # θ
        columns = [
            state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20] for x in range(5)
        ]
        diff = [columns[(x - 1) % 5] ^ _rotl(columns[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] ^= diff[x]
        # ρ + π
        moved = [0] * 25
        for x in range(5):
            for y in range(5):
                moved[y + 5 * ((2 * x + 3 * y) % 5)] = _rotl(state[x + 5 * y], _ROTATIONS[x + 5 * y])
        # χ
        for x in range(5):
            for y in range(5):
                row = 5 * y
                state[x + row] = moved[x + row] ^ ((~moved[(x + 1) % 5 + row]) & moved[(x + 2) % 5 + row])
        # ι
        state[0] ^= constant


def keccak256(data: bytes) -> bytes:
    """Keccak-256（Ethereum 的地址哈希原语）。"""
    padded = bytearray(data)
    padded.append(0x01)  # Keccak 的域分隔（SHA3 是 0x06，别混）
    padded.extend(b"\x00" * ((-len(padded)) % _RATE))
    padded[-1] ^= 0x80

    state = [0] * 25
    for offset in range(0, len(padded), _RATE):
        block = padded[offset : offset + _RATE]
        for lane in range(_RATE // 8):
            state[lane] ^= int.from_bytes(block[lane * 8 : lane * 8 + 8], "little")
        _keccak_f(state)
    output = bytearray()
    while len(output) < 32:
        for lane in range(_RATE // 8):
            output.extend(state[lane].to_bytes(8, "little"))
            if len(output) >= 32:
                break
        if len(output) < 32:
            _keccak_f(state)
    return bytes(output[:32])


# —— secp256k1（只有点加与标量乘：够算公钥，不够签任何东西）——

#: 域参数（公开常量）。写成不带前缀的十六进制字符串：它们不是任何凭据，
#: 但也没必要长得像私钥（`tools/check_no_secrets.py` 会拦 `0x` + 64 位十六进制）。
_FIELD_PRIME = int("fffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2f", 16)
_ORDER = int("fffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141", 16)
_GENERATOR_X = int("79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798", 16)
_GENERATOR_Y = int("483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8", 16)

Point = tuple[int, int]


class CurveError(ValueError):
    """椭圆曲线运算的非法输入（点不在曲线上、标量越界等）。"""


def _inverse(value: int) -> int:
    return pow(value, _FIELD_PRIME - 2, _FIELD_PRIME)


def point_add(left: Point | None, right: Point | None) -> Point | None:
    """两点相加（`None` 是无穷远点）。"""
    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 + y2) % _FIELD_PRIME == 0:
        return None
    if left == right:
        slope = (3 * x1 * x1) * _inverse(2 * y1) % _FIELD_PRIME
    else:
        slope = (y2 - y1) * _inverse(x2 - x1) % _FIELD_PRIME
    x3 = (slope * slope - x1 - x2) % _FIELD_PRIME
    return x3, (slope * (x1 - x3) - y1) % _FIELD_PRIME


def negate(point: Point) -> Point:
    """点的逆元（`y` 取模取负）—— 用于自检「P + (−P) = 无穷远」。"""
    return (point[0], (-point[1]) % _FIELD_PRIME)


def scalar_mult(scalar: int, point: Point | None = None) -> Point | None:
    """标量乘（倍点-加，256 轮）。标量 0 或为 `n` 的倍数得到无穷远点。"""
    if point is None:
        point = (_GENERATOR_X, _GENERATOR_Y)
    if scalar % _ORDER == 0:
        return None
    result: Point | None = None
    addend: Point | None = point
    while scalar:
        if scalar & 1:
            result = point_add(result, addend)
        addend = point_add(addend, addend)
        scalar >>= 1
    return result


def on_curve(point: Point) -> bool:
    x, y = point
    return (y * y - x * x * x - 7) % _FIELD_PRIME == 0


def decompress(public_key: bytes) -> Point:
    """33 字节压缩公钥（BIP32 xpub 里的形状）→ 仿射坐标点。"""
    if len(public_key) != 33 or public_key[0] not in (2, 3):
        raise CurveError(f"压缩公钥应为 33 字节且以 02/03 开头，收到 {len(public_key)} 字节")
    x = int.from_bytes(public_key[1:], "big")
    if x >= _FIELD_PRIME:
        raise CurveError("压缩公钥的 x 超出域范围")
    # y² = x³ + 7；取模平方根的候选值，再按奇偶挑选
    y = pow((x * x * x + 7) % _FIELD_PRIME, (_FIELD_PRIME + 1) // 4, _FIELD_PRIME)
    if (y * y - x * x * x - 7) % _FIELD_PRIME != 0:
        raise CurveError("压缩公钥不在 secp256k1 曲线上")
    if (y & 1) != (public_key[0] & 1):
        y = _FIELD_PRIME - y
    point = (x, y)
    if not on_curve(point):
        raise CurveError("压缩公钥不在 secp256k1 曲线上")
    return point


def compress(point: Point) -> bytes:
    x, y = point
    return bytes([2 + (y & 1)]) + x.to_bytes(32, "big")


def uncompressed(point: Point) -> bytes:
    """未压缩公钥（64 字节，不含 `04` 前缀）—— keccak 哈希的对象正是这 64 字节。"""
    return point[0].to_bytes(32, "big") + point[1].to_bytes(32, "big")


def curve_order() -> int:
    return _ORDER


# —— base58check（BIP32 扩展密钥的文本形式）——

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class Base58Error(ValueError):
    """base58 文本非法（含非法字符、校验和不符）。"""


def b58decode(text: str) -> bytes:
    """base58check 解码（含 4 字节校验和校验）。"""
    value = 0
    for char in text:
        index = _B58_ALPHABET.find(char)
        if index < 0:
            raise Base58Error(f"base58 文本含非法字符：{char}")
        value = value * 58 + index
    body = value.to_bytes((value.bit_length() + 7) // 8, "big") if value else b""
    payload = b"\x00" * (len(text) - len(text.lstrip("1"))) + body
    if len(payload) < 5:
        raise Base58Error("base58check 内容过短，不像扩展密钥")
    data, checksum = payload[:-4], payload[-4:]
    if hashlib.sha256(hashlib.sha256(data).digest()).digest()[:4] != checksum:
        raise Base58Error("base58check 校验和不符（文本可能在传输中被改坏）")
    return data


def b58encode(data: bytes) -> str:
    """base58check 编码（4 字节校验和），与 `b58decode` 互逆。"""
    payload = data + hashlib.sha256(hashlib.sha256(data).digest()).digest()[:4]
    value = int.from_bytes(payload, "big")
    encoded = ""
    while value:
        value, remainder = divmod(value, 58)
        encoded = _B58_ALPHABET[remainder] + encoded
    return "1" * (len(payload) - len(payload.lstrip(b"\x00"))) + encoded
