import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core_logic import ScannerCore


def mp4_box(box_type, payload):
    return (len(payload) + 8).to_bytes(4, 'big') + box_type.encode('ascii') + payload


class MetadataContainerTests(unittest.TestCase):
    def test_matroska_source_uses_matroska_muxer_when_filename_ends_in_mp4(self):
        core = ScannerCore()

        self.assertEqual(
            core.get_metadata_remux_muxer('matroska,webm', '/media/episode.mp4'),
            'matroska',
        )

    def test_normal_mp4_source_uses_extension_selected_muxer(self):
        core = ScannerCore()

        self.assertIsNone(core.get_metadata_remux_muxer('mov,mp4,m4a,3gp,3g2,mj2', '/media/episode.mp4'))

    def test_metadata_scan_text_includes_all_stream_tag_values(self):
        core = ScannerCore()

        scan_text = core.get_stream_metadata_scan_text({
            'streams': [
                {'tags': {'language': 'und', 'handler_name': 'SoundHandler', 'name': 'DDP 5.1 (xinghanWEB)'}},
            ]
        })

        self.assertEqual(core.find_keywords(scan_text, ['xinghanWEB']), ['xinghanWEB'])

    def test_track_title_removes_only_keyword_segments(self):
        core = ScannerCore()
        self.assertEqual(core.clean_track_title('gyweb..国语', ['GyWEB']), '国语')
        self.assertEqual(core.clean_track_title('双语特效@KKYY', ['KKYY']), '双语特效')
        self.assertEqual(core.clean_track_title('XX资源群 简体', ['资源群']), 'XX 简体')
        self.assertEqual(core.clean_track_title('简体 [TG@abc]', ['TG@abc']), '简体')
        self.assertEqual(core.clean_track_title('English', ['GyWEB']), 'English')
        self.assertEqual(core.clean_track_title('GyWEB', ['GyWEB']), '')
        self.assertEqual(core.clean_track_title('', ['GyWEB']), '')

    def test_track_title_without_keyword_hit_is_untouched(self):
        core = ScannerCore()
        for title in ('English [SDH]', '中文（简体）', 'Português (Brasil)', '???',
                      'English [Dolby Digital Plus 5.1]'):
            self.assertEqual(core.clean_track_title(title, ['GyWEB', '资源群']), title)

    def test_track_title_keeps_balanced_brackets_after_removal(self):
        core = ScannerCore()
        self.assertEqual(core.clean_track_title('GyWEB..English [SDH]', ['GyWEB']), 'English [SDH]')
        self.assertEqual(core.clean_track_title('中文（简体）@KKYY', ['KKYY']), '中文（简体）')

    def test_track_title_handles_zero_width_obfuscation(self):
        core = ScannerCore()
        self.assertEqual(core.clean_track_title('Gy\u200bWEB..国语', ['GyWEB']), '国语')

    def test_stream_metadata_cleanup_clears_mp4_name_tag(self):
        core = ScannerCore()

        clear_args = core.get_stream_metadata_clear_args()

        self.assertIn(['-metadata:s', 'name='], [clear_args[index:index + 2] for index in range(len(clear_args) - 1)])

    def test_mp4_track_udta_name_is_scanned_when_ffprobe_omits_it(self):
        core = ScannerCore()
        track = mp4_box('trak', mp4_box('udta', mp4_box('name', b'DDP 5.1 (xinghanWEB)')))
        content = mp4_box('ftyp', b'isom') + mp4_box('moov', track)
        fd, path = tempfile.mkstemp(suffix='.mp4')
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(content)

            scan_text = core.get_mp4_track_name_metadata_scan_text(path)

            self.assertEqual(core.find_keywords(scan_text, ['xinghanWEB']), ['xinghanWEB'])
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_ffprobe_reports_track_names_detects_name_tag(self):
        core = ScannerCore()

        self.assertTrue(core.ffprobe_reports_track_names({'streams': [{'tags': {'name': 'DDP 5.1'}}]}))
        self.assertFalse(core.ffprobe_reports_track_names({'streams': [{'tags': {'handler_name': 'SoundHandler'}}]}))
        self.assertFalse(core.ffprobe_reports_track_names(None))

    def _run_sanitize_with_probe(self, probe_data):
        core = ScannerCore()
        calls = []
        core.run_cmd = lambda *args, **kwargs: None
        core.get_stream_tags_probe_data = lambda file_path: probe_data
        core.get_container_format_name = lambda file_path: 'mov,mp4,m4a,3gp,3g2,mj2'
        core.get_mp4_track_name_metadata_scan_text = lambda file_path: calls.append(file_path) or ''
        core.sanitize_metadata('/media/episode.mp4', ['xinghanWEB'])
        return calls

    def test_raw_mp4_fallback_skipped_when_ffprobe_reads_track_names(self):
        calls = self._run_sanitize_with_probe({'streams': [{'tags': {'name': 'DDP 5.1'}}]})

        self.assertEqual(calls, [])

    def test_raw_mp4_fallback_used_when_ffprobe_omits_track_names(self):
        calls = self._run_sanitize_with_probe({'streams': [{'tags': {'handler_name': 'SoundHandler'}}]})

        self.assertEqual(calls, ['/media/episode.mp4'])


    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg not installed')
    def test_metadata_cleanup_restores_cleaned_track_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, 'sample.mkv')
            srt = os.path.join(tmp, 'one.srt')
            srt2 = os.path.join(tmp, 'two.srt')
            srt3 = os.path.join(tmp, 'three.srt')
            with open(srt, 'w', encoding='utf-8') as f:
                f.write('1\n00:00:00,000 --> 00:00:01,000\nHello\n')
            with open(srt2, 'w', encoding='utf-8') as f:
                f.write('1\n00:00:00,000 --> 00:00:01,000\nCiao\n')
            with open(srt3, 'w', encoding='utf-8') as f:
                f.write('1\n00:00:00,000 --> 00:00:01,000\n你好\n')
            subprocess.run([
                'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=d=2:s=64x64',
                '-f', 'lavfi', '-i', 'sine=d=2', '-f', 'lavfi', '-i', 'sine=d=2',
                '-i', srt, '-i', srt2, '-i', srt3,
                '-map', '0:v', '-map', '1:a', '-map', '2:a',
                '-map', '3:s', '-map', '4:s', '-map', '5:s',
                '-c:v', 'mpeg4', '-c:a', 'aac', '-c:s', 'copy', '-metadata', 'comment=blocked-marker',
                '-metadata:s:a:0', 'language=eng', '-metadata:s:a:0', 'title=English',
                '-metadata:s:a:1', 'language=chi', '-metadata:s:a:1', 'title=GyWEB..国语',
                '-metadata:s:s:0', 'language=eng', '-metadata:s:s:0', 'title=English',
                '-metadata:s:s:1', 'language=ita', '-metadata:s:s:1', 'title=GyWEB..Italian',
                '-metadata:s:s:2', 'language=chi', '-metadata:s:s:2', 'title=GyWEB',
                '-y', source], check=True)
            core = ScannerCore()
            self.assertTrue(core.sanitize_metadata(source, ['blocked-marker', 'GyWEB']))

            def tags(selector):
                info = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', selector,
                                       '-show_entries', 'stream_tags=language,title', '-of', 'json', source],
                                      capture_output=True, text=True, check=True)
                return [s.get('tags', {}) for s in json.loads(info.stdout)['streams']]

            self.assertEqual(tags('a'), [{'language': 'eng', 'title': 'English'},
                                         {'language': 'chi', 'title': '国语'}])
            self.assertEqual(tags('s'), [{'language': 'eng', 'title': 'English'},
                                         {'language': 'ita', 'title': 'Italian'},
                                         {'language': 'chi'}])

if __name__ == '__main__':
    unittest.main()
