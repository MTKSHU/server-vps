import unittest

from .images.policy import image_available_to_user


class ImagePolicyTests(unittest.TestCase):
    def test_member_cannot_use_system_image(self):
        self.assertFalse(
            image_available_to_user(
                {"id": "system/resource-downloader", "owner": "system"},
                {"role": "member", "group_name": "member"},
            )
        )

    def test_member_can_use_regular_image(self):
        self.assertTrue(
            image_available_to_user(
                {"id": "images:ubuntu-24.04", "owner": "admin"},
                {"role": "member", "group_name": "member"},
            )
        )

    def test_admin_can_use_system_image(self):
        self.assertTrue(
            image_available_to_user(
                {"id": "system/resource-downloader", "owner": "system"},
                {"role": "admin", "group_name": "admin"},
            )
        )


if __name__ == "__main__":
    unittest.main()
