"""devdocs — documentation generation with no runtime dependencies.

Everything here is stdlib. Providers are reached over `http.client`, storage is
`sqlite3`, and rendering is hand-written, so installing this package pulls
nothing else in.

`__all__` is the supported surface; anything not listed is private and may
change without a major version bump.
"""

__version__ = "4.0.0"

__all__ = ["__version__"]
