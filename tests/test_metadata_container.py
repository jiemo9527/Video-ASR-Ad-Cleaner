import os
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


if __name__ == '__main__':
    unittest.main()
