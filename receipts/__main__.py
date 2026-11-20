import argparse
import logging
import sys

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox

from . import config


def _menu_bar_only() -> None:
    """On a Mac, live in the menu bar like other utilities instead of showing a Python icon in the Dock."""
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception:  # cosmetic; never worth failing over
        logging.getLogger(__name__).debug("Couldn't hide the Dock icon", exc_info=True)


def main() -> int:
    parser = argparse.ArgumentParser(prog="receipts", description="Hotkey fact-checker for voice chat.")
    parser.add_argument("--no-hotkeys", action="store_true", help="don't register global hotkeys")
    parser.add_argument("--after-update", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(config.LOG_PATH, encoding="utf-8")]
        + ([logging.StreamHandler()] if sys.stderr else []),  # pythonw has no console
    )

    app = QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    if sys.platform == "darwin":
        _menu_bar_only()
    app.setQuitOnLastWindowClosed(False)  # closing the popup just hides it; quit from the tray

    from .ui import apply_theme

    apply_theme(app)

    lock = QLockFile(str(config.CONFIG_DIR / "receipts.lock"))
    # After an update the old copy may take a moment to exit and release the lock.
    if not lock.tryLock(15000 if args.after_update else 100):
        QMessageBox.information(None, config.APP_NAME, "Receipts is already running. Look for it in the system tray.")
        return 0

    from .app import Controller

    controller = Controller(app, use_hotkeys=not args.no_hotkeys, after_update=args.after_update)  # noqa: F841
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
