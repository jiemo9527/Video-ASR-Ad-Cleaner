import unittest

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


if __name__ == '__main__':
    unittest.main()
