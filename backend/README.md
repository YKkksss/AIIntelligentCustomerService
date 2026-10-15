# AICS 后端

Python 3.12 + uv + FastAPI。环境搭建与常用命令见 [开发环境搭建](../docs/06-development/02-开发环境搭建.md)。

```bash
uv sync
uv run aics-api                        # http://localhost:8000/readyz
uv run ruff check . && uv run ruff format --check .
uv run lint-imports                    # 模块依赖规则
uv run python -m aics.core.prompts check
uv run pytest
uv run alembic upgrade head
```
