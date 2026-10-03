"""启动入口：python -m green_match（数据库路径取环境变量 GREEN_MATCH_DB）。"""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    from .api import create_app

    db_path = os.environ.get("GREEN_MATCH_DB", "green_match.db")
    app = create_app(db_path)
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
