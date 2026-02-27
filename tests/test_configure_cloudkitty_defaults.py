import io
import json
import unittest
from unittest.mock import patch

from scripts import configure_cloudkitty_defaults as cfg


class ConfigureCloudKittyDefaultsTests(unittest.TestCase):
    def test_warn_if_flavors_will_be_rated_prints_matching_flavors(self):
        pricing = {"instance": [{"value": "small", "cost": 0.01}, {"value": "medium", "cost": 0.02}]}
        with patch.object(cfg, "get_openstack_flavor_names", return_value=["small", "xlarge"]), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as out:
            cfg.warn_if_flavors_will_be_rated(pricing)

        stdout = out.getvalue()
        self.assertIn("will be rated", stdout)
        self.assertIn(" - small", stdout)
        self.assertNotIn(" - xlarge", stdout)

    def test_warn_if_flavors_will_be_rated_no_match_no_output(self):
        pricing = {"instance": [{"value": "small", "cost": 0.01}]}
        with patch.object(cfg, "get_openstack_flavor_names", return_value=["xlarge"]), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as out:
            cfg.warn_if_flavors_will_be_rated(pricing)

        self.assertEqual(out.getvalue(), "")

    def test_get_openstack_flavor_names_handles_missing_cli(self):
        with patch("subprocess.run", side_effect=FileNotFoundError), patch("sys.stdout", new_callable=io.StringIO) as out:
            names = cfg.get_openstack_flavor_names()

        self.assertIsNone(names)
        self.assertIn("openstack CLI not found", out.getvalue())

    def test_get_openstack_flavor_names_parses_json_output(self):
        payload = json.dumps([{"Name": "small"}, {"Name": "medium"}])

        class Result:
            stdout = payload

        with patch("subprocess.run", return_value=Result()):
            names = cfg.get_openstack_flavor_names()

        self.assertEqual(names, ["small", "medium"])


if __name__ == "__main__":
    unittest.main()
