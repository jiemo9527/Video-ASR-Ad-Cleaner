import threading
import unittest

from core_logic import ScannerCore


def reset_slot_state():
    ScannerCore._cloud_asr_active_total = 0
    ScannerCore._cloud_asr_active_by_key = {}
    ScannerCore._cloud_asr_next_key = 0
    ScannerCore._cloud_asr_session_seq = 0
    ScannerCore._cloud_asr_session_order = {}
    ScannerCore._cloud_asr_session_waiting = {}
    ScannerCore._cloud_asr_session_active = {}


class CloudAsrSlotTests(unittest.TestCase):
    def setUp(self):
        reset_slot_state()
        self.addCleanup(reset_slot_state)

    def test_per_key_concurrency_defaults_to_three(self):
        core = ScannerCore()

        self.assertEqual(core.get_cloud_asr_per_key_concurrency({}), 3)
        self.assertEqual(core.get_cloud_asr_per_key_concurrency({'cloud_asr_per_key_concurrency': 5}), 5)
        self.assertEqual(core.get_cloud_asr_per_key_concurrency({'cloud_asr_per_key_concurrency': 0}), 1)
        self.assertEqual(core.get_cloud_asr_per_key_concurrency({'cloud_asr_per_key_concurrency': 'x'}), 3)

    def test_effective_limit_degrades_to_key_pool_capacity(self):
        core = ScannerCore()
        config = {'cloud_asr_concurrency': 8, 'cloud_asr_per_key_concurrency': 3}

        global_limit, per_key, effective = core.resolve_cloud_asr_limits(config, ['k1'])
        self.assertEqual((global_limit, per_key, effective), (8, 3, 3))

        global_limit, per_key, effective = core.resolve_cloud_asr_limits(config, ['k1', 'k2'])
        self.assertEqual((global_limit, per_key, effective), (8, 3, 6))

    def test_effective_limit_keeps_global_when_capacity_is_enough(self):
        core = ScannerCore()
        config = {'cloud_asr_concurrency': 4, 'cloud_asr_per_key_concurrency': 3}

        global_limit, per_key, effective = core.resolve_cloud_asr_limits(config, ['k1', 'k2', 'k3'])
        self.assertEqual((global_limit, per_key, effective), (4, 3, 4))

    def test_single_key_never_exceeds_per_key_limit(self):
        core = ScannerCore()
        keys = ['k1']

        acquired = [core.acquire_cloud_asr_slot(keys, 3, 3) for _ in range(3)]

        self.assertEqual(acquired, ['k1', 'k1', 'k1'])
        self.assertEqual(ScannerCore._cloud_asr_active_by_key['k1'], 3)
        self.assertEqual(ScannerCore._cloud_asr_active_total, 3)

    def test_fourth_request_on_one_key_blocks_until_release(self):
        core = ScannerCore()
        keys = ['k1']
        for _ in range(3):
            core.acquire_cloud_asr_slot(keys, 6, 3)

        blocked = ScannerCore()
        result = {}

        def worker():
            result['key'] = blocked.acquire_cloud_asr_slot(keys, 6, 3)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout=2)
        self.assertTrue(thread.is_alive(), "4th request must wait while the only key is saturated")
        self.assertEqual(ScannerCore._cloud_asr_active_by_key['k1'], 3)

        core.release_cloud_asr_slot('k1')
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result.get('key'), 'k1')
        self.assertEqual(ScannerCore._cloud_asr_active_by_key['k1'], 3)

    def test_slots_spread_across_keys_before_saturating_one(self):
        core = ScannerCore()
        keys = ['k1', 'k2']

        for _ in range(4):
            core.acquire_cloud_asr_slot(keys, 4, 2)

        self.assertEqual(ScannerCore._cloud_asr_active_by_key, {'k1': 2, 'k2': 2})

    def test_release_decrements_per_key_counter(self):
        core = ScannerCore()
        keys = ['k1', 'k2']
        first = core.acquire_cloud_asr_slot(keys, 4, 2)
        core.acquire_cloud_asr_slot(keys, 4, 2)

        core.release_cloud_asr_slot(first)

        self.assertNotIn(first, ScannerCore._cloud_asr_active_by_key)
        self.assertEqual(ScannerCore._cloud_asr_active_total, 1)


if __name__ == '__main__':
    unittest.main()
