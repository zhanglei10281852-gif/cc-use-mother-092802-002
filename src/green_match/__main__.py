"""服务入口：python -m green_match

数据库路径由环境变量 GREEN_MATCH_DB 指定（默认 ./green_match.db）。
"""

import os

import uvicorn

from .api import create_app

app = create_app()


def main() -> None:
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
