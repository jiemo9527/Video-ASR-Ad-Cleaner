import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core_logic import ScannerCore

KEYWORDS = ['QQ', 'q群']

SRT = """1
00:00:00,500 --> 00:00:02,000
正常台词一

2
00:00:02,000 --> 00:00:04,000
加Q​Q群 123456
第二行正常

3
00:00:04,000 --> 00:00:06,000
更多资源q群

4
00:00:06,000 --> 00:00:08,000
正常台词四
"""

ASS = """[Script Info]
ScriptType: v4.00+

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.50,0:00:02.00,Default,,0,0,0,,你好, 世界
Dialogue: 0,0:00:02.00,0:00:04.00,Default,,0,0,0,,{\\i1}Q{\\b1}Q{\\i0}群 88\\N保留这一行
Dialogue: 0,0:00:04.00,0:00:06.00,Default,,0,0,0,,全是 q群
"""


class SubtitleLineCleaningTests(unittest.TestCase):
    def setUp(self):
        self.core = ScannerCore()

    def test_srt_removes_hit_lines_and_empty_cues(self):
        cleaned, removed = self.core.clean_subtitle_content(SRT, 'srt', KEYWORDS)

        self.assertEqual(removed, 2)
        self.assertIn('第二行正常', cleaned)
        self.assertIn('正常台词四', cleaned)
        self.assertNotIn('00:00:04,000 --> 00:00:06,000', cleaned)
        self.assertEqual(self.core.find_keywords(self.core.subtitle_dialogue_text(cleaned, 'srt'), KEYWORDS), [])

    def test_vtt_keeps_header(self):
        vtt = "WEBVTT\n\n" + SRT.split('\n\n', 1)[1].replace(',', '.')
        cleaned, removed = self.core.clean_subtitle_content(vtt, 'webvtt', KEYWORDS)

        self.assertEqual(removed, 2)
        self.assertTrue(cleaned.startswith('WEBVTT\n'))
        self.assertIn('第二行正常', cleaned)

    def test_ass_matches_through_override_tags_and_keeps_other_lines(self):
        cleaned, removed = self.core.clean_subtitle_content(ASS, 'ass', KEYWORDS)

        self.assertEqual(removed, 2)
        self.assertIn('Dialogue: 0,0:00:00.50,0:00:02.00,Default,,0,0,0,,你好, 世界', cleaned)
        self.assertIn('Dialogue: 0,0:00:02.00,0:00:04.00,Default,,0,0,0,,保留这一行', cleaned)
        self.assertNotIn('全是', cleaned)
        self.assertIn('Format: Layer, Start, End', cleaned)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg not installed')
class SubtitleTrackRemuxTests(unittest.TestCase):
    def test_hit_text_track_is_modified_not_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            srt = os.path.join(tmp, 'a.srt')
            with open(srt, 'w', encoding='utf-8') as f:
                f.write(SRT)
            source = os.path.join(tmp, 'v.mkv')
            subprocess.run([
                'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=d=8:s=160x120:r=25', '-f', 'lavfi',
                '-i', 'sine=d=8', '-i', srt, '-map', '0', '-map', '1', '-map', '2', '-c:v', 'libx264',
                '-preset', 'ultrafast', '-c:a', 'aac', '-c:s', 'copy', '-metadata:s:s:0', 'language=chi',
                '-y', source], check=True)

            output = ScannerCore().check_subtitles(source, KEYWORDS)

            self.assertTrue(output and os.path.exists(output))
            probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 's', '-show_entries',
                                    'stream_tags=language', '-of', 'csv=p=0', output],
                                   capture_output=True, text=True).stdout.split()
            self.assertEqual(probe, ['chi'])
            text = subprocess.run(['ffmpeg', '-v', 'error', '-i', output, '-map', '0:s:0', '-f', 'srt', '-'],
                                  capture_output=True, text=True).stdout
            self.assertIn('第二行正常', text)
            self.assertNotIn('QQ', text)


if __name__ == '__main__':
    unittest.main()
