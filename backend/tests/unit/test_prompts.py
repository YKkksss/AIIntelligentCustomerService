import json
from pathlib import Path

import pytest

from aics.core import prompts
from aics.core.prompts import PromptError, check, discover, parse_prompt, update


def write(root: Path, rel: str, name: str, version: int, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nname: {name}\nversion: {version}\ndescription: d\n---\n{body}", "utf-8")
    return p


def test_parse_ok(tmp_path: Path) -> None:
    p = write(tmp_path, "rag/prompts/generate.md", "generate", 5, "你是{app_name}的客服。\n")
    pr = prompts.load_prompt_file(p)
    assert (pr.name, pr.version, pr.description) == ("generate", 5, "d")
    assert pr.body == "你是{app_name}的客服。\n"
    assert len(pr.body_hash) == 64


def test_crlf_same_hash(tmp_path: Path) -> None:
    a = parse_prompt("---\nname: a\nversion: 1\n---\nx\ny\n", tmp_path / "a.md")
    b = parse_prompt("---\r\nname: a\r\nversion: 1\r\n---\r\nx\r\ny\r\n", tmp_path / "a.md")
    assert a.body_hash == b.body_hash


@pytest.mark.parametrize(
    ("text", "msg"),
    [
        ("no front matter", "front-matter"),
        ("---\nname: a\nversion: 1\n", "未闭合"),
        ("---\nversion: 1\n---\nx", "name"),
        ("---\nname: b\nversion: 1\n---\nx", "不一致"),
        ("---\nname: a\nversion: 0\n---\nx", "正整数"),
        ("---\nname: a\nversion: '2'\n---\nx", "正整数"),
        ("---\nname: a\nversion: 1\n---\n   \n", "正文为空"),
    ],
)
def test_parse_errors(tmp_path: Path, text: str, msg: str) -> None:
    with pytest.raises(PromptError, match=msg):
        parse_prompt(text, tmp_path / "a.md")


def test_check_workflow(tmp_path: Path) -> None:
    lock = tmp_path / "prompts.lock.json"
    write(tmp_path, "rag/prompts/judge.md", "judge", 1, "body v1")
    write(tmp_path, "memory/prompts/compact.md", "compact", 1, "c")
    assert any("未登记" in x for x in check(tmp_path, lock))
    assert update(tmp_path, lock) == []
    assert check(tmp_path, lock) == []
    assert set(json.loads(lock.read_text("utf-8"))) == {
        "rag/prompts/judge.md",
        "memory/prompts/compact.md",
    }

    # 改正文不升版本 → 失败，update 也拒绝
    write(tmp_path, "rag/prompts/judge.md", "judge", 1, "body v2")
    assert any("version 未增加" in x for x in check(tmp_path, lock))
    assert update(tmp_path, lock) != []

    # 升版本但未更新锁文件 → 失败；update 后通过
    write(tmp_path, "rag/prompts/judge.md", "judge", 2, "body v2")
    assert any("锁文件未更新" in x for x in check(tmp_path, lock))
    assert update(tmp_path, lock) == []
    assert check(tmp_path, lock) == []

    # 删除文件 → 失败
    (tmp_path / "memory/prompts/compact.md").unlink()
    assert any("已删除" in x for x in check(tmp_path, lock))


def test_discover_only_prompt_dirs(tmp_path: Path) -> None:
    write(tmp_path, "rag/prompts/a.md", "a", 1, "x")
    write(tmp_path, "modules/eval/prompts/eval_judge.md", "eval_judge", 1, "x")
    write(tmp_path, "rag/other/b.md", "b", 1, "x")
    assert set(discover(tmp_path)) == {"rag/prompts/a.md", "modules/eval/prompts/eval_judge.md"}


def test_repo_prompts_consistent() -> None:
    """仓库中的提示词与 prompts.lock.json 一致（与 CI 的 check 相同）。"""
    assert check() == []


def test_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert prompts.main(["check"]) == 0
    assert prompts.main(["bogus"]) == 2
