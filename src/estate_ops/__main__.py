"""Command line: python -m estate_ops {migrate|seed|api|worker}."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

import psycopg

from . import db, estate, jobs
from .config import Settings
from .tenancy import Tenants

log = logging.getLogger("estate_ops")


def cmd_migrate(settings: Settings) -> None:
    applied = db.migrate(settings.database_url)
    Tenants.load(settings.tenants_file).sync(settings.database_url)
    print(f"migrations applied: {applied or 'none'}; tenants synced")


def cmd_seed(settings: Settings) -> None:
    if not settings.seed_file:
        print("no SEED_FILE set")
        return
    with psycopg.connect(settings.database_url) as conn:
        n = estate.load_seed(conn, settings.seed_file)
    print(f"seeded {n} assets from {settings.seed_file}")


def cmd_api(settings: Settings, host: str, port: int) -> None:
    import uvicorn

    from .api import create_app
    from .app import build

    uvicorn.run(create_app(build(settings)), host=host, port=port, log_level="info")


def cmd_worker(settings: Settings) -> None:
    from .app import build

    platform = build(settings)
    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("worker started (model: %s)", settings.diagnosis_model if platform.anthropic else "none, deterministic only")
    while not stop:
        job = jobs.claim(platform.db)
        if job is None:
            time.sleep(1.0)
            continue
        try:
            log.info("job %s %s %s", job["id"], job["kind"], platform.handle(job))
        except Exception as exc:
            retry = jobs.fail(platform.db, job, f"{type(exc).__name__}: {exc}")
            log.exception("job %s %s failed (%s)", job["id"], job["kind"], "will retry" if retry else "giving up")


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
