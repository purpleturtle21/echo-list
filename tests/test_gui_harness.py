"""The GUI test harness itself: the app fixture is built by App.__init__,
opening a workspace writes only the isolated home, and a failing callback
neither stops the poll loop nor passes silently."""

from pathlib import Path
from unittest.mock import patch

import echolist.config as _config
import echolist.gui as gui
import echolist.journal as _journal
from echolist.gui import App
from test_gui import (app, gui_env, isolated_home, callback_errors)  # noqa: F401  (fixtures)
from test_gui_sync_safety import _reopen


def test_fixture_app_has_every_attribute_init_sets(app):
    """The shared fixture built App.__new__ by hand and missed
    _exit_after_sync / _callback_queue, so callbacks died on AttributeError."""
    with patch.object(App, "_set_icon"), patch.object(App, "_center_window"), \
            patch.object(App, "_show_setup"), patch.object(App, "_poll_callbacks"):
        real = App()
    try:
        missing = set(vars(real)) - set(vars(app))
    finally:
        real.root.destroy()
    assert not missing


def test_opening_a_workspace_writes_only_the_isolated_home(app, gui_env, tmp_path):
    """_open_workspace wrote the real ~/.echolist/default.json and read the
    real legacy journal/pending file from the shared fixture."""
    for path in (_config.DEFAULT_FILE, _config.BACKUPS_ROOT, gui.PENDING_FILE,
                 _journal.JOURNAL_FILE):
        assert Path(path).is_relative_to(tmp_path), path
    _reopen(app, gui_env)
    assert _config.DEFAULT_FILE.exists()


def test_failing_callback_does_not_stop_the_poll_loop(app, callback_errors):
    """One raising callback ended _poll_callbacks for good (no reschedule),
    hanging every later sync, playlist op and Ctrl+C."""
    ran = []

    def bad():
        raise RuntimeError("boom")

    app._schedule_callback(bad)
    app._schedule_callback(lambda: ran.append("next"))
    with patch.object(app.root, "after") as after:
        app._poll_callbacks()

    assert ran == ["next"]
    after.assert_called_once_with(16, app._poll_callbacks)
    assert [type(e[1]) for e in callback_errors] == [RuntimeError]
    callback_errors.clear()
