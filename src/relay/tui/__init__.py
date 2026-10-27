"""Textual conversational interface (design 14.1, 15; phase 2c).

Importing this package does not import Textual: `relay.tui.app` does, and only the `tui`
command (and bare `relay` on a terminal) imports it. Widgets project the state of
`relay.core.session_app.SessionApp` and submit commands to it; they contain no execution logic.
"""
