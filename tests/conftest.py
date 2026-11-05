"""Make the repository root importable so tests can import isce_dolphin_mintpy."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
