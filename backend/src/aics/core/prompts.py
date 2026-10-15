"""提示词文件加载与版本检查（ADR-0009，docs/02-architecture/03-RAG流程设计.md §5）。

- 提示词只放在 `rag/prompts/`、`memory/prompts/`、`modules/eval/prompts/` 下的 `*.md`；
  文件头 YAML front-matter 必含 `name`（= 文件名）与 `version`（正整数）。
- 版本检查：`backend/prompts.lock.json` 记录每个文件的 `version` 与正文 sha256。
  正文变化而 version 未增加 → 失败；version 增加但锁文件未更新 → 失败（提示运行 update）。
  CLI：`python -m aics.core.prompts check|update`（CI 运行 check）。
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # src/aics
PROMPT_DIRS: tuple[str, ...] = ("rag/prompts", "memory/prompts", "modules/eval/prompts")
DEFAULT_LOCK = PACKAGE_ROOT.parent.parent / "prompts.lock.json"  # backend/prompts.lock.json


class PromptError(ValueError):
    pass


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    description: str
    body: str
    path: Path

    @property
    def body_hash(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()


def parse_prompt(text: str, path: Path) -> Prompt:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise PromptError(f"{path}: 缺少 YAML front-matter")
    end = text.find("\n---", 4)
    if end < 0:
        raise PromptError(f"{path}: front-matter 未闭合")
    meta = yaml.safe_load(text[4:end]) or {}
    if not isinstance(meta, dict):
        raise PromptError(f"{path}: front-matter 必须是映射")
    body = text[end + 4 :].lstrip("\n")
    name, version = meta.get("name"), meta.get("version")
    if not isinstance(name, str) or not name:
        raise PromptError(f"{path}: front-matter 缺少 name")
    if name != path.stem:
        raise PromptError(f"{path}: name={name!r} 与文件名 {path.stem!r} 不一致")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PromptError(f"{path}: version 必须是正整数")
    if not body.strip():
        raise PromptError(f"{path}: 正文为空")
    return Prompt(name, version, str(meta.get("description", "")), body, path)


def load_prompt_file(path: Path) -> Prompt:
    return parse_prompt(path.read_text(encoding="utf-8"), path)


def discover(root: Path = PACKAGE_ROOT) -> dict[str, Prompt]:
    """返回 {相对 root 的 posix 路径: Prompt}。"""
    found: dict[str, Prompt] = {}
    for d in PROMPT_DIRS:
        for p in sorted((root / d).glob("*.md")):
            found[p.relative_to(root).as_posix()] = load_prompt_file(p)
    return found


@cache
def get_prompt(rel_path: str) -> Prompt:
    """按相对 `aics/` 的路径加载，如 `rag/prompts/generate.md`（进程内缓存）。"""
    return load_prompt_file(PACKAGE_ROOT / rel_path)


def check(root: Path = PACKAGE_ROOT, lock_path: Path = DEFAULT_LOCK) -> list[str]:
    """返回问题列表；为空表示通过。"""
    lock: dict[str, dict[str, object]] = (
        json.loads(lock_path.read_text(encoding="utf-8")) if lock_path.exists() else {}
    )
    try:
        current = discover(root)
    except PromptError as e:
        return [str(e)]
    problems: list[str] = []
    for rel, p in current.items():
        old = lock.get(rel)
        if old is None:
            problems.append(f"{rel}: 新提示词未登记，运行 `python -m aics.core.prompts update`")
            continue
        old_v, old_h = int(old["version"]), str(old["sha256"])  # type: ignore[call-overload]
        if p.body_hash != old_h and p.version <= old_v:
            problems.append(f"{rel}: 正文已修改但 version 未增加（当前 {p.version}）")
        elif p.version < old_v:
            problems.append(f"{rel}: version 不能回退（{old_v} → {p.version}）")
        elif p.version != old_v or p.body_hash != old_h:
            problems.append(f"{rel}: 版本已变更但锁文件未更新，运行 update")
    for rel in lock.keys() - current.keys():
        problems.append(f"{rel}: 文件已删除但仍在锁文件中，运行 update")
    return problems


def update(root: Path = PACKAGE_ROOT, lock_path: Path = DEFAULT_LOCK) -> list[str]:
    """重写锁文件；正文变化而未升版本的文件拒绝写入并返回问题。"""
    problems = [x for x in check(root, lock_path) if "version 未增加" in x or "回退" in x]
    if problems:
        return problems
    data = {
        rel: {"version": p.version, "sha256": p.body_hash}
        for rel, p in sorted(discover(root).items())
    }
    lock_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return []


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    cmd = args[0] if args else "check"
    if cmd not in {"check", "update"}:
        print("用法：python -m aics.core.prompts check|update", file=sys.stderr)
        return 2
    problems = check() if cmd == "check" else update()
    for p in problems:
        print(f"[prompts] {p}", file=sys.stderr)
    if not problems:
        print(f"[prompts] {cmd} ok（{len(discover())} 个提示词）")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
