import json
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

    def test_srt_removes_entire_hit_cues_and_keeps_other_times(self):
        cleaned, removed = self.core.clean_subtitle_content(SRT, 'srt', KEYWORDS)

        self.assertEqual(removed, 3)
        self.assertNotIn('第二行正常', cleaned)
        self.assertNotIn('00:00:02,000 --> 00:00:04,000', cleaned)
        self.assertNotIn('00:00:04,000 --> 00:00:06,000', cleaned)
        self.assertIn('正常台词一', cleaned)
        self.assertIn('正常台词四', cleaned)
        self.assertEqual(self.core.find_keywords(self.core.subtitle_dialogue_text(cleaned, 'srt'), KEYWORDS), [])

    def test_vtt_keeps_header(self):
        vtt = "WEBVTT\n\n" + SRT.split('\n\n', 1)[1].replace(',', '.')
        cleaned, removed = self.core.clean_subtitle_content(vtt, 'webvtt', KEYWORDS)

        self.assertEqual(removed, 3)
        self.assertTrue(cleaned.startswith('WEBVTT\n'))
        self.assertNotIn('第二行正常', cleaned)
        self.assertIn('正常台词四', cleaned)

    def test_ass_matches_through_override_tags_and_removes_entire_event(self):
        cleaned, removed = self.core.clean_subtitle_content(ASS, 'ass', KEYWORDS)

        self.assertEqual(removed, 3)
        self.assertIn('Dialogue: 0,0:00:00.50,0:00:02.00,Default,,0,0,0,,你好, 世界', cleaned)
        self.assertNotIn('保留这一行', cleaned)
        self.assertNotIn('全是', cleaned)
        self.assertIn('Format: Layer, Start, End', cleaned)

    def test_keyword_split_across_lines_in_same_event_is_removed(self):
        bs = chr(92)
        ass = ('[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
               'Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,加入资源' + bs + 'N群123\n'
               'Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,正常台词\n')
        self.assertEqual(self.core.find_subtitle_content_keywords(ass, 'ass', ['资源群']), ['资源群'])
        cleaned, removed = self.core.clean_subtitle_content(ass, 'ass', ['资源群'])
        self.assertEqual(removed, 2)
        self.assertNotIn('加入资源', cleaned)
        self.assertIn('正常台词', cleaned)

        srt = '1\n00:00:01,000 --> 00:00:02,000\n更多link3\n.cc/vip\n\n2\n00:00:03,000 --> 00:00:04,000\n正常\n'
        self.assertEqual(self.core.find_subtitle_content_keywords(srt, 'srt', ['link3.cc']), ['link3.cc'])
        cleaned, removed = self.core.clean_subtitle_content(srt, 'srt', ['link3.cc'])
        self.assertEqual(removed, 2)
        self.assertIn('正常', cleaned)

    def test_keyword_split_across_different_events_is_not_joined(self):
        srt = '1\n00:00:01,000 --> 00:00:02,000\n加入资源\n\n2\n00:00:02,000 --> 00:00:03,000\n群123\n'
        self.assertEqual(self.core.find_subtitle_content_keywords(srt, 'srt', ['资源群']), [])
        cleaned, removed = self.core.clean_subtitle_content(srt, 'srt', ['资源群'])
        self.assertEqual(removed, 0)

    def test_real_sample_removes_whole_ad_event(self):
        sample = os.path.join(os.path.dirname(__file__), 'fixtures', 'sample_37_ad.ass')
        with open(sample, encoding='utf-8') as f:
            content = f.read()
        cleaned, removed = self.core.clean_subtitle_content(content, 'ass', ['link3.cc'])
        self.assertEqual(removed, 3)
        self.assertNotIn('永裴资源君', cleaned)
        self.assertNotIn('更多实时同步更新优品资源', cleaned)
        self.assertNotIn('link3.cc/vip666888', cleaned)
        self.assertIn('[Events]', cleaned)

    def test_ass_keeps_unrelated_event_in_same_track(self):
        sample = os.path.join(os.path.dirname(__file__), 'fixtures', 'sample_37_ad.ass')
        with open(sample, encoding='utf-8') as f:
            content = f.read()
        normal = 'Dialogue: 0,0:00:31.00,0:00:32.00,Default,,0,0,0,,正常台词'
        cleaned, removed = self.core.clean_subtitle_content(content + normal + '\n', 'ass', ['link3.cc'])
        self.assertEqual(removed, 3)
        self.assertNotIn('永裴资源君', cleaned)
        self.assertIn(normal, cleaned)


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
                '-metadata:s:s:0', 'title=Chinese Simplified', '-y', source], check=True)

            output = ScannerCore().check_subtitles(source, KEYWORDS)

            self.assertTrue(output and os.path.exists(output))
            assert output is not None
            probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 's', '-show_entries',
                                    'stream_tags=language,title', '-of', 'json', output],
                                   capture_output=True, text=True, check=True)
            tags = json.loads(probe.stdout)['streams'][0]['tags']
            self.assertEqual(tags['language'], 'chi')
            self.assertEqual(tags['title'], 'Chinese Simplified')
            text = subprocess.run(['ffmpeg', '-v', 'error', '-i', output, '-map', '0:s:0', '-f', 'srt', '-'],
                                  capture_output=True, text=True).stdout
            self.assertNotIn('第二行正常', text)
            self.assertIn('正常台词一', text)
            self.assertIn('正常台词四', text)
            self.assertNotIn('QQ', text)

    def test_subtitle_title_keyword_keeps_track_and_language_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            srt = os.path.join(tmp, 'a.srt')
            with open(srt, 'w', encoding='utf-8') as f:
                f.write('1\n00:00:00,000 --> 00:00:01,000\n正常台词\n')
            source = os.path.join(tmp, 'v.mkv')
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=d=2:s=64x64',
                            '-i', srt, '-map', '0:v', '-map', '1:s', '-c:v', 'mpeg4',
                            '-c:s', 'copy', '-metadata:s:s:0', 'language=chi',
                            '-metadata:s:s:0', 'title=GyWEB..国语', '-y', source], check=True)
            core = ScannerCore()
            self.assertTrue(core.sanitize_metadata(source, ['GyWEB']))
            output = core.check_subtitles(source, ['GyWEB'])
            self.assertIsNone(output)  # No subtitle dialogue matched; keep the track.
            probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 's',
                                    '-show_entries', 'stream_tags=language,title', '-of', 'json', source],
                                   capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(probe.stdout)['streams'][0]['tags'],
                             {'language': 'chi', 'title': '国语'})

    def test_ad_only_track_logs_clear_reason(self):
        sample = os.path.join(os.path.dirname(__file__), 'fixtures', 'sample_37_ad.ass')
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, 'v.mkv')
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=d=2:s=64x64',
                            '-i', sample, '-map', '0:v', '-map', '1:s', '-c:v', 'mpeg4',
                            '-c:s', 'copy', '-y', source], check=True)
            logs = []
            ScannerCore(logger_callback=logs.append).check_subtitles(source, ['link3.cc'])
            joined = '\n'.join(logs)
            self.assertIn('仅含广告（1 条事件全部命中），整轨剔除', joined)
            self.assertNotIn('无法按事件清理', joined)

    def test_editing_one_track_keeps_labels_of_all_tracks(self):
        with tempfile.TemporaryDirectory() as tmp:
            clean = os.path.join(tmp, 'clean.srt')
            dirty = os.path.join(tmp, 'dirty.srt')
            with open(clean, 'w', encoding='utf-8') as f:
                f.write('1\n00:00:00,000 --> 00:00:01,000\nHello\n')
            with open(dirty, 'w', encoding='utf-8') as f:
                f.write('1\n00:00:00,000 --> 00:00:01,000\nvisit x.com\n\n'
                        '2\n00:00:01,000 --> 00:00:02,000\nok\n')
            source = os.path.join(tmp, 'v.mkv')
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=d=2:s=64x64',
                            '-f', 'lavfi', '-i', 'sine=d=2', '-i', clean, '-i', dirty, '-i', clean,
                            '-map', '0:v', '-map', '1:a', '-map', '2:s', '-map', '3:s', '-map', '4:s',
                            '-c:v', 'mpeg4', '-c:a', 'aac', '-c:s', 'copy',
                            '-metadata:s:a:0', 'language=eng', '-metadata:s:a:0', 'title=English',
                            '-metadata:s:s:0', 'language=ara', '-metadata:s:s:0', 'title=Arabic',
                            '-metadata:s:s:1', 'language=dut', '-metadata:s:s:1', 'title=Dutch',
                            '-metadata:s:s:2', 'language=fin', '-metadata:s:s:2', 'title=Finnish',
                            '-disposition:s:2', 'default', '-y', source], check=True)
            output = ScannerCore().check_subtitles(source, ['.com'])
            self.assertTrue(output and os.path.isfile(output))
            assert output is not None
            probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                                    'stream=codec_type:stream_tags=language,title:stream_disposition=default',
                                    '-of', 'json', output], capture_output=True, text=True, check=True)
            streams = [s for s in json.loads(probe.stdout)['streams'] if s['codec_type'] != 'video']
            labels = [(s['codec_type'], s.get('tags', {}).get('language'), s.get('tags', {}).get('title'))
                      for s in streams]
            self.assertEqual(labels, [('audio', 'eng', 'English'), ('subtitle', 'ara', 'Arabic'),
                                      ('subtitle', 'dut', 'Dutch'), ('subtitle', 'fin', 'Finnish')])
            self.assertEqual(streams[3]['disposition']['default'], 1)
            text = subprocess.run(['ffmpeg', '-v', 'error', '-i', output, '-map', '0:s:1', '-f', 'srt', '-'],
                                  capture_output=True, text=True).stdout
            self.assertNotIn('x.com', text)
            self.assertIn('ok', text)

    def test_handler_name_keyword_alone_does_not_remove_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            srt = os.path.join(tmp, 'a.srt')
            with open(srt, 'w', encoding='utf-8') as f:
                f.write('1\n00:00:00,000 --> 00:00:01,000\n正常台词\n')
            source = os.path.join(tmp, 'v.mkv')
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=d=2:s=64x64',
                            '-i', srt, '-map', '0:v', '-map', '1:s', '-c:v', 'mpeg4',
                            '-c:s', 'copy', '-metadata:s:s:0', 'language=chi',
                            '-y', source], check=True)
            core = ScannerCore()
            original = core.get_subtitle_streams
            def with_handler(file_path):
                streams = original(file_path)
                assert streams is not None
                for stream in streams:
                    stream['handler_name'] = 'NewLanguage'
                return streams
            core.get_subtitle_streams = with_handler
            self.assertIsNone(core.check_subtitles(source, ['NewLanguage']))
            self.assertTrue(os.path.isfile(source))

    def test_sample_with_only_ad_event_removes_subtitle_track(self):
        sample = os.environ.get('SCANNER_SUBTITLE_AD_SAMPLE', '')
        if not sample or not os.path.isfile(sample):
            self.skipTest('set SCANNER_SUBTITLE_AD_SAMPLE to a local sample video')
        with tempfile.TemporaryDirectory() as tmp:
            clip = os.path.join(tmp, 'sample.mkv')
            subprocess.run(['ffmpeg', '-v', 'error', '-i', sample, '-t', '35',
                            '-map', '0:v:0', '-map', '0:a:0', '-map', '0:s:0',
                            '-c', 'copy', '-y', clip], check=True)
            output = ScannerCore().check_subtitles(clip, ['link3.cc'])
            self.assertTrue(output and os.path.isfile(output))
            assert output is not None
            probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 's',
                                    '-show_entries', 'stream=index', '-of', 'csv=p=0', output],
                                   capture_output=True, text=True, check=True)
            self.assertEqual(probe.stdout.strip(), '')
            self.assertTrue(os.path.isfile(sample))


if __name__ == '__main__':
    unittest.main()
