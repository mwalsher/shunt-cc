#!/usr/bin/env python3
"""Generate inventory_service.py: a large synthetic module for behavioral evals.

Deterministic. Planted facts the graders check: RETRY_LIMIT = 3, the classes
listed in CLASSES, late fees in BillingEngine.compute_late_fee, and two bugs
that only show in method bodies (see PLANTED).
"""
import sys

CLASSES = [
    ("WarehouseRegistry", "tracks warehouses and their capacity"),
    ("StockLedger", "records stock movements per SKU"),
    ("ReservationManager", "holds stock for open orders"),
    ("ReplenishmentPlanner", "decides when to reorder from suppliers"),
    ("SupplierGateway", "talks to supplier APIs with retries"),
    ("BillingEngine", "invoices customers and computes late fees"),
    ("AuditTrail", "keeps an append-only log of changes"),
]
VERBS = ["load", "save", "validate", "refresh", "summarize", "export", "reconcile",
         "archive", "restore", "lock", "unlock", "rebalance", "forecast", "notify"]


# Bugs: release() never checks the hold is still open, so releasing twice
# returns the stock twice; adjust(force=True) skips the floor and lets
# on_hand go negative.
PLANTED = {
    "StockLedger": [
        "    def adjust(self, sku, delta, force=False):",
        "        entry = self._records.setdefault(sku, {'on_hand': 0})",
        "        new_level = entry['on_hand'] + delta",
        "        if new_level < 0 and not force:",
        "            raise ValueError('insufficient stock for ' + sku)",
        "        entry['on_hand'] = new_level",
        "        return new_level",
        "",
    ],
    "ReservationManager": [
        "    def hold(self, ledger, order_id, sku, qty):",
        "        ledger.adjust(sku, -qty)",
        "        self._records[order_id] = {'sku': sku, 'qty': qty, 'open': True}",
        "",
        "    def release(self, ledger, order_id):",
        "        hold = self._records[order_id]",
        "        ledger.adjust(hold['sku'], hold['qty'])",
        "        return hold",
        "",
    ],
}


def method(cls, verb, i):
    noun = cls.lower()
    return [
        "    def {}_{}(self, key, payload=None):".format(verb, i),
        '        """{} {} record {} for key."""'.format(verb.capitalize(), noun, i),
        "        record = self._records.get(key)",
        "        if record is None:",
        "            record = {'key': key, 'version': 0}",
        "        record['version'] += 1",
        "        record['last_op'] = '{}_{}'".format(verb, i),
        "        if payload:",
        "            record.update(payload)",
        "        self._records[key] = record",
        "        return record",
        "",
    ]


def main(out):
    lines = ['"""Inventory service: warehouses, stock, reservations, billing."""',
             "import time", "", "RETRY_LIMIT = 3", "LATE_FEE_RATE = 0.015", ""]
    for cls, doc in CLASSES:
        lines += ["", "class {}:".format(cls), '    """{}."""'.format(doc.capitalize()), "",
                  "    def __init__(self):", "        self._records = {}", ""]
        if cls == "SupplierGateway":
            lines += ["    def call_supplier(self, request):",
                      "        for attempt in range(RETRY_LIMIT):",
                      "            ok = request(attempt)",
                      "            if ok:",
                      "                return ok",
                      "            time.sleep(2 ** attempt)",
                      "        raise RuntimeError('supplier unavailable')", ""]
        if cls == "BillingEngine":
            lines += ["    def compute_late_fee(self, amount, days_late):",
                      "        if days_late <= 0:",
                      "            return 0.0",
                      "        return round(amount * LATE_FEE_RATE * days_late, 2)", ""]
        lines += PLANTED.get(cls, [])
        for i, verb in enumerate(VERBS * 1):
            lines += method(cls, verb, i)
    open(out, "w").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "inventory_service.py")
