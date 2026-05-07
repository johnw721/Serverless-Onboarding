"""
Unit tests for helpers.py — validate_employee_data and sanitize_dn_value.
Run from the project root: python -m pytest tests/
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lambda-package'))

from helpers import validate_employee_data, sanitize_dn_value


class TestValidateEmployeeData(unittest.TestCase):

    def _valid(self):
        return {
            "username": "jdoe",
            "name": "John Doe",
            "Role": "Software Engineer",
            "Department": "Engineering",
        }

    def test_valid_data_passes(self):
        validate_employee_data(self._valid())  # should not raise

    def test_missing_username_raises(self):
        data = self._valid()
        del data["username"]
        with self.assertRaises(ValueError) as ctx:
            validate_employee_data(data)
        self.assertIn("username", str(ctx.exception))

    def test_missing_role_raises(self):
        data = self._valid()
        del data["Role"]
        with self.assertRaises(ValueError) as ctx:
            validate_employee_data(data)
        self.assertIn("Role", str(ctx.exception))

    def test_null_field_treated_as_missing(self):
        data = self._valid()
        data["name"] = None
        with self.assertRaises(ValueError) as ctx:
            validate_employee_data(data)
        self.assertIn("name", str(ctx.exception))

    def test_empty_string_treated_as_missing(self):
        data = self._valid()
        data["username"] = ""
        with self.assertRaises(ValueError) as ctx:
            validate_employee_data(data)
        self.assertIn("username", str(ctx.exception))

    def test_novel_role_not_in_map_still_passes(self):
        data = self._valid()
        data["Role"] = "Chief Vibes Officer"
        validate_employee_data(data)  # should not raise — Claude handles novel roles

    def test_multiple_missing_fields_reported(self):
        with self.assertRaises(ValueError) as ctx:
            validate_employee_data({})
        msg = str(ctx.exception)
        for field in ("username", "name", "Role", "Department"):
            self.assertIn(field, msg)


class TestSanitizeDnValue(unittest.TestCase):

    def test_simple_username_passes(self):
        self.assertEqual(sanitize_dn_value("jdoe"), "jdoe")

    def test_username_with_dot_passes(self):
        self.assertEqual(sanitize_dn_value("john.doe"), "john.doe")

    def test_username_with_hyphen_and_underscore_passes(self):
        self.assertEqual(sanitize_dn_value("j-doe_2"), "j-doe_2")

    def test_mixed_case_passes(self):
        self.assertEqual(sanitize_dn_value("JDoe"), "JDoe")

    def test_comma_injection_raises(self):
        with self.assertRaises(ValueError):
            sanitize_dn_value("jdoe,CN=Admins")

    def test_plus_injection_raises(self):
        with self.assertRaises(ValueError):
            sanitize_dn_value("jdoe+admin")

    def test_semicolon_raises(self):
        with self.assertRaises(ValueError):
            sanitize_dn_value("jdoe;drop")

    def test_angle_brackets_raise(self):
        with self.assertRaises(ValueError):
            sanitize_dn_value("jdoe<script>")

    def test_quote_raises(self):
        with self.assertRaises(ValueError):
            sanitize_dn_value('jdoe"quote')

    def test_backslash_raises(self):
        with self.assertRaises(ValueError):
            sanitize_dn_value("jdoe\\admin")

    def test_space_raises(self):
        with self.assertRaises(ValueError):
            sanitize_dn_value("john doe")


if __name__ == "__main__":
    unittest.main()
