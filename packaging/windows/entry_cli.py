"""PyInstaller entry point for the console samesame.exe."""

import multiprocessing

from dedupe.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
