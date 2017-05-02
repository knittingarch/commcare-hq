# -*- coding: utf-8 -*-
from django.test import TestCase, override_settings, Client
from django.urls import reverse
from corehq.apps.hqwebapp.models import HashedPasswordLoginAttempt
from corehq.apps.hqwebapp.utils import decode_password, extract_password, verify_password, hash_password
from corehq.apps.domain.models import Domain
from corehq.apps.users.models import WebUser

HASHED_PASSWORD_MAPPING = {
    "sha256$1e2d5bc2hhMjU2JDFlMmQ1Yk1USXpORFUyZjc5MTI3PQ==f79127=": "123456",
    "sha256$8f5008c2hhMjU2JDhmNTAwOFlXSmpNVEl6TFE9PTRhNjBhOT0=4a60a9=": "abc123-",
    "sha256$4bf7cdc2hhMjU2JDRiZjdjZE1USXpRQ01rSlRFeTEzMGM4ZD0=130c8d=": "123@#$%12",
    "sha256$29df66c2hhMjU2JDI5ZGY2NklDRkFJeVFsWGlZcUtDbGZLeTFjYTQwN2VkPQ==a407ed=": " !@#$%^&*()_+-\\",
    "sha256$ad5e3ac2hhMjU2JGFkNWUzYTRLU0o0S1NxNEtTVjRLU3c0S1NqTVRJejQyNDgyOT0=424829=": u"उपकरण123"
}


class TestDecodePassword(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.domain = Domain(name="delhi", is_active=True)
        cls.domain.save()
        cls.username = "username@test.com"
        cls.web_user = WebUser.create(cls.domain.name, cls.username, "123456")

    @classmethod
    def tearDownClass(cls):
        cls.domain.delete()
        cls.web_user.delete()

    def test_login_attempt(self):
        with override_settings(ENABLE_PASSWORD_HASHING=True):
            password_hash = "sha256$1e2d5bc2hhMjU2JDFlMmQ1Yk1USXpORFUyZjc5MTI3PQ==f79127="
            HashedPasswordLoginAttempt.objects.all().delete()
            client = Client(enforce_csrf_checks=False)
            form_data = {
                'auth-username': self.username,
                'auth-password': password_hash,
                'hq_login_view-current_step': 'auth'
            }
            response = client.post(reverse('login'), form_data, follow=True)
            self.assertRedirects(response, '/domain/select/')
            # ensure that login attempt was stored
            self.assertTrue(
                verify_password(
                    password_hash,
                    HashedPasswordLoginAttempt.objects.filter(username=self.username).all()[0].password_hash
                )
            )
            client.get(reverse('logout'))

            # test replay attack
            response = client.post(reverse('login'), form_data, follow=True)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.request['PATH_INFO'], '/accounts/login/')


class TestExtractPassword(TestCase):
    def test_password_decoding(self):
        for password_hash, password in HASHED_PASSWORD_MAPPING.items():
            self.assertEqual(extract_password(password_hash), password)

    def test_invalid_regex_format(self):
        password_hash = "sha255$1e2d5bc2hhMjU2JDFlMmQ1Yk1USXpORFUyZjc5MTI3PQ==f79127="
        self.assertEqual(extract_password(password_hash), password_hash)

        password_hash = "sha255$1e2d5bc2hhMjU2JDFlMmQ1Yk1USXpORFUyZjc5MTI3PQ=="
        self.assertEqual(extract_password(password_hash), password_hash)

    def test_invalid_padding(self):
        password_hash = "sha256$1e456bc2hhMjU2JDFlMmQ1Yk1USXpORFUyZjc5MTI3PQ==f79127="
        self.assertEqual(extract_password(password_hash), '')