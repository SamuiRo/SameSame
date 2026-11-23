"""PyInstaller entry point for the windowed SameSame.exe."""

import multiprocessing

from dedupe.gui.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
