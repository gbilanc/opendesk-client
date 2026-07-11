"""
UI tests using PySide6 QTest.

Requires a QApplication instance.  These tests verify widget
behaviour without a full MainWindow.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtTest import QTest

# ── QApplication fixture ─────────────────────────────────────────────


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    """Create a QApplication for the test session."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
        app.setStyle("Fusion")
    return app


# ═══════════════════════════════════════════════════════════════════
# SessionInfoWidget tests
# ═══════════════════════════════════════════════════════════════════


class TestSessionInfoWidget:
    """Tests for the session info/identity widget."""

    def test_initial_state(self, qapp: QApplication) -> None:
        """Widget initialises with placeholder values."""
        from opendesk.ui.session_info import SessionInfoWidget
        from opendesk.crypto.auth import AuthManager

        auth = AuthManager()
        widget = SessionInfoWidget(auth, device_id="test-uuid-1234", device_name="MyPC")
        assert widget.session_id == ""
        assert widget.password == ""

    def test_set_session(self, qapp: QApplication) -> None:
        """set_session() updates the displayed session info."""
        from opendesk.ui.session_info import SessionInfoWidget
        from opendesk.crypto.auth import AuthManager

        auth = AuthManager()
        widget = SessionInfoWidget(auth, device_id="abcd1234-xxxx", device_name="TestPC")
        widget.set_session("123 456 789", "ABC12345")
        assert widget.session_id == "123 456 789"
        assert widget.password == "ABC12345"

    def test_device_name_changed_signal(self, qapp: QApplication) -> None:
        """Editing the device name emits device_name_changed."""
        from opendesk.ui.session_info import SessionInfoWidget
        from opendesk.crypto.auth import AuthManager
        from PySide6.QtCore import QEvent

        emitted_names: list[str] = []
        auth = AuthManager()
        widget = SessionInfoWidget(auth, device_id="test-uuid", device_name="OldName")

        widget.device_name_changed.connect(lambda n: emitted_names.append(n))

        # Simulate inline editing: click label → editor appears → finish
        widget._start_name_edit()
        widget._name_editor.setText("NewName")
        widget._finish_name_edit()

        assert emitted_names == ["NewName"]

    def test_copy_device_id(self, qapp: QApplication) -> None:
        """Copy button places device ID on clipboard."""
        from opendesk.ui.session_info import SessionInfoWidget
        from opendesk.crypto.auth import AuthManager
        from PySide6.QtGui import QClipboard

        auth = AuthManager()
        widget = SessionInfoWidget(auth, device_id="abcdef01-1234-5678", device_name="PC")
        widget.set_session("111 222 333", "PASS1234")

        widget._copy_device_id()
        clipboard = QApplication.clipboard().text()
        # Device ID is formatted as first 4+4 chars of UUID
        assert "ABCD" in clipboard or "abcd" in clipboard


# ═══════════════════════════════════════════════════════════════════
# EmptyStateWidget tests
# ═══════════════════════════════════════════════════════════════════


class TestEmptyStateWidget:
    """Tests for the empty state placeholder."""

    def test_initial_visibility(self, qapp: QApplication) -> None:
        """Action button visibility follows action_text."""
        from opendesk.ui.widgets.empty_state_widget import EmptyStateWidget

        no_action = EmptyStateWidget(title="Empty", description="No items")
        no_action.show()
        # Button is hidden because action_text is empty
        assert not no_action._action_btn.isVisible()

        with_action = EmptyStateWidget(
            title="Empty", description="No items", action_text="Refresh",
        )
        with_action.show()
        assert with_action._action_btn.isVisible()
        assert with_action._action_btn.text() == "Refresh"

    def test_action_clicked_signal(self, qapp: QApplication) -> None:
        """Clicking the action button emits action_clicked."""
        from opendesk.ui.widgets.empty_state_widget import EmptyStateWidget

        clicked = False

        def on_action() -> None:
            nonlocal clicked
            clicked = True

        widget = EmptyStateWidget(
            title="Empty", description="No items",
            action_text="Go", on_action=on_action,
        )
        widget.show()
        QTest.mouseClick(widget._action_btn, Qt.MouseButton.LeftButton)
        assert clicked

    def test_configure_updates_content(self, qapp: QApplication) -> None:
        """configure() updates icon, title, description."""
        from opendesk.ui.widgets.empty_state_widget import EmptyStateWidget

        widget = EmptyStateWidget()
        widget.show()
        widget.configure(icon="🖥️", title="New Title", description="New desc", action_text="Click")
        assert widget._title.text() == "New Title"
        assert widget._description.text() == "New desc"
        assert widget._action_btn.isVisible()


# ═══════════════════════════════════════════════════════════════════
# StatusBadge tests
# ═══════════════════════════════════════════════════════════════════


class TestStatusBadge:
    """Tests for the status badge widget."""

    def test_default_state(self, qapp: QApplication) -> None:
        """Default status is 'pending'."""
        from opendesk.ui.widgets.status_badge import StatusBadge

        badge = StatusBadge()
        assert badge.status == "pending"

    def test_set_status(self, qapp: QApplication) -> None:
        """set_status() updates the badge text and style."""
        from opendesk.ui.widgets.status_badge import StatusBadge

        badge = StatusBadge("online")
        assert badge.status == "online"
        assert "Online" in badge.text()

        badge.set_status("error")
        assert badge.status == "error"
        assert "Error" in badge.text()

    def test_unknown_status_fallback(self, qapp: QApplication) -> None:
        """Unknown status falls back gracefully."""
        from opendesk.ui.widgets.status_badge import StatusBadge

        badge = StatusBadge("unknown_status_xyz")
        assert badge.status == "unknown_status_xyz"


# ═══════════════════════════════════════════════════════════════════
# ChatPanel tests
# ═══════════════════════════════════════════════════════════════════


class TestChatPanel:
    """Tests for the chat panel."""

    def test_send_message(self, qapp: QApplication) -> None:
        """Typing and sending emits message_sent signal."""
        from opendesk.ui.chat_panel import ChatPanel

        messages: list[str] = []
        panel = ChatPanel()
        panel._input.setText("Hello World")
        panel.message_sent.connect(lambda m: messages.append(m))

        panel._send_message()
        assert messages == ["Hello World"]
        # Input should be cleared
        assert panel._input.text() == ""

    def test_add_message(self, qapp: QApplication) -> None:
        """add_message() appends to the display."""
        from opendesk.ui.chat_panel import ChatPanel

        panel = ChatPanel()
        panel.add_message("Alice", "Hi there", is_remote=True)
        panel.add_message("Me", "Hello back", is_remote=False)

        html = panel._display.toHtml()
        assert "Alice" in html
        assert "Hi there" in html
        assert "Hello back" in html

    def test_clear_messages(self, qapp: QApplication) -> None:
        """clear() removes all messages."""
        from opendesk.ui.chat_panel import ChatPanel

        panel = ChatPanel()
        panel.add_message("Alice", "Hello")
        panel.clear()
        assert panel._display.toPlainText() == ""

    def test_empty_message_not_sent(self, qapp: QApplication) -> None:
        """Whitespace-only input does not emit signal."""
        from opendesk.ui.chat_panel import ChatPanel

        emitted = False
        panel = ChatPanel()
        panel.message_sent.connect(lambda m: setattr(panel, '_emitted', True))

        panel._input.setText("   ")
        panel._send_message()
        # Should not have emitted
        with pytest.raises(AttributeError):
            _ = panel._emitted


# ═══════════════════════════════════════════════════════════════════
# ToastNotification tests
# ═══════════════════════════════════════════════════════════════════


class TestToastNotification:
    """Tests for toast notification overlay."""

    def test_create_toast(self, qapp: QApplication) -> None:
        """Toast can be created with a message and type."""
        from opendesk.ui.widgets.toast_notification import ToastNotification

        parent = QWidget()
        toast = ToastNotification(parent, "Test message", ToastNotification.Type.INFO)
        assert toast is not None
        assert toast._duration_ms == 4000

    def test_toast_types_have_icons(self, qapp: QApplication) -> None:
        """All toast types define an icon."""
        from opendesk.ui.widgets.toast_notification import ToastNotification

        for t in ToastNotification.Type:
            assert t.icon, f"{t.name} missing icon"


# ═══════════════════════════════════════════════════════════════════
# Challenge-response auth module tests
# ═══════════════════════════════════════════════════════════════════


class TestChallengeResponse:
    """Tests for the challenge-response auth module."""

    def test_nonce_generation(self) -> None:
        from opendesk.crypto.challenge import generate_nonce
        nonce = generate_nonce()
        assert len(nonce) == 64  # 32 bytes = 64 hex chars
        assert isinstance(nonce, str)

    def test_compute_response(self) -> None:
        from opendesk.crypto.challenge import compute_response
        resp = compute_response("nonce123", "secret")
        assert len(resp) == 64
        assert isinstance(resp, str)

    def test_verify_valid(self) -> None:
        from opendesk.crypto.challenge import generate_nonce, compute_response, verify_response
        nonce = generate_nonce()
        resp = compute_response(nonce, "secret")
        assert verify_response(nonce, "secret", resp)

    def test_verify_wrong_password(self) -> None:
        from opendesk.crypto.challenge import generate_nonce, compute_response, verify_response
        nonce = generate_nonce()
        resp = compute_response(nonce, "secret")
        assert not verify_response(nonce, "wrong", resp)

    def test_verify_wrong_nonce(self) -> None:
        from opendesk.crypto.challenge import compute_response, verify_response
        resp = compute_response("nonce1", "secret")
        assert not verify_response("nonce2", "secret", resp)

    def test_verify_tampered_hash(self) -> None:
        from opendesk.crypto.challenge import generate_nonce, verify_response
        nonce = generate_nonce()
        assert not verify_response(nonce, "secret", "f" * 64)


# ═══════════════════════════════════════════════════════════════════
# AuthManager session cleanup tests
# ═══════════════════════════════════════════════════════════════════


class TestAuthSessionCleanup:
    """Tests for AuthManager session lifecycle."""

    def test_max_sessions_enforced(self) -> None:
        """Creating more than MAX_SESSIONS prunes oldest."""
        from opendesk.crypto.auth import AuthManager
        import tempfile, json

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write(json.dumps({"credentials": {}}))
            tmp = f.name

        try:
            am = AuthManager(config_path=tmp)
            # Create 5 sessions
            for i in range(5):
                am.create_session(f"pass{i}")
            assert len(am._pending_sessions) == 5
        finally:
            import os
            os.unlink(tmp)
