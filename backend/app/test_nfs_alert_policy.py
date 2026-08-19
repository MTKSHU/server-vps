import unittest

from .metrics.routes import _nfs_unavailable


class NFSAlertPolicyTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"shared_storage_mode": "enabled", "shared_storage_canary_user_ids": []}
        self.node = {
            "id": 1,
            "status": "online",
            "node_type": "compute",
            "shared_storage_mode": "enabled",
            "nfs_healthy": False,
            "nfs_error": "managed NFS is not mounted",
        }

    def test_unused_nfs_does_not_alert_for_legacy_agent(self):
        self.assertFalse(_nfs_unavailable(self.node, self.settings, False))

    def test_same_report_alerts_when_node_has_managed_mounts(self):
        self.assertTrue(_nfs_unavailable(self.node, self.settings, True))

    def test_concrete_mount_failure_alerts(self):
        self.node["nfs_error"] = "mount NFS: connection timed out"
        self.assertTrue(_nfs_unavailable(self.node, self.settings, False))


if __name__ == "__main__":
    unittest.main()
