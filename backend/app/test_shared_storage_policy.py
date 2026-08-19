import unittest

from .platform_settings import effective_node_shared_storage_mode


class SharedStoragePolicyTests(unittest.TestCase):
    def test_node_override_wins_over_global_mode(self):
        global_enabled = {"shared_storage_mode": "enabled", "shared_storage_canary_user_ids": []}
        global_disabled = {"shared_storage_mode": "disabled", "shared_storage_canary_user_ids": []}
        self.assertEqual(
            effective_node_shared_storage_mode({"shared_storage_mode": "disabled"}, global_enabled, 7),
            "disabled",
        )
        self.assertEqual(
            effective_node_shared_storage_mode({"shared_storage_mode": "enabled"}, global_disabled, 7),
            "enabled",
        )

    def test_inherit_honors_canary_user_scope(self):
        settings = {"shared_storage_mode": "canary", "shared_storage_canary_user_ids": [7]}
        node = {"shared_storage_mode": "inherit"}
        self.assertEqual(effective_node_shared_storage_mode(node, settings, 7), "enabled")
        self.assertEqual(effective_node_shared_storage_mode(node, settings, 8), "disabled")

    def test_missing_node_value_is_backward_compatible(self):
        settings = {"shared_storage_mode": "disabled", "shared_storage_canary_user_ids": []}
        self.assertEqual(effective_node_shared_storage_mode({}, settings, 7), "disabled")


if __name__ == "__main__":
    unittest.main()
