import importlib.util
import unittest
from pathlib import Path


def load_vv_box():
    module_path = Path("vv-speaker-box-logic/scripts/vv_box.py")
    spec = importlib.util.spec_from_file_location("vv_box", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeVoicevoxClient:
    def __init__(self):
        self.calls = 0

    def resolve_speaker_id(self, speaker_name: str) -> int:
        self.calls += 1
        if speaker_name == "冥鳴ひまり":
            return 14
        if speaker_name == "四国めたん":
            return 2
        raise RuntimeError("not found")


class VVBoxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vv = load_vv_box()

    def test_normalize_direct_text_keeps_short_text(self):
        text = "こんにちは"
        out = self.vv.normalize_direct_text(text)
        self.assertEqual(out, "こんにちは。")

    def test_normalize_text_rejects_short_text(self):
        out = self.vv.normalize_text("こんにちは", 80, 160)
        self.assertIsNone(out)

    def test_resolve_speaker_id_with_cache(self):
        cfg = self.vv.Config(
            voicevox_url="http://dummy:50021",
            speaker_name="冥鳴ひまり",
            llm_timeout_sec=15,
            min_chars=80,
            max_chars=160,
            queue_max=10,
            lock_path=Path("/tmp/vv-speaker-test.lock"),
            llm_command="gemini -p",
            player_command="",
            stream_playback=True,
        )
        logic = self.vv.BoxLogic(cfg)
        fake_client = FakeVoicevoxClient()
        logic.client = fake_client

        self.assertEqual(logic.resolve_speaker_id("冥鳴ひまり"), 14)
        self.assertEqual(logic.resolve_speaker_id("冥鳴ひまり"), 14)
        self.assertEqual(fake_client.calls, 1)


if __name__ == "__main__":
    unittest.main()
