"""供应商接口的公共错误词汇（两个客户端共用）。

消息里**只有**状态码与供应商给的短说明：绝不回显 API key、也绝不回显带 `apikey`
参数的完整 URL（一句日志把凭据漏出去，比漏检一次严重得多）。
"""

from __future__ import annotations


class ProviderError(RuntimeError):
    """链上数据供应商调用失败（断网 / 非 2xx / 响应不是我们认识的形状）。"""


class RateLimited(ProviderError):
    """供应商明确回了限速（HTTP 429 或 "Max rate limit reached"）。

    单独一个类型是因为处理方式不同：限速要**报警**（FR-C6-11），而不是悄悄重试了事——
    "这段时间看不见链上"本身就是必须让人知道的事实。
    """
