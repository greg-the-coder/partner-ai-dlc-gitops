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
IMPORTANT: /run MUST return 200 — a non-200 from /run terminates the MicroVM
("Run lifecycle hook returned HTTP status 503"). So /run is best-effort: it
launches the agent if a token is present but always answers 200.

The /run body carries the per-workspace secrets injected by the template's
microvm_run.sh (kept OUT of the image snapshot, whose memory is shared across
runs):
  { coder_agent_token, coder_agent_url, coder_agent_init_b64, efs_dns,
    efs_access_point_id, home_dir }

NOTE: bind to 0.0.0.0 — the platform calls hooks over the guest network
namespace, so a localhost-only listener is unreachable.
"""
from __future__ import annotations  # AL2023 base ships Python 3.9; keep PEP 604
                                    # (`X | None`) annotations lazy.
import base64
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("HOOK_PORT", "9000"))
BASE = "/aws/lambda-microvms/runtime/v1"

_state = {"agent_started": False}
_last_run = {"body_len": None, "body_head": "", "keys": [], "headers": {}}
AGENT_LOG = "/tmp/coder-agent.log"
_lock = threading.Lock()


def _mount_efs(efs_dns, access_point_id, home_dir):
    """Mount the per-workspace EFS access point at home_dir (in-guest).

    Requires the image built with additional-os-capabilities=ALL and a
    VPC_EGRESS connector reaching the EFS mount targets on TCP 2049.
    """
    if not efs_dns or not access_point_id:
        print("[hook] no EFS configured; using MicroVM local disk (ephemeral).")
        return
    os.makedirs(home_dir, exist_ok=True)
    cmd = [
        "mount", "-t", "efs", "-o",
        "tls,accesspoint=" + access_point_id,
        efs_dns.split(".")[0] + ":/", home_dir,
    ]
    print("[hook] mounting EFS: " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def _launch_agent(payload):
    token = payload.get("coder_agent_token", "")
    url = payload.get("coder_agent_url", "")
    init_b64 = payload.get("coder_agent_init_b64", "")
    if not token or not url:
        # Do NOT raise: a non-200 from /run terminates the MicroVM. Log & skip.
        print("[hook] /run: no agent token/url in payload (keys=%s); "
              "skipping agent launch." % list(payload))
        return

    env = dict(os.environ)
    env["CODER_AGENT_TOKEN"] = token
    env["CODER_AGENT_URL"] = url
    try:
        logf = open(AGENT_LOG, "ab", buffering=0)
        if init_b64:
            # Run the exact init script Coder generated (parity with the K8s
            # template's `command = init_script`); it downloads the
            # server-matched agent and execs `coder agent`.
            script = base64.b64decode(init_b64).decode()
            subprocess.Popen(["sh", "-c", script], env=env, stdout=logf, stderr=logf)
        else:
            # IMPORTANT: the agent binary must match the server's RPC API
            # version. A baked agent that is newer than the server fails the
            # handshake ('Unknown or unsupported API version'). So download the
            # SERVER-MATCHED agent from <access_url>/bin/coder-linux-arm64 (this
            # is what Coder's init script does) and fall back to the baked
            # binary only if the download fails.
            base = url.rstrip("/")
            dl = ('curl -fsSL "%s/bin/coder-linux-arm64" -o /tmp/coder-agent-bin '
                  '&& chmod +x /tmp/coder-agent-bin') % base
            agent_bin = "coder"
            try:
                subprocess.run(["sh", "-c", dl], check=True, timeout=90,
                               stdout=logf, stderr=logf)
                agent_bin = "/tmp/coder-agent-bin"
                print("[hook] downloaded server-matched agent from %s/bin" % base)
            except Exception as exc:  # noqa: BLE001
                print("[hook] WARN: agent download failed (%r); using baked coder" % exc)
            subprocess.Popen([agent_bin, "agent"], env=env, stdout=logf, stderr=logf)
        with _lock:
            _state["agent_started"] = True
        print("[hook] Coder agent launched (outbound tailnet).")
    except Exception as exc:  # noqa: BLE001 — never fail /run
        print("[hook] WARNING: failed to launch Coder agent: %r" % exc)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # quiet default logging
        pass

    def _reply(self, code, body=None):
        data = json.dumps(body or {"ok": code == 200}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_raw(self):
        n = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        # Health endpoint (also used for the ingress smoke test).
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/debug/run":
            self._reply(200, _last_run)
            return
        if path == "/debug/agent":
            # PROTOTYPE debug: tail the launched agent's stdout/stderr.
            try:
                with open(AGENT_LOG, "rb") as fh:
                    tail = fh.read()[-4000:].decode("utf-8", "replace")
            except FileNotFoundError:
                tail = "(no agent log yet)"
            self._reply(200, {"agent_started": _state["agent_started"], "log_tail": tail})
            return
        if path == "/debug/exec":
            # PROTOTYPE debug ONLY: run a shell command passed as ?c=<base64>.
            # Reachable only with a valid MicroVM proxy auth token. Remove before
            # any non-prototype use.
            import urllib.parse
            q = urllib.parse.urlparse(self.path).query
            c = urllib.parse.parse_qs(q).get("c", [""])[0]
            try:
                cmd = base64.b64decode(c).decode() if c else "echo no-cmd"
                out = subprocess.run(["sh", "-c", cmd], capture_output=True,
                                     timeout=25, text=True)
                self._reply(200, {"cmd": cmd, "rc": out.returncode,
                                  "stdout": out.stdout[-3000:], "stderr": out.stderr[-3000:]})
            except Exception as exc:  # noqa: BLE001
                self._reply(200, {"error": repr(exc)})
            return
        self._reply(200, {"status": "ok", "agent_started": _state["agent_started"]})

    def do_POST(self):
        path = self.path.rstrip("/")
        try:
            if path == BASE + "/ready":
                self._reply(200)
            elif path == BASE + "/validate":
                self._reply(200)
            elif path == BASE + "/run":
                raw = self._read_raw()
                print("[hook] /run invoked: body_len=%d body_head=%r"
                      % (len(raw), raw[:120]))
                payload = {}
                if raw:
                    try:
                        outer = json.loads(raw)
                    except json.JSONDecodeError:
                        outer = {}
                        print("[hook] /run: body not JSON; using empty payload.")
                    # The platform wraps our payload: the /run body is
                    #   {"microvmId": "...", "runHookPayload": "<string we passed>"}
                    # so the RunMicrovm --run-hook-payload arrives as the STRING
                    # value of runHookPayload (not at the top level).
                    inner = outer.get("runHookPayload", "")
                    if isinstance(inner, str) and inner:
                        try:
                            payload = json.loads(inner)
                        except json.JSONDecodeError:
                            print("[hook] /run: runHookPayload not JSON.")
                    elif isinstance(inner, dict):
                        payload = inner
                # Record what /run actually received so we can learn the payload
                # delivery mechanism via GET /debug/run (runtime stdout is not
                # shipped to CloudWatch).
                with _lock:
                    _last_run["body_len"] = len(raw)
                    _last_run["body_head"] = raw[:200].decode("utf-8", "replace")
                    _last_run["keys"] = list(payload)
                    _last_run["headers"] = {k: v for k, v in self.headers.items()}
                try:
                    _mount_efs(payload.get("efs_dns", ""),
                               payload.get("efs_access_point_id", ""),
                               payload.get("home_dir", "/home/coder"))
                except Exception as exc:  # noqa: BLE001
                    print("[hook] WARNING: EFS mount failed: %r" % exc)
                _launch_agent(payload)
                self._reply(200)  # MUST be 200 or the platform terminates the VM
            elif path == BASE + "/resume":
                self._reply(200)
            elif path == BASE + "/suspend":
                self._reply(200)
            elif path == BASE + "/terminate":
                self._reply(200)
            else:
                self._reply(404, {"error": "unknown hook", "path": self.path})
        except Exception as exc:  # noqa: BLE001
            print("[hook] ERROR handling %s: %r" % (self.path, exc))
            # 503 lets the platform retry /ready|/validate within the timeout.
            self._reply(503, {"error": str(exc)})


if __name__ == "__main__":
    print("[hook] lifecycle hook server listening on 0.0.0.0:%d" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
