# Vercel entry point: exposes the ASGI app.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hosted import app  # noqa: E402,F401
