"""Explicit Qt entry point; the maintained classic launcher remains available."""
from studio.ui_qt.app import main

if __name__ == '__main__':
    raise SystemExit(main())
