"""The shared demo key: the last step of key resolution, when one is set.

A free-tier Gemini key placed here lets someone try devdocs with no signup.
Its quota is shared by everyone who uses it, so `devdocs` warns whenever it is
in use and every other source of a key takes precedence. Empty means no demo
key: the key shipped with v3 was revoked by Google once it was public, and a
key that fails on every call is worse than a clear "no API key" message.
"""

__all__ = ["DEMO_KEY"]

DEMO_KEY = ""
