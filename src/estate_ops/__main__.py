"""Command line: python -m estate_ops {migrate|seed|api|worker}."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from . import db
from .config import Settings
from .estate import EstateGraph
from .tenancy import Tenants

log = logging.getLogger("estate_ops")


def cmd_migrate(settings: Settings) -> None:
    applied = db.migrate(settings.database_url)
    Tenants.load(settings.tenants_file).sync(settings.database_url)
    graph = EstateGraph(settings.neo4j_url, settings.neo4j_user, settings.neo4j_password)
    graph.ensure_schema()
    graph.close()
    print(f"migrations applied: {applied or 'none'}; tenants synced; graph schema ensured")


def cmd_seed(settings: Settings) -> None:
    if not settings.seed_file:
        print("no SEED_FILE set")
        return
    graph = EstateGraph(settings.neo4j_url, settings.neo4j_user, settings.neo4j_password)
    graph.ensure_schema()
    n = graph.load_seed(settings.seed_file)
    graph.close()
    print(f"seeded {n} assets from {settings.seed_file}")


def cmd_api(settings: Settings, host: str, port: int) -> None:
    import uvicorn

    from .api import create_app
    from .app import build

    uvicorn.run(create_app(build(settings)), host=host, port=port, log_level="info")


def cmd_worker(settings: Settings) -> None:
    from . import worker
    from .app import build

    platform = build(settings)

    async def _run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await worker.run(platform, stop)

    asyncio.run(_run())


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="estate_ops")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("seed")
    api = sub.add_parser("api")
    api.add_argument("--host", default="0.0.0.0")
    api.add_argument("--port", type=int, default=8080)
    sub.add_parser("worker")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    if args.cmd == "migrate":
        cmd_migrate(settings)
    elif args.cmd == "seed":
        cmd_seed(settings)
    elif args.cmd == "api":
        cmd_api(settings, args.host, args.port)
    elif args.cmd == "worker":
        cmd_worker(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
