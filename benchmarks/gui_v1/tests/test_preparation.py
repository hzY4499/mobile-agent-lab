import tempfile
import unittest
from unittest.mock import patch

from benchmarks.gui_v1.common import EnvironmentIssue
from benchmarks.gui_v1.prepare import Phone, Preparation


class ChatPhone:
    """Model the shifted keyboard layout that caused E05-r3 to leave the chat."""
    def __init__(self, keyboard, focus_works=True, hide_after_clear=False):
        self.keyboard = keyboard
        self.focus_works = focus_works
        self.hide_after_clear = hide_after_clear
        self.page = 'chat'
        self.draft = '\n'
        self.taps = 0

    def keyboard_shown(self):
        return self.keyboard

    def tap(self, x, y):
        self.taps += 1
        # With IME open the old coordinate dismisses it; otherwise it focuses.
        self.keyboard = not self.keyboard if self.focus_works else False

    def text(self, value):
        if self.keyboard:
            self.draft = value
        if self.hide_after_clear:
            self.keyboard = False

    def back(self):
        if self.keyboard:
            self.keyboard = False
        else:
            self.page = 'results'


class PreparationTests(unittest.TestCase):
    def test_open_and_closed_keyboard_both_leave_empty_chat(self):
        for initial in (True, False):
            with self.subTest(initial=initial), tempfile.TemporaryDirectory() as tmp:
                phone = ChatPhone(initial)
                Preparation(phone, tmp).clear_chat_draft()
                self.assertEqual(phone.page, 'chat')
                self.assertEqual(phone.draft, '')
                self.assertFalse(phone.keyboard)
                self.assertEqual(phone.taps, 0 if initial else 1)

    def test_keyboard_already_hidden_does_not_navigate_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            phone = ChatPhone(True, hide_after_clear=True)
            Preparation(phone, tmp).clear_chat_draft()
            self.assertEqual(phone.page, 'chat')

    @patch('benchmarks.gui_v1.prepare.time.sleep')
    def test_missing_focus_keeps_draft_and_page(self, _sleep):
        with tempfile.TemporaryDirectory() as tmp:
            phone = ChatPhone(False, focus_works=False)
            with self.assertRaises(EnvironmentIssue):
                Preparation(phone, tmp).clear_chat_draft()
            self.assertEqual(phone.page, 'chat')
            self.assertEqual(phone.draft, '\n')

    def test_unrecognized_ime_state_fails_closed(self):
        phone = Phone('test')
        with patch.object(phone, 'adb', return_value=b'unrecognized output'):
            with self.assertRaises(EnvironmentIssue):
                phone.keyboard_shown()


if __name__ == '__main__':
    unittest.main()
