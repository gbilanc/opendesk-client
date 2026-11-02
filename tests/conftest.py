"""Shared pytest configuration.

Use Qt's offscreen platform by default so UI tests do not require a display
server and do not emit clipboard-manager warnings on headless machines.
An explicit ``QT_QPA_PLATFORM`` in the environment still takes precedence.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
