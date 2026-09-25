#!/usr/bin/env python3
"""凭据扫描（设计 §14.4，AC-12 / NFR-S-1 / NFR-S-4）。

按模式扫描全库：私钥块、`0x`+64 位十六进制私钥、常见 API key 前缀、助记词文件、
`.env` 文件名。命中即非零退出——**代码、配置、文档、日志里都不得出现可动用资产的凭据**。

用法：
    python3 tools/check_no_secrets.py [路径 ...]      # 缺省扫描仓库根目录
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules", ".frames-dump", "htmlcov"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".zst", ".sqlite3"}

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("十六进制私钥", re.compile(r"(?<![0-9a-fA-Fx])0x[0-9a-fA-F]{64}(?![0-9a-fA-F])")),
    ("助记词", re.compile(r"\b(?:mnemonic|seed phrase|助记词)\b\s*[:=]", re.IGNORECASE)),
    ("OpenAI key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("AWS key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}\b")),
    ("keystore 文件", re.compile(r"UTC--\d{4}-\d{2}-\d{2}T.*--[0-9a-fA-F]{40}")),
    # BIP32 扩展**私钥**：派生地址只需要 xpub（watch-only），xprv 一出现就是凭据泄露
    ("扩展私钥", re.compile(r"\b(?:xprv|yprv|zprv|tprv|uprv|vprv)[1-9A-HJ-NP-Za-km-z]{80,}")),
)

# 只扫这些扩展名（其余按二进制跳过）
TEXT_SUFFIXES = {
    "", ".py", ".md", ".json", ".jsonl", ".toml", ".cfg", ".ini", ".sh", ".yml", ".yaml",
    ".txt", ".html", ".css", ".service", ".js", ".ts", ".env", ".example",
}


def is_scannable(path: Path) -> bool:
    if any(part in SKIP_DIRS for part in path.parts):
        return False
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    return path.suffix.lower() in TEXT_SUFFIXES or path.name.startswith(".env")


def scan_file(path: Path) -> list[tuple[Path, int, str, str]]:
    hits: list[tuple[Path, int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return hits
    for line_no, line in enumerate(text.splitlines(), start=1):
        for label, pattern in PATTERNS:
            if pattern.search(line):
                hits.append((path, line_no, label, line.strip()[:120]))
    return hits


def scan_tree(root: Path) -> list[tuple[Path, int, str, str]]:
    hits: list[tuple[Path, int, str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and is_scannable(path):
            hits.extend(scan_file(path))
    return hits


def main(argv: list[str] | None = None) -> int:
    roots = [Path(arg) for arg in (argv or sys.argv[1:])] or [Path.cwd()]
    hits: list[tuple[Path, int, str, str]] = []
    for root in roots:
        hits.extend(scan_tree(root) if root.is_dir() else scan_file(root))
    if hits:
        for path, line_no, label, line in hits:
            print(f"{path}:{line_no}: {label} -> {line}", file=sys.stderr)
        print(f"发现 {len(hits)} 处疑似可动用资产凭据，禁止提交。", file=sys.stderr)
        return 1
    print(f"已扫描 {', '.join(str(root) for root in roots)}：零命中可动用资产凭据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
