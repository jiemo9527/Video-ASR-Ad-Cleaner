import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core_logic import ScannerCore


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


if __name__ == '__main__':
    unittest.main()
