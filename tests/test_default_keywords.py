"""Default keyword lists shipped with the project."""
import ast
import os
import unittest

APP_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app.py')


def load_list(name):
    with open(APP_PY, encoding='utf-8') as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f'{name} not found')


class DefaultKeywordTests(unittest.TestCase):
    def test_subtitle_defaults_include_resource_credit(self):
        self.assertIn('资源君', load_list('SUBTITLE_BLACKLIST_INIT'))

    def test_meta_defaults_do_not_include_language_descriptor_mandarin(self):
        self.assertNotIn('Mandarin', load_list('SUB_META_BLACKLIST_INIT'))


if __name__ == '__main__':
    unittest.main()
