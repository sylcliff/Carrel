"""Wrapper that launches @brave/brave-search-mcp-server with ``--use-env-proxy``.

Why this exists
---------------

Node's global ``fetch`` (undici under the hood) does **not** honor
``HTTPS_PROXY`` / ``HTTP_PROXY`` on its own. Node 22+ ships a
``--use-env-proxy`` flag that turns that on for the whole process,
but the flag is consumed by the ``node`` binary itself, so it has
to be on the command line of the process that actually issues the
HTTP request.

``@brave/brave-search-mcp-server`` is launched via ``npx``, and
``npx`` has no way to forward node flags to the inner process. So
we have to:

1. Use ``npx -y`` once to ensure the package is downloaded (and
   therefore present in ``~/.npm/_npx/<hash>/node_modules/...``).
2. Find the cached package directory.
3. Spawn ``node --use-env-proxy <cached>/dist/index.js <args>``
   with the same env Carrel would have set for npx.

This module is the command Carrel's MCP registry runs instead of
``npx``. The registry's env-layer rewrite still works exactly the
same way: the rewritten env (e.g. ``HTTPS_PROXY=http://127.0.0.1:
<bridge>``) is what triggers ``--use-env-proxy`` to route through
our SOCKS→HTTP bridge.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PACKAGE_NAME = "@brave/brave-search-mcp-server"
ENTRY_REL = "dist/index.js"

# Reuse the npx install cache rather than vendoring the package —
# lets users upgrade the Brave MCP server with a plain ``npx -y``
# invocation, same as today.
NPX_CACHE = Path.home() / ".npm" / "_npx"


def _cached_entry() -> Path:
    """Return the path of the most recent cached install.

    Searches ``~/.npm/_npx/*/node_modules/<PACKAGE_NAME>/<ENTRY_REL>``
    and picks the directory with the most recent mtime. If no cache
    hit is found, falls back to running ``npx -y`` to populate it.
    """
    candidates: list[tuple[float, Path]] = []
    if NPX_CACHE.is_dir():
        for pkg_dir in NPX_CACHE.glob(f"*/node_modules/{PACKAGE_NAME}"):
            entry = pkg_dir / ENTRY_REL
            if entry.is_file():
                candidates.append((entry.stat().st_mtime, entry))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]

    # Cold cache: trigger an npx run to populate it, then retry. We
    # invoke ``--version`` because every MCP server ``bin`` script
    # supports it; if it doesn't, the run still downloads the
    # package, which is all we need.
    npx = shutil.which("npx")
    if npx is None:
        raise FileNotFoundError("npx not found on PATH; install Node.js first")
    subprocess.run(
        [npx, "-y", PACKAGE_NAME, "--version"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return _cached_entry()


def main() -> int:
    node = shutil.which("node")
    if node is None:
        raise FileNotFoundError("node not found on PATH")
    entry = _cached_entry()
    argv = [node, "--use-env-proxy", str(entry), *sys.argv[1:]]
    return subprocess.call(argv, env=os.environ)


if __name__ == "__main__":
    sys.exit(main())
