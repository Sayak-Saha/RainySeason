import asyncio

from rainyai.core import main

import rainyai.commands
import rainyai.events
import rainyai.views


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down...")
