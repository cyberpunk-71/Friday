"""python -m core.worker — background SETTLE process (VM systemd unit)."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_db  # noqa: E402
from core.worker import Worker  # noqa: E402


async def main():
    w = Worker(db=get_db())
    print("friday worker: SETTLE loop started", flush=True)
    await w.run_forever(tick_s=10.0)


if __name__ == "__main__":
    asyncio.run(main())
