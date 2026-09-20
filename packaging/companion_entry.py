"""Frozen application entry point; retain module imports for PyInstaller hooks."""
import tkinter
import bleak
from meter.__main__ import main

if __name__ == '__main__':
    raise SystemExit(main())
