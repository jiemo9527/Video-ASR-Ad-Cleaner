import unittest

from core_logic import deduplicate_keyword_values, normalize_keyword_identity


class KeywordRulesTests(unittest.TestCase):
    def test_keyword_identity_ignores_case_and_zero_width_characters(self):
        self.assertEqual(normalize_keyword_identity('Telegram'), 'telegram')
        self.assertEqual(normalize_keyword_identity('Te\u200blegram'), 'telegram')
        self.assertEqual(normalize_keyword_identity('  TELEGRAM  '), 'telegram')

    def test_keyword_identity_keeps_types_independent(self):
        self.assertEqual(normalize_keyword_identity('推广'), '推广')
        self.assertEqual(normalize_keyword_identity('推广'), '推广')

    def test_deduplicate_keyword_values_keeps_first_value(self):
        values, skipped = deduplicate_keyword_values([
            'Telegram', 'telegram', 'Te\u200blegram', '加群', '加群', '  '
        ])

        self.assertEqual(values, ['Telegram', '加群'])
        self.assertEqual(skipped, ['telegram', 'Te\u200blegram', '加群'])


if __name__ == '__main__':
    unittest.main()
