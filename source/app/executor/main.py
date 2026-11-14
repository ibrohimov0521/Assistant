from __future__ import annotations

import asyncio
import logging

from app.core.config import Settings
from app.executor.worker import run_executor


async def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(level=settings.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    await run_executor(settings)


if __name__ == "__main__":
    asyncio.run(main())

