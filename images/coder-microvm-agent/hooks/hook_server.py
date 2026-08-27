#!/usr/bin/env python3
"""
hook_server.py — PROTOTYPE

Lifecycle-hook HTTP server for the coder-microvm-agent Lambda MicroVM image.
Implements the AWS Lambda MicroVMs hook contract on a single port (default 9000)
and, on /run, mounts the workspace's EFS home and launches the Coder agent.

Hook contract (see AWS Lambda MicroVMs docs):
  POST /aws/lambda-microvms/runtime/v1/ready      (image build: signal booted)
  POST /aws/lambda-microvms/runtime/v1/validate   (image build: smoke test)
  POST /aws/lambda-microvms/runtime/v1/run        (runtime: once after run)
  POST /aws/lambda-microvms/runtime/v1/resume     (runtime: after resume)
  POST /aws/lambda-microvms/runtime/v1/suspend    (runtime: before suspend)
  POST /aws/lambda-microvms/runtime/v1/terminate  (runtime: before terminate)

Return 200 when the hook is complete; 503 for "need more time" (ready/validate).

The /run body carries the per-workspace secrets injected by the template's
microvm_run.sh (kept OUT of the image snapshot, whose memory is shared across
runs):
  { coder_agent_token, coder_agent_url, coder_agent_init_b64, efs_dns,
    efs_access_point_id, home_dir }

NOTE: bind to 0.0.0.0 — the platform calls hooks over the guest network
namespace, so a localhost-only listener is unreachable.
"""
import base64
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("HOOK_PORT", "9000"))
BASE = "/aws/lambda-microvms/runtime/v1"

# Set true by /run once the Coder agent has been launched, so /validate at build
# time (no payload) and /resume behave correctly.
_state = {"agent_started": False}
_lock = threading.Lock()


def _mount_efs(efs_dns: str, access_point_id: str, home_dir: str) -> None:
    """Mount the per-workspace EFS access point at home_dir (in-guest).

    Requires the image built with additional-os-capabilities=ALL and a
    VPC_EGRESS connector reaching the EFS mount targets on TCP 2049.
    """
    if not efs_dns or not access_point_id:
        print("[hook] no EFS configured; using MicroVM local disk (ephemeral).")
        return
    os.makedirs(home_dir, exist_ok=True)
    # amazon-efs-utils is baked into the image (see Dockerfile).
    cmd = [
        "mount", "-t", "efs", "-o",
        f"tls,accesspoint={access_point_id}",
        f"{efs_dns.split('.')[0]}:/", home_dir,
    ]
    print(f"[hook] mounting EFS: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def _launch_agent(payload: dict) -> None:
    token = payload.get("coder_agent_token", "")
    url = payload.get("coder_agent_url", "")
    init_b64 = payload.get("coder_agent_init_b64", "")
    if not token or not url:
        raise ValueError("missing coder_agent_token / coder_agent_url in /run payload")

    env = dict(os.environ)
    env["CODER_AGENT_TOKEN"] = token
    env["CODER_AGENT_URL"] = url

    if init_b64:
        # Run the exact init script Coder generated (parity with the K8s
        # template's `command = init_script`). It fetches/execs `coder agent`,
        # which dials OUT to coderd over the tailnet — no inbound needed.
        script = base64.b64decode(init_b64).decode()
        subprocess.Popen(["sh", "-c", script], env=env)
    else:
        # Fallback: the coder binary is baked into the image.
        subprocess.Popen(["coder", "agent"], env=env)

    with _lock:
        _state["agent_started"] = True
    print("[hook] Coder agent launched (outbound tailnet).")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # quiet default logging
        pass

    def _reply(self, code: int, body: dict | None = None):
        data = json.dumps(body or {"ok": code == 200}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", "0") or "0")
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        # simple health endpoint for local `docker run` testing
        self._reply(200, {"status": "ok", "agent_started": _state["agent_started"]})

    def do_POST(self):
        path = self.path.rstrip("/")
        try:
            if path == f"{BASE}/ready":
                # App booted and ready to snapshot at image build time.
                self._reply(200)
            elif path == f"{BASE}/validate":
                # Snapshot smoke test (no per-workspace payload at build time).
                self._reply(200)
            elif path == f"{BASE}/run":
                payload = self._read_json()
                _mount_efs(
                    payload.get("efs_dns", ""),
                    payload.get("efs_access_point_id", ""),
                    payload.get("home_dir", "/home/coder"),
                )
                _launch_agent(payload)
                self._reply(200)
            elif path == f"{BASE}/resume":
                # State (incl. the running agent) is preserved across suspend;
                # nothing to do for the MVP.
                self._reply(200)
            elif path == f"{BASE}/suspend":
                self._reply(200)
            elif path == f"{BASE}/terminate":
                self._reply(200)
            else:
                self._reply(404, {"error": "unknown hook", "path": self.path})
        except Exception as exc:  # noqa: BLE001 — hooks must return, not crash
            print(f"[hook] ERROR handling {self.path}: {exc}")
            # 503 lets the platform retry /ready|/validate within the timeout.
            self._reply(503, {"error": str(exc)})


if __name__ == "__main__":
    print(f"[hook] lifecycle hook server listening on 0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
