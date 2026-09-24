#!/usr/bin/env python3
"""Smoke test for the Meta Model API connection.

Reads YAADHAMMA_MODEL_API_KEY (and friends) from the environment only,
sends one tiny chat completion, and prints the model that answered.
Exits non-zero with a clear message when no key is configured.

Usage:  uv run python scripts/smoke_meta.py
"""

import asyncio
import sys

sys.path.insert(0, "src")

from meta_client import MetaBrainClient, MetaConfig, MetaConfigError


async def main() -> int:
    cfg = MetaConfig.from_env()
    try:
        turn = await MetaBrainClient(cfg).generate(
            cfg.brain_model,
            [{"role": "user", "content": "Reply with exactly: smoke ok"}],
            [],
        )
    except MetaConfigError as exc:
        print(f"Not configured: {exc}")
        return 2
    print(f"Model answered via {cfg.brain_model}: {turn.text.strip()!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
