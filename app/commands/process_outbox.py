import argparse
import asyncio

from app.core.config import settings
from app.services.outbound_worker_service import run_worker_once


async def _run(once: bool) -> None:
    while True:
        processed = await run_worker_once()
        if once:
            return
        if processed == 0:
            await asyncio.sleep(settings.mail_worker_poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Process durable outbound email jobs")
    parser.add_argument("--once", action="store_true", help="Process one available batch and exit")
    args = parser.parse_args()
    asyncio.run(_run(args.once))


if __name__ == "__main__":
    main()
