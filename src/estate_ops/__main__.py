"""Command line: python -m estate_ops {migrate|seed|discover|provision|api|worker}."""

from __future__ import annotations

import argparse
import asyncio
import json
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


def cmd_discover(settings: Settings) -> None:
    from .app import build
    from .worker import discover_all

    if not (settings.netbox_url and settings.netbox_token):
        print("set NETBOX_URL and NETBOX_TOKEN")
        return
    platform = build(settings)
    print(json.dumps(discover_all(platform), indent=2))


def cmd_provision(settings: Settings, what: str, tenant_ids: list[str]) -> None:
    from .app import build
    from .integrations import provision

    platform = build(settings)
    tenants = [platform.tenants.get(t) for t in tenant_ids] if tenant_ids else list(platform.tenants)
    report: dict[str, dict] = {}
    for t in tenants:
        if what in ("semaphore", "all"):
            report.setdefault(t.id, {})["semaphore"] = provision.semaphore_templates(platform.semaphore, platform.catalog, t)
        if what in ("openobserve", "all"):
            admin = provision.OpenObserveAdmin(settings.openobserve_url, settings.openobserve_user, settings.openobserve_password)
            report.setdefault(t.id, {})["openobserve"] = admin.provision(platform.catalog, t, settings.public_url)
    print(json.dumps(report, indent=2))


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
    sub.add_parser("discover", help="read NetBox into the estate graph once")
    prov = sub.add_parser("provision", help="create Semaphore templates and OpenObserve alerts from the catalog")
    prov.add_argument("what", choices=["semaphore", "openobserve", "all"])
    prov.add_argument("--tenant", action="append", default=[])
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
    elif args.cmd == "discover":
        cmd_discover(settings)
    elif args.cmd == "provision":
        cmd_provision(settings, args.what, args.tenant)
    elif args.cmd == "api":
        cmd_api(settings, args.host, args.port)
    elif args.cmd == "worker":
        cmd_worker(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
