import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent.parent.parent  # repo root
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from uat.visualize import main as _main
    _main()
