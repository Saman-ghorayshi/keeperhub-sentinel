import json
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server


def build_handler(path="/", payment=None, body=b""):
    """Build a mock handler with a given request state."""
    h = MagicMock()
    h.path = path
    h.headers = MagicMock()

    headers = {
        "Content-Length": str(len(body)),
    }
    if payment:
        headers["X-Payment"] = payment

    h.headers.get.side_effect = lambda k, d=None: headers.get(k, d)

    h.rfile = MagicMock()
    h.rfile.read.return_value = body

    h._run_cycle = MagicMock(return_value={
        "outcome": "success",
        "decision": {"action": "supply"},
        "tx_hash": "0xabc",
        "id": "test-id",
    })

    # capture send output
    h._send_output = {}
    h._send_json = MagicMock(side_effect=lambda code, body: h._send_output.update(code=code, body=body))
    return h


def test_returns_402_when_no_payment():
    h = build_handler(path="/run")
    server.X402Handler.do_POST(h)
    assert h._send_output["code"] == 402
    assert h._send_output["body"]["error"] == "payment required"


def test_accepts_paid_request():
    h = build_handler(path="/run", payment="proof_0x123")
    server.X402Handler.do_POST(h)
    assert h._send_output["code"] == 200
    assert h._send_output["body"]["outcome"] == "success"


def test_404_on_wrong_path():
    h = build_handler(path="/random")
    server.X402Handler.do_POST(h)
    assert h._send_output["code"] == 404


def test_500_on_agent_crash():
    h = build_handler(path="/run", payment="ok")
    h._run_cycle.side_effect = RuntimeError("boom")
    server.X402Handler.do_POST(h)
    assert h._send_output["code"] == 500
    assert "boom" in h._send_output["body"]["error"]


def test_blocked_outcome_passed_through():
    h = build_handler(path="/run", payment="x")
    h._run_cycle.return_value = {"outcome": "blocked", "decision": {"action": "supply"}, "id": "blk-id"}
    server.X402Handler.do_POST(h)
    assert h._send_output["code"] == 200
    assert h._send_output["body"]["outcome"] == "blocked"