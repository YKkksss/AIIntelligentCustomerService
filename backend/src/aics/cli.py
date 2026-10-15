"""`aics-cli` 入口。M0 只有骨架；bootstrap-admin（M4）、reindex / reconcile（M1）、eval（M5）随里程碑实现。"""

from __future__ import annotations

import argparse
import sys

PLANNED = {
    "bootstrap-admin": "M4：读取 AICS_BOOTSTRAP_ADMIN_* 创建初始管理员",
    "reindex": "M1：重建应用索引",
    "reconcile": "M1：PG 与 Qdrant 对账",
    "eval": "M5：运行评测",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aics-cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version", help="显示版本")
    for name, help_ in PLANNED.items():
        sub.add_parser(name, help=help_)
    args = parser.parse_args(argv)
    if args.cmd == "version":
        from aics import __version__

        print(__version__)
        return 0
    print(f"{args.cmd}: 尚未实现（{PLANNED[args.cmd]}）", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
