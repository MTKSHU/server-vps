import unittest

from .auth_policy import password_change_enabled


class AuthPolicyTests(unittest.TestCase):
    def test_oidc_disables_local_password_changes(self):
        self.assertFalse(password_change_enabled({"sso_provider_type": "oidc"}, True))

    def test_disabled_or_non_oidc_sso_keeps_local_password_changes(self):
        self.assertTrue(password_change_enabled({"sso_provider_type": "oidc"}, False))
        self.assertTrue(password_change_enabled({"sso_provider_type": "cas"}, True))


if __name__ == "__main__":
    unittest.main()
