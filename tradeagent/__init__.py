"""tradeagent: enforcement + journaling layer around a Claude Code trading agent.

Nothing in this package calls a model. It reads Claude Code hook payloads,
decides whether an order may go through, records everything, and exposes a CLI.
"""

__version__ = "0.1.0"
