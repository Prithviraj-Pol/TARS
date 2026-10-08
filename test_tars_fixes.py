import os
import sys
import unittest
import time
import tempfile
import pathlib

# Ensure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from memory.config_manager import (
    TARS_VOICE,
    MALE_VOICES,
    AVAILABLE_VOICES,
    get_voice,
    save_voice
)
from core.execution_guard import ExecutionGuard
from core.app_verifier import (
    is_app_already_open,
    is_folder_open,
    is_file_open,
    normalize_app,
    verify_app_launched,
    AppLaunchGuard
)
from actions.open_app import open_app
from actions.file_controller import create_folder, open_folder, open_file, create_file
from actions.desktop import create_desktop_shortcut


class TestTARSFixes(unittest.TestCase):

    def test_01_permanent_male_voice_configuration(self):
        """Test 8, 9, 10: Permanent Male Voice locked across init, wake, and reconnect."""
        print("\n=== Test 8, 9, 10: Permanent Male Voice Lock ===")
        # 1. Constant defined and is a male voice
        self.assertEqual(TARS_VOICE, "Charon")
        self.assertIn(TARS_VOICE, MALE_VOICES)
        for v in AVAILABLE_VOICES:
            self.assertIn(v, MALE_VOICES, f"Voice {v} should be male!")

        # 2. get_voice() returns locked male voice
        v = get_voice()
        self.assertIn(v, MALE_VOICES)
        print(f"Current configured voice: {v}")

        # 3. Trying to save or load a female voice must be rejected / collapsed to male
        save_voice("Kore")  # Female voice
        reloaded = get_voice()
        self.assertIn(reloaded, MALE_VOICES, "Female voice Kore should NOT be permitted!")
        print(f"Attempted to set 'Kore', voice resolved to: {reloaded}")

        save_voice("Aoede")  # Another female voice
        reloaded2 = get_voice()
        self.assertIn(reloaded2, MALE_VOICES, "Female voice Aoede should NOT be permitted!")

        # Restore canonical locked voice
        save_voice(TARS_VOICE)
        self.assertEqual(get_voice(), TARS_VOICE)
        print("SYS: TARS male voice locked test PASSED.")

    def test_02_idempotent_chrome_open(self):
        """Test 1 & 2: Open Chrome idempotency (APP_ALREADY_OPEN if already running)."""
        print("\n=== Test 1 & 2: Chrome Idempotency ===")
        # We know chrome.exe is running on this machine (or if not, test app verifier)
        is_open, pid, win = is_app_already_open("chrome")
        print(f"Chrome check: open={is_open}, pid={pid}, win={win}")
        if is_open:
            result = open_app("chrome")
            print(f"open_app('chrome') returned: {result}")
            self.assertIn("APP_ALREADY_OPEN", result)
            self.assertTrue(result.startswith("APP_ALREADY_OPEN"))
        else:
            print("Chrome not running, skipping already-open check for chrome")

    def test_03_tool_call_deduplication(self):
        """Test 3: Simulate duplicate tool calls within same request / short cooldown."""
        print("\n=== Test 3: Tool Call Deduplication ===")
        guard = ExecutionGuard(cooldown_seconds=4.0)
        req_id = guard.new_user_request(source="test_user")

        fc_id = "call_abc123"
        tool_name = "open_app"
        tool_args = {"app_name": "chrome"}

        # First call
        is_dup, reason, cached = guard.check_duplicate(fc_id, tool_name, tool_args)
        self.assertFalse(is_dup)
        guard.record_start(tool_name, tool_args)
        guard.record_finish(fc_id, tool_name, tool_args, "SUCCESS: Chrome is already running")

        # Second call with same tool_id (e.g. streaming duplicate)
        is_dup, reason, cached = guard.check_duplicate(fc_id, tool_name, tool_args)
        self.assertTrue(is_dup)
        self.assertEqual(reason, "DUPLICATE_IGNORED")
        self.assertIsNotNone(cached)
        print(f"Duplicate call by ID rejected: {reason}, cached={cached}")

        # Third call with new tool_id but same user request & same action
        is_dup2, reason2, cached2 = guard.check_duplicate("call_xyz456", tool_name, tool_args)
        self.assertTrue(is_dup2)
        self.assertIn("DUPLICATE_IGNORED", reason2)
        print(f"Duplicate call by request action rejected: {reason2}")

        # New user request later (e.g., user says "Open Chrome again")
        time.sleep(0.1)
        req_id2 = guard.new_user_request(source="test_user_again")
        # Should now allow check_duplicate because it is a NEW user request (though cooldown might apply if within 4s)
        # Testing explicitly with force or after cooldown:
        guard_no_cooldown = ExecutionGuard(cooldown_seconds=0.0)
        guard_no_cooldown.new_user_request(source="req1")
        guard_no_cooldown.record_start(tool_name, tool_args)
        guard_no_cooldown.record_finish("1", tool_name, tool_args, "SUCCESS")

        guard_no_cooldown.new_user_request(source="req2_user_spoke_again")
        is_dup3, reason3, _ = guard_no_cooldown.check_duplicate("2", tool_name, tool_args)
        self.assertFalse(is_dup3, "New user request should NOT be blocked if user genuinely asked again!")
        print("Deduplication test PASSED.")

    def test_04_vague_group_launch_prevention(self):
        """Test Requirement 9: 'Open my PC apps' should NOT launch random apps repeatedly."""
        print("\n=== Test Requirement 9: Vague command prevention ===")
        res = open_app("my pc apps")
        self.assertIn("FAILED", res)
        self.assertIn("specify", res.lower())
        print(f"open_app('my pc apps') safely rejected: {res}")

    def test_05_folder_and_file_idempotency(self):
        """Test 6: Create folder / open file idempotency."""
        print("\n=== Test 6: Folder & File Idempotency ===")
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir_name = "tars_test_dir"
            full_path = os.path.join(tmpdir, test_dir_name)

            # 1. Create directory
            res1 = create_folder(tmpdir, test_dir_name)
            self.assertIn("SUCCESS", res1)
            self.assertTrue(os.path.isdir(full_path))

            # 2. Try creating it again -> must be ALREADY_EXISTS
            res2 = create_folder(tmpdir, test_dir_name)
            self.assertIn("ALREADY_EXISTS", res2)
            print(f"Duplicate folder creation rejected: {res2}")

            # 3. Create file
            file_name = "test.txt"
            f_path = os.path.join(tmpdir, file_name)
            res_f1 = create_file(f_path, "Hello TARS")
            self.assertIn("SUCCESS", res_f1)

            # 4. Try creating identical file -> ALREADY_EXISTS
            res_f2 = create_file(f_path, "Hello TARS")
            self.assertIn("ALREADY_EXISTS", res_f2)
            print(f"Duplicate file creation rejected: {res_f2}")

            # 5. Non-existent file open check
            res_no_file = open_file(os.path.join(tmpdir, "nonexistent.pdf"))
            self.assertIn("NOT_FOUND", res_no_file)
            print(f"Non-existent file open rejected: {res_no_file}")

            # 6. File open detection
            file_is_open, win_title = is_file_open("nonexistent.pdf")
            self.assertFalse(file_is_open)

    def test_06_desktop_shortcut_idempotency(self):
        """Test 7: Desktop shortcut idempotency."""
        print("\n=== Test 7: Desktop Shortcut Idempotency ===")
        target = r"C:\Windows\notepad.exe"
        shortcut_name = "TARS_Test_Notepad_Shortcut"
        desktop_dir = os.path.join(os.path.expanduser("~"), "Desktop")
        lnk_path = os.path.join(desktop_dir, f"{shortcut_name}.lnk")

        try:
            # Clean up if existing
            if os.path.exists(lnk_path):
                os.remove(lnk_path)

            # 1. Create shortcut once
            res1 = create_desktop_shortcut(target, shortcut_name)
            print(f"Create shortcut 1: {res1}")
            self.assertIn("SUCCESS", res1)
            self.assertTrue(os.path.exists(lnk_path))

            # 2. Call again -> must return ALREADY_EXISTS
            res2 = create_desktop_shortcut(target, shortcut_name)
            print(f"Create shortcut 2: {res2}")
            self.assertIn("ALREADY_EXISTS", res2)
        finally:
            if os.path.exists(lnk_path):
                try:
                    os.remove(lnk_path)
                except Exception:
                    pass

    def test_07_wake_word_preservation(self):
        """Test 11 & 15: Wake word configuration preservation."""
        print("\n=== Test 11 & 15: Wake word preservation ===")
        from core.wake_word import WAKE_PHRASE, CUSTOM_MODEL_PATH
        print(f"Wake word phrase: {WAKE_PHRASE}")
        self.assertEqual(WAKE_PHRASE, "TARS", "Wake word MUST remain 'TARS'!")
        self.assertTrue(str(CUSTOM_MODEL_PATH).endswith("tars.onnx"))


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromTestCase(TestTARSFixes)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(not result.wasSuccessful())
