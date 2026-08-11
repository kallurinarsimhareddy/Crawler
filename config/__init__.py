"""Runtime configuration for the crawler.

Holds the knobs that a run needs to share across modules — worker count,
whether the browser fallback is allowed, where diagnostics land — so that
adapters do not have to be threaded a settings object through every call.
"""
