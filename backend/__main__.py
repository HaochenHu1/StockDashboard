"""Start the dashboard:  python -m backend [--mock] [--config config.yaml] [--port 8600]"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import shutil
import sys
from pathlib import Path

import uvicorn

from .app import create_app
from .config import ConfigStore
from .envfile import load_env

BASE_DIR = Path(__file__).resolve().parent.parent


def setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m backend", description="moomoo 本地股票与期权监控看板")
    ap.add_argument("--mock", action="store_true", help="模拟模式：不连接 OpenD，生成模拟行情（休市时测试界面）")
    ap.add_argument("--config", default=None,
                    help="配置文件路径（默认 config.yaml；模拟模式默认 config.mock.yaml，首次运行时从 config.yaml 复制）")
    ap.add_argument("--port", type=int, default=None, help="看板端口（默认取配置文件 server.port）")
    args = ap.parse_args()

    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.is_absolute():
            cfg_path = (Path.cwd() / cfg_path) if (Path.cwd() / cfg_path).exists() else BASE_DIR / cfg_path
    elif args.mock:
        # mock mode keeps its own config so experiments (accounts, limits, watchlist) never leak into live use
        cfg_path = BASE_DIR / "config.mock.yaml"
        if not cfg_path.exists():
            shutil.copyfile(BASE_DIR / "config.yaml", cfg_path)
    else:
        cfg_path = BASE_DIR / "config.yaml"
    setup_logging(BASE_DIR / "logs" / ("runtime-mock.log" if args.mock else "runtime.log"))
    loaded = load_env(BASE_DIR / ".env")   # API keys for the server only (values are never logged)
    if loaded:
        logging.getLogger(__name__).info("loaded from .env: %s", ", ".join(loaded))
    port =args.port or int(ConfigStore(cfg_path).get("server", "port", default=8600))
    app = create_app(cfg_path, args.mock, BASE_DIR, port)
    logging.getLogger(__name__).info("dashboard: http://127.0.0.1:%d  (mode=%s, config=%s)",
                                     port, "mock" if args.mock else "OpenD", cfg_path)
    # bind to 127.0.0.1 only - never 0.0.0.0
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", ws="websockets-sansio")


if __name__ == "__main__":
    main()
