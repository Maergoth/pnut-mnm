"""Captured shares prompt for review without changing timers or taking focus."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from mnmparse.app.main import App, MainWindow
from mnmparse.triggers import Trigger
from mnmparse.trigger_chat import ChatShareAssembler, encode_trigger


class ChatShareWiringTests(unittest.TestCase):
    def setUp(self):
        self.page = Mock()
        self.window = Mock()
        self.window.page.return_value = self.page
        self.host = SimpleNamespace(window=self.window, tray=Mock(), triggers=Mock(),
                                    _chat_shares=ChatShareAssembler())
        self.trigger = Trigger(name="Shared pacify", pattern="begins casting Pacify", timer=True,
                               timer_seconds=110, timer_mode="retain", timer_color="#123456")

    def test_captured_chat_offers_one_definition_without_importing(self):
        code = encode_trigger(self.trigger)
        msg = SimpleNamespace(text=f'Alice says, "{code}"', backlog=False)
        App._on_chat_share(self.host, msg, SimpleNamespace(kind="chat", actor="Alice"))
        self.page.offer_chat_share.assert_called_once()
        share = self.page.offer_chat_share.call_args.args[0]
        self.assertEqual(share.trigger.to_dict(), self.trigger.to_dict())
        self.assertEqual(share.sender, "Alice")
        self.host.triggers.save.assert_not_called()
        self.window.activateWindow.assert_not_called()
        App._on_chat_share(self.host, SimpleNamespace(text=code), SimpleNamespace(kind="chat", actor="Alice"))
        self.page.offer_chat_share.assert_called_once()

    def test_backlog_and_incomplete_shares_do_not_prompt(self):
        App._on_chat_share(self.host, SimpleNamespace(text=encode_trigger(self.trigger), backlog=True), None)
        self.page.offer_chat_share.assert_not_called()
        App._on_chat_share(self.host, SimpleNamespace(text="PNUT1 incomplete"), None)
        self.page.offer_chat_share.assert_not_called()

    def test_full_review_queue_allows_a_later_resend(self):
        code = encode_trigger(self.trigger)
        self.page.offer_chat_share.return_value = False
        App._on_chat_share(self.host, SimpleNamespace(text=code), None)
        self.page.offer_chat_share.return_value = True
        App._on_chat_share(self.host, SimpleNamespace(text=code), None)
        self.assertEqual(self.page.offer_chat_share.call_count, 2)
        App._on_chat_share(self.host, SimpleNamespace(text=code), None)
        self.assertEqual(self.page.offer_chat_share.call_count, 2)

    def test_pending_notice_stays_passive_and_clears(self):
        self.window.isVisible.return_value = False
        App._on_chat_share_pending(self.host, "Shared timer ready")
        self.window.set_timer_share_notice.assert_called_with("Shared timer ready")
        self.host.tray.notify.assert_called_once()
        self.window.show.assert_not_called()
        self.window.activateWindow.assert_not_called()
        App._on_chat_share_pending(self.host, "")
        self.window.set_timer_share_notice.assert_called_with("")
        self.assertEqual(self.host.tray.notify.call_count, 1)

    def test_review_button_opens_the_trigger_review_only_on_request(self):
        MainWindow._review_shared_timer(self.window)
        self.window.show_page.assert_called_once_with("triggers")
        self.page.review_chat_shares.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
