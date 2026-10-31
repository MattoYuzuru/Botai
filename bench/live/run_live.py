"""Alias of ``python -m bench.live.run`` for the command form from the task brief.

Both commands are equivalent:

    python -m bench.live.run --scenario all
    python bench/live/run_live.py --scenario all
"""

from __future__ import annotations

import sys

from .run import main

if __name__ == "__main__":
    sys.exit(main())
