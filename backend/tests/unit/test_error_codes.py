"""错误码一致性（M3.6 ③）：接口规范 §4 的表格与代码里的 ``AppError`` 子类必须一一对应。

表格是**对外契约**（接入方按 `code` 分支），代码是最容易漂移的一侧：新增一个
``AppError`` 子类却忘了写进文档、或文档里提前写了还没实现的码，都会让接入方
按文档写出来的分支永远走不到。本测试双向比对：

- 文档里有、代码里没有的码只能在 ``PENDING`` 里（尚未实现的里程碑），且必须写清原因；
- 代码里有、文档里没有的码不允许；
- 同一个码在两侧的状态码必须一致（`PARSE_FAILED` 例外：只写进 `documents.error` 与 job，
  接口不会直接返回，文档里 HTTP 列写「—」）；
- 每个码都要有非空的中文 ``message``（错误体里的 `message` 直接来自它）。
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path

import aics
from aics.core.errors import AppError

DOC = Path(__file__).resolve().parents[3] / "docs" / "04-api" / "01-接口规范.md"

# 文档里写了、代码里还没有的码：值写明「谁在哪一步实现」，实现后必须从这里删掉
# （删掉的那一刻，另一侧比对会自然变绿）。
PENDING = {
    "TOKEN_EXPIRED": "JWT 登录（M4.1）",
    "ACCOUNT_LOCKED": "登录锁定（M4.1）",
}

# 不作为 HTTP 响应返回的码：文档 HTTP 列写「—」，仍要求代码里有。
NOT_A_RESPONSE = {"PARSE_FAILED"}

_CODE_RE = re.compile(r"`([A-Z][A-Z0-9_]*)`")
_STATUS_RE = re.compile(r"^(\d{3})$")
_LITERAL_RE = re.compile(r"""\bcode\s*[:=]\s*["']([A-Za-z][A-Za-z0-9_]*)["']""")
SRC = Path(__file__).resolve().parents[2] / "src" / "aics"


def documented() -> dict[str, int | None]:
    """解析接口规范 §4 的错误码表 → {code: HTTP 状态码 | None}。"""

    text = DOC.read_text(encoding="utf-8")
    start = text.index("## 4. 错误码")
    end = text.index("\n## ", start + 1)
    table = text[start:end]
    found: dict[str, int | None] = {}
    for line in table.splitlines():
        if not line.startswith("| "):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[1] == "code":
            continue
        http_cell, code_cell = cells[0], cells[1]
        match = _STATUS_RE.match(http_cell)
        status = int(match.group(1)) if match else None
        for code in _CODE_RE.findall(code_cell):
            if code in found:
                raise AssertionError(f"错误码表里 {code} 出现了两次")
            found[code] = status
    assert found, "错误码表没有解析出任何码"
    return found


def implemented() -> dict[str, type[AppError]]:
    """遍历 ``aics`` 包，收集带 ``code`` 的 ``AppError`` 子类。"""

    classes: dict[str, type[AppError]] = {}
    for info in pkgutil.walk_packages(aics.__path__, prefix="aics."):
        module = importlib.import_module(info.name)
        for name in dir(module):
            obj = getattr(module, name)
            if not (isinstance(obj, type) and issubclass(obj, AppError)):
                continue
            code = getattr(obj, "code", "")
            if not code:
                continue
            known = classes.get(code)
            if known is not None and known.status != obj.status:
                raise AssertionError(
                    f"{code} 在 {known.__name__} 与 {obj.__name__} 里的状态码不一致"
                )
            classes.setdefault(code, obj)
    return classes


def test_every_documented_code_is_implemented_or_pending() -> None:
    items = implemented()
    missing = set(documented()) - set(items) - set(PENDING)
    assert not missing, f"文档里有、代码里没有的码：{sorted(missing)}"


def test_every_implemented_code_is_documented() -> None:
    items = implemented()
    extra = set(items) - set(documented())
    assert not extra, f"代码里有、文档里没有的码：{sorted(extra)}"


def test_pending_codes_are_not_implemented_yet() -> None:
    items = implemented()
    done = set(PENDING) & set(items)
    assert not done, f"这些码已经实现，请从 PENDING 里删掉：{sorted(done)}"


def test_status_codes_match_the_documentation() -> None:
    items = implemented()
    table = documented()
    for code, status in table.items():
        if code not in items or status is None:
            continue
        assert items[code].status == status, f"{code} 文档写 {status}，代码是 {items[code].status}"


def test_parse_failed_is_not_returned_directly() -> None:
    items = implemented()
    assert "PARSE_FAILED" in NOT_A_RESPONSE
    assert documented()["PARSE_FAILED"] is None
    assert items["PARSE_FAILED"].status == 422


def test_every_code_has_a_message() -> None:
    for code, cls in implemented().items():
        message = getattr(cls, "message", "")
        assert message, f"{code}（{cls.__name__}）没有 message"
        assert any("\u4e00" <= ch <= "\u9fff" for ch in message), (
            f"{code} 的 message 不是中文：{message!r}"
        )


def test_no_code_escapes_the_app_error_hierarchy() -> None:
    """全仓 ``code=`` / ``code:`` 字面量都必须来自 ``AppError`` 子类，否则本测试就漏掉了它。"""

    items = implemented()
    found: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for code in _LITERAL_RE.findall(line):
                if code.isupper():
                    found.setdefault(code, []).append(f"{path.relative_to(SRC)}:{number}")
    stray = {code: where for code, where in found.items() if code not in items}
    assert not stray, f"这些 code 字面量不是 AppError 子类上的：{stray}"
