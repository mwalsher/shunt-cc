#!/usr/bin/env python3
"""Build the A/B workspace: a small repo of large files with planted problems.

Usage: workspace.py <dir>

Deterministic. On top of evals/fixtures/make_inventory.py's module it plants:
  - a misleading comment in ReservationManager.release, which says a second
    release is a no-op when it isn't (the double-release bug is still there);
  - a shell command built from input in AuditTrail.export_archive;
  - order_api.py, a second large file, where one handler passes hold()'s
    arguments in the wrong order;
  - test_inventory.py, whose test of releasing twice fails.
For the harder tasks it also plants:
  - CaseOrders, which stores a booking in cases but returns and counts it as
    units, with a comment that points the fix at the wrong method;
    test_cases.py fails, and hidden tests (HIDDEN_TESTS, written only when
    grading) check that the fix is at the cause;
  - two more AuditTrail methods that look risky but aren't (validated name,
    argument list; shell=True on a fixed command) and one that is (a path
    joined from input);
  - order_api.py handlers that pass force=True to adjust(), by keyword and by
    position, only some of which can drive stock below zero; and a restock
    and a writeoff handler whose sign is flipped, among 64.
The tasks in run.py grade against these. The directory is a git repo with one
commit, so a task's edits show up in `git diff`.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "fixtures"))
import make_inventory  # noqa: E402

SWAPPED_HANDLER = "handle_rush_order"

EXPORT_ARCHIVE = [
    "    def export_archive(self, name):",
    "        \"\"\"Archive the audit log under a name chosen in the admin UI.\"\"\"",
    "        os.system('tar czf /var/backups/' + name + '.tgz /var/audit')",
    "        return name",
    "",
]
MISLEADING = "        # Closed holds are skipped, so releasing twice is a no-op."

CONSTANTS = [
    "CASE_SIZE = 12",
    "SNAPSHOT_DIR = '/var/snapshots'",
    "MAINTENANCE = {",
    "    'vacuum': 'sqlite3 /var/audit/audit.db VACUUM',",
    "    'rotate': 'logrotate /etc/logrotate.d/audit',",
    "}",
]

AUDIT_EXTRA = [
    "    def export_report(self, name):",
    "        \"\"\"Archive a report under a name chosen in the admin UI.\"\"\"",
    "        if not re.fullmatch(r'[a-z0-9_-]+', name):",
    "            raise ValueError('bad report name')",
    "        subprocess.run(['tar', 'czf', '/var/reports/' + name + '.tgz', '/var/audit'], check=True)",
    "        return name",
    "",
    "    def run_maintenance(self, task):",
    "        \"\"\"Run one of the fixed maintenance commands, by name.\"\"\"",
    "        subprocess.run(MAINTENANCE[task], shell=True, check=True)",
    "",
    "    def restore_snapshot(self, name):",
    "        \"\"\"Read back a snapshot the admin picked in the UI.\"\"\"",
    "        with open(os.path.join(SNAPSHOT_DIR, name)) as f:",
    "            return f.read()",
    "",
]
VULNERABLE = {"export_archive", "restore_snapshot"}

CASE_ORDERS = [
    "class CaseOrders:",
    "    \"\"\"Books stock for orders placed in whole cases.\"\"\"",
    "",
    "    def __init__(self):",
    "        self._records = {}",
    "",
    "    def book(self, ledger, order_id, sku, cases):",
    "        units = cases * CASE_SIZE",
    "        ledger.adjust(sku, -units)",
    "        # A booking keeps the order's case count; callers convert as needed.",
    "        self._records[order_id] = {'sku': sku, 'qty': cases}",
    "",
    "    def booked_units(self, sku):",
    "        \"\"\"Units booked for sku across open orders.\"\"\"",
    "        return sum(r['qty'] for r in self._records.values() if r['sku'] == sku)",
    "",
    "    def cancel(self, ledger, order_id):",
    "        \"\"\"Return a booking's stock to the ledger.\"\"\"",
    "        booking = self._records.pop(order_id)",
    "        ledger.adjust(booking['sku'], booking['qty'])",
    "        return booking",
    "",
    "",
]

CASE_TESTS = '''"""Tests for CaseOrders. Run: python3 -m unittest test_cases"""
import unittest

from inventory_service import CaseOrders, StockLedger


class CaseOrdersTest(unittest.TestCase):
    def setUp(self):
        self.ledger = StockLedger()
        self.ledger.adjust("sku-1", 100)
        self.orders = CaseOrders()

    def level(self):
        return self.ledger._records["sku-1"]["on_hand"]

    def test_book_takes_whole_cases(self):
        self.orders.book(self.ledger, "o1", "sku-1", 2)
        self.assertEqual(self.level(), 76)

    def test_cancel_returns_stock(self):
        self.orders.book(self.ledger, "o1", "sku-1", 2)
        self.orders.cancel(self.ledger, "o1")
        self.assertEqual(self.level(), 100)


if __name__ == "__main__":
    unittest.main()
'''

# Written into the workspace only after the session ends, then run.
HIDDEN_TESTS = '''import unittest

from inventory_service import CaseOrders, StockLedger


class HiddenCaseOrdersTest(unittest.TestCase):
    def setUp(self):
        self.ledger = StockLedger()
        self.ledger.adjust("sku-1", 100)
        self.orders = CaseOrders()

    def test_booked_units_counts_units(self):
        self.orders.book(self.ledger, "o1", "sku-1", 2)
        self.orders.book(self.ledger, "o2", "sku-1", 1)
        self.assertEqual(self.orders.booked_units("sku-1"), 36)

    def test_booked_units_drops_cancelled(self):
        self.orders.book(self.ledger, "o1", "sku-1", 2)
        self.orders.book(self.ledger, "o2", "sku-1", 1)
        self.orders.cancel(self.ledger, "o1")
        self.assertEqual(self.orders.booked_units("sku-1"), 12)
        self.assertEqual(self.ledger._records["sku-1"]["on_hand"], 88)
'''

TESTS = '''"""Tests for inventory_service. Run: python3 -m unittest test_inventory"""
import unittest

from inventory_service import BillingEngine, ReservationManager, StockLedger


class StockTest(unittest.TestCase):
    def setUp(self):
        self.ledger = StockLedger()
        self.ledger.adjust("sku-1", 10)
        self.holds = ReservationManager()

    def level(self):
        return self.ledger._records["sku-1"]["on_hand"]

    def test_hold_reduces_stock(self):
        self.holds.hold(self.ledger, "o1", "sku-1", 3)
        self.assertEqual(self.level(), 7)

    def test_release_returns_stock(self):
        self.holds.hold(self.ledger, "o1", "sku-1", 3)
        self.holds.release(self.ledger, "o1")
        self.assertEqual(self.level(), 10)

    def test_release_twice_returns_stock_once(self):
        self.holds.hold(self.ledger, "o1", "sku-1", 3)
        self.holds.release(self.ledger, "o1")
        self.holds.release(self.ledger, "o1")
        self.assertEqual(self.level(), 10)

    def test_hold_more_than_stock_fails(self):
        with self.assertRaises(ValueError):
            self.holds.hold(self.ledger, "o1", "sku-1", 11)


class BillingTest(unittest.TestCase):
    def test_late_fee(self):
        self.assertEqual(BillingEngine().compute_late_fee(100.0, 2), 3.0)

    def test_no_fee_when_on_time(self):
        self.assertEqual(BillingEngine().compute_late_fee(100.0, 0), 0.0)


if __name__ == "__main__":
    unittest.main()
'''

HANDLER_KINDS = [
    ("standard", "hold", "reservations.hold(ledger, order_id, sku, qty)"),
    ("cancel", "release", "reservations.release(ledger, order_id)"),
    ("restock", "adjust", "ledger.adjust(sku, qty)"),
    ("writeoff", "adjust", "ledger.adjust(sku, -qty)"),
]
REGIONS = ["north", "south", "east", "west", "central", "coastal", "mountain", "island",
           "metro", "rural", "export", "returns", "bulk", "harbor", "valley", "desert"]


def inventory_source():
    with tempfile.TemporaryDirectory() as tmp:  # runs build workspaces in parallel
        path = os.path.join(tmp, "inventory_service.py")
        make_inventory.main(path)
        lines = open(path).read().splitlines()
    out = []
    for line in lines:
        if line == "class AuditTrail:":
            out += CASE_ORDERS
        out.append(line)
        if line == "import time":
            out[-1:] = ["import os", "import re", "import subprocess", "import time"]
        if line.startswith("LATE_FEE_RATE"):
            out += CONSTANTS
        if line == "    def release(self, ledger, order_id):":
            out.append(MISLEADING)
        if line == "class AuditTrail:":
            out.append("__AUDIT__")
    text = "\n".join(out) + "\n"
    # Put export_archive right after AuditTrail.__init__.
    head, tail = text.split("__AUDIT__\n")
    marker = "        self._records = {}\n\n"
    i = tail.index(marker) + len(marker)
    return head + tail[:i] + "\n".join(EXPORT_ARCHIVE + AUDIT_EXTRA) + "\n" + tail[i:]


# Handlers that pass force=True to adjust(): which can drive stock below zero.
FORCED = [
    ("handle_damage_writeoff", "damage writeoff", "ledger.adjust(sku, -qty, force=True)"),
    ("handle_vendor_restock", "vendor restock", "ledger.adjust(sku, qty, force=True)"),
    ("handle_shrinkage_writeoff", "shrinkage writeoff", "ledger.adjust(sku, -qty, True)"),
]
CAN_GO_NEGATIVE = {"handle_damage_writeoff", "handle_shrinkage_writeoff"}
# Regional handlers whose sign is the opposite of their kind.
FLIPPED = {"handle_valley_restock": "ledger.adjust(sku, -qty)",
           "handle_metro_writeoff": "ledger.adjust(sku, qty)"}


def order_api_source():
    lines = ['"""Order API: HTTP handlers that drive inventory_service."""',
             "from inventory_service import ReservationManager, StockLedger", "",
             "ledger = StockLedger()", "reservations = ReservationManager()", ""]
    for region in REGIONS:
        for kind, verb, call in HANDLER_KINDS:
            name = "handle_{}_{}".format(region, kind)
            lines += _handler(name, kind, FLIPPED.get(name, call))
    # The planted bug: sku and order_id swapped in hold().
    lines += _handler(SWAPPED_HANDLER, "rush", "reservations.hold(ledger, sku, order_id, qty)")
    for region in REGIONS[:2]:
        lines += _handler("handle_{}_audit".format(region), "audit", "reservations.release(ledger, order_id)")
    for name, kind, call in FORCED:
        lines += _handler(name, kind, call)
    return "\n".join(lines) + "\n"


def _handler(name, kind, call):
    return [
        "",
        "def {}(request):".format(name),
        '    """Handle a {} request."""'.format(kind),
        "    order_id = request['order_id']",
        "    sku = request['sku']",
        "    qty = int(request.get('qty', 1))",
        "    if qty <= 0:",
        "        return {'status': 400, 'error': 'qty must be positive'}",
        "    result = {}".format(call),
        "    return {'status': 200, 'result': result}",
    ]


def main(root):
    os.makedirs(root, exist_ok=True)
    files = {"inventory_service.py": inventory_source(), "order_api.py": order_api_source(),
             "test_inventory.py": TESTS, "test_cases.py": CASE_TESTS}
    for name, text in files.items():
        with open(os.path.join(root, name), "w") as f:
            f.write(text)
    git = ["git", "-C", root, "-c", "user.name=eval", "-c", "user.email=eval@example.com"]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-qm", "workspace"], check=True)


if __name__ == "__main__":
    main(sys.argv[1])
