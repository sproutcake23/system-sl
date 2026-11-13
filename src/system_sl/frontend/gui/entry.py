"""Entry point for the ``system-sl`` command.

This module stays deliberately tiny. The GUI lives in
:mod:`system_sl.frontend.gui.main`, which pulls in the chat panel, the icon
font and the priority engine's NLP model. The background notifier
(``--bg``) needs none of that, so the dispatch happens here, before the GUI
module is ever imported.
"""

import sys

from system_sl.utils.logging_setup import configure_logging


def main() -> int:
    """Run the background notifier or the full GUI, depending on the flags."""
    configure_logging()
    if "--bg" in sys.argv:
        from system_sl.frontend.gui.notifier import main as notifier_main
        return notifier_main()

    from system_sl.frontend.gui.main import main as app_main
    return app_main()


if __name__ == "__main__":
    sys.exit(main())
