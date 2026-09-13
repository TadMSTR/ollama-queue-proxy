"""ollama-queue-proxy: Drop-in HTTP proxy for Ollama with priority queuing."""

# THE single in-code version. `pyproject.toml` carries the packaging copy; nothing else
# in `src/` may hold a version literal, and `tests/test_version_parity.py` enforces both
# halves of that.
#
# Why a literal rather than `importlib.metadata.version(...)`: under the editable install
# CI and local dev both use, the installed metadata can lag `pyproject.toml` until the
# package is reinstalled. Reading it here would make the parity test flap on a stale
# install instead of catching real drift — reporting a problem when there is none, and
# training people to re-run rather than look.
#
# This drifted twice before the check existed: `__version__` stuck at 0.3.1 through
# v0.3.2, and at 0.4.0 through v0.5.0, while `main.py` advertised 0.2.0 to every
# OpenAPI consumer from v0.3.0 onward without ever being bumped.
__version__ = "0.5.1"
