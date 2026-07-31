"""Minimal x402 paid-execution HTTP endpoint.

x402 is KeeperHub's pay-per-execution protocol. Clients send a POST to /run
with a payment header (X-Payment) and an optional event body. The server
validates payment, runs a sentinel cycle, and returns the audit record.

Usage:
    python server.py [--port PORT] [--mock]

KEEPERHUB_API_KEY and WALLET_ADDRESS must be set in the environment.
"""

import asyncio
import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sentinel


class X402Handler(BaseHTTPRequestHandler):

    def _run_cycle(self, event=None):
        cfg = sentinel.load_config()
        log_path = sentinel.AUDIT_PATH
        mock = "--mock" in sys.argv
        coro = sentinel.run_cycle(None, cfg, log_path, trigger="x402", mock=mock)
        return asyncio.run(coro)

    def _send_json(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Powered-By", "sentinel-x402/1.0")
        self.end_headers()
        self.wfile.write(json.dumps(body, default=str).encode())

    def do_POST(self):
        if self.path != "/run":
            self._send_json(404, {"error": "only /run"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length > 0 else {}
        event = body.get("event")

        payment = self.headers.get("X-Payment")
        if not payment:
            self._send_json(402, {
                "error": "payment required",
                "protocol": "x402",
                "price_usdc": "0.01",
                "receiver": os.getenv("X402_RECEIVER", ""),
            })
            return

        try:
            record = self._run_cycle(event=event)
            self._send_json(200, {
                "outcome": record["outcome"],
                "decision": record.get("decision", {}),
                "tx_hash": record.get("tx_hash"),
                "audit_id": record.get("id"),
            })
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def do_GET(self):
        if self.path != "/status":
            self._send_json(404, {"error": "only /status"})
            return
        entries = sentinel.audit_list(sentinel.AUDIT_PATH, 20)
        self._send_json(200, {"entries": entries, "count": len(entries)})


def main():
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8420
    server = HTTPServer(("127.0.0.1", port), X402Handler)
    print(f"Sentinel x402 API on http://127.0.0.1:{port}")
    print("Endpoints: POST /run  GET /status")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutdown.")
        server.shutdown()


if __name__ == "__main__":
    main()