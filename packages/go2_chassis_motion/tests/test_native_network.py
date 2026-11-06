import copy
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from go2_chassis_motion.transport import check_native_network


LINK = [{
    "ifname": "eth10", "flags": ["UP", "LOWER_UP", "BROADCAST", "MULTICAST"],
    "addr_info": [
        {"family": "inet", "scope": "global", "local": "192.168.123.18", "prefixlen": 24},
        {"family": "inet", "scope": "global", "local": "192.168.137.2", "prefixlen": 24},
        {"family": "inet6", "scope": "global", "local": "2001:db8::18", "prefixlen": 64},
    ],
}]
ROUTE = [{"dst": "192.168.123.161", "dev": "eth10", "prefsrc": "192.168.123.18"}]


class NativeNetworkTest(unittest.TestCase):
    def check(self, link=None, route=None, robot_address="192.168.123.161"):
        responses = [
            SimpleNamespace(stdout=json.dumps(LINK if link is None else link)),
            SimpleNamespace(stdout=json.dumps(ROUTE if route is None else route)),
        ]
        with mock.patch("go2_chassis_motion.transport.subprocess.run",
                        side_effect=responses) as run:
            result = check_native_network("eth10", robot_address)
            self.assertEqual(run.call_count, 2)
            # No NM edits, address/route changes, or controller API requests.
            for call in run.call_args_list:
                self.assertIn(call.args[0][3], ("address", "route"))
                self.assertTrue(call.kwargs["check"])
                self.assertEqual(call.kwargs["timeout"], 3)
        return result

    def test_onboard_secondary_address_and_ipv6_do_not_block_direct_robot_route(self):
        result = self.check()
        self.assertEqual(result["local_address"], "192.168.123.18/24")
        self.assertEqual(result["robot_address"], "192.168.123.161")
        self.assertFalse(result["host_network_changed"])

    def test_local_address_is_measured_rather_than_hardcoded_to_dot_99(self):
        link, route = copy.deepcopy(LINK), copy.deepcopy(ROUTE)
        link[0]["addr_info"][0]["local"] = "192.168.123.42"
        route[0]["prefsrc"] = "192.168.123.42"
        self.assertEqual(self.check(link, route)["local_address"], "192.168.123.42/24")

    def test_link_down_or_missing_is_rejected(self):
        link = copy.deepcopy(LINK)
        link[0]["flags"].remove("LOWER_UP")
        for value in [[], link]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.check(link=value)

    def test_wrong_device_gateway_route_and_local_route_are_rejected(self):
        for changes in [{"dev": "eth0"}, {"gateway": "192.168.137.1"}, {"type": "local"}]:
            route = copy.deepcopy(ROUTE)
            route[0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.check(route=route)

    def test_unassigned_or_wrong_subnet_source_and_ambiguous_routes_are_rejected(self):
        for route in [
            [{**ROUTE[0], "prefsrc": "192.168.123.99"}],
            [{**ROUTE[0], "prefsrc": "192.168.137.2"}],
            [], ROUTE + ROUTE,
        ]:
            with self.subTest(route=route), self.assertRaises(ValueError):
                self.check(route=route)

    def test_this_host_and_subnet_endpoints_are_not_robot_targets(self):
        for address in ["192.168.123.18", "192.168.123.0", "192.168.123.255"]:
            route = [{**ROUTE[0], "dst": address}]
            with self.subTest(address=address), self.assertRaises(ValueError):
                self.check(route=route, robot_address=address)

    def test_invalid_public_or_non_unicast_targets_do_not_probe_network(self):
        for address in ["not-an-address", "8.8.8.8", "127.0.0.1", "0.0.0.0",
                        "239.255.0.1", "255.255.255.255", "169.254.1.2", "::1"]:
            with self.subTest(address=address):
                with mock.patch("go2_chassis_motion.transport.subprocess.run") as run:
                    with self.assertRaises(ValueError):
                        check_native_network("eth10", address)
                    run.assert_not_called()
