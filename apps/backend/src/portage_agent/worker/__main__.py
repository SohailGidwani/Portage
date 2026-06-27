"""Entry point: ``python -m portage_agent.worker``."""

import asyncio

from portage_agent.worker.main import main

if __name__ == "__main__":
    asyncio.run(main())
