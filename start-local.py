"""Start the free local simulator from any working directory; no pip or AWS needed."""
from pathlib import Path
import sys

if sys.version_info < (3, 11):
    raise SystemExit('Capital Ledger requires Python 3.11 or newer.')

sys.path.insert(0, str(Path(__file__).resolve().parent / 'backend'))

from ledger.local_server import main

if __name__ == '__main__':
    main()
