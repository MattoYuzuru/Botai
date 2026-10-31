"""Model providers of the v4 runtime (docs/v4/02-architecture.md §10).

The package is deliberately light: it exports the protocol and the scripted double. Concrete HTTP
providers are imported where they are built (`botai.agent.v4.providers.factory.build_provider`),
so `botai.agent.v4.runtime` can be imported and tested without the DeepSeek/DB stack.
"""

from __future__ import annotations

from botai.agent.v4.providers.base import Provider, require_secret
from botai.agent.v4.providers.mock import MockProvider

__all__ = ["MockProvider", "Provider", "require_secret"]
