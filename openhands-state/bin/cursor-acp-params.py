#!/usr/bin/env python3
"""cursor-acp-params — ACP proxy that lets Agent Canvas use Cursor model parameters.

Cursor's `agent acp` only exposes context / reasoning effort / fast as separate
settings when the client opts into its "parameterized model picker". Agent Canvas
doesn't, so it only ever sees one default variant per model (grok-4.7 = 256k/high).
This proxy sits between Canvas and `agent acp` and:

  1. opts into the parameterized picker during `initialize`;
  2. right after `session/new` / `session/load`, applies the defaults given on the
     command line (e.g. grok-4.7 + context=500k + reasoning_effort=xhigh + fast=true);
  3. accepts composite model IDs from Canvas, e.g.
     "grok-4.7[context=500k,reasoning_effort=xhigh,fast=true]", and applies each part;
  4. reports the model back to Canvas as a composite built from the values Cursor
     actually confirmed, so the Canvas tooltip shows what is really in effect.

Prompts and responses pass through untouched. The log only records config changes.

Profile "Command" example:
  python3 /home/openhands/.openhands/bin/cursor-acp-params.py --model grok-4.7
      --set context=500k --set reasoning_effort=xhigh --set fast=true
      -- /Users/vitale/.local/bin/agent acp
"""
import copy
import itertools
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

PARAM_CATEGORIES = ("model_config", "thought_level")
INTERNAL_ID_BASE = 900_000_000
COMPOSITE = re.compile(r"^([^\[\]]+)\[(.*)\]$")


def parse_composite(value):
    """'grok-4.7[context=500k,fast=true]' -> ('grok-4.7', [('context','500k'), ('fast','true')])."""
    m = COMPOSITE.match(value or "")
    if not m:
        return value, []
    params = []
    for part in m.group(2).split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            params.append((k.strip(), v.strip()))
    return m.group(1).strip(), params


def iter_options(options):
    for o in options or []:
        if isinstance(o, dict) and isinstance(o.get("options"), list):  # grouped select
            for x in o["options"]:
                yield x
        elif isinstance(o, dict):
            yield o


class Proxy:
    def __init__(self, cmd, model, params, log_path):
        self.default_model = model
        self.default_params = params
        self.log_path = log_path
        self.out = sys.stdout.buffer
        self.out_lock = threading.Lock()
        self.agent_lock = threading.Lock()
        self.hold = 0
        self.buffer = []
        self.pending = {}      # internal request id -> [Event, response]
        self.intercept = {}    # canvas request id -> (method, params) for session/new|load
        self.req_sid = {}      # canvas request id -> sessionId (forwarded set_config_option)
        self.last_opts = {}    # sessionId -> raw configOptions as Cursor last reported them
        self.ids = itertools.count(INTERNAL_ID_BASE)
        self.agent = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
        self.log(f"start pid={self.agent.pid} cmd={cmd} defaults={model} {params}")

    # ---------------------------------------------------------------- utils
    def log(self, text):
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "a") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {text}\n")
        except Exception:
            pass

    def to_agent(self, msg):
        data = (json.dumps(msg) + "\n").encode() if not isinstance(msg, bytes) else msg
        with self.agent_lock:
            self.agent.stdin.write(data)
            self.agent.stdin.flush()

    def _write(self, data):
        self.out.write(data)
        self.out.flush()

    def to_canvas(self, msg, force=False):
        data = (json.dumps(msg) + "\n").encode() if not isinstance(msg, bytes) else msg
        with self.out_lock:
            if self.hold and not force:
                self.buffer.append(data)
            else:
                self._write(data)

    def release(self, msg):
        with self.out_lock:
            self._write((json.dumps(msg) + "\n").encode())
            self.hold = max(0, self.hold - 1)
            if not self.hold:
                for data in self.buffer:
                    self._write(data)
                self.buffer.clear()

    def call(self, method, params, timeout=60):
        iid = next(self.ids)
        slot = [threading.Event(), None]
        self.pending[iid] = slot
        self.to_agent({"jsonrpc": "2.0", "id": iid, "method": method, "params": params})
        ok = slot[0].wait(timeout)
        self.pending.pop(iid, None)
        return slot[1] if ok else {"error": {"code": -32000, "message": f"timeout after {timeout}s"}}

    # ------------------------------------------------------------ model logic
    @staticmethod
    def summary(opts):
        return {o.get("id"): o.get("currentValue") for o in opts or [] if o.get("id") != "mode"}

    def current_model(self, sid):
        for o in self.last_opts.get(sid) or []:
            if o.get("id") == "model":
                return o.get("currentValue")
        return None

    def apply(self, sid, model, params):
        """Set model then each param. Returns (configOptions or None, [errors])."""
        last, errors = None, []
        if model and model != self.current_model(sid):
            r = self.call("session/set_config_option", {"sessionId": sid, "configId": "model", "value": model})
            if "error" in r:
                errors.append(f"model={model}: {json.dumps(r['error'])}")
                self.log(f"session {sid}: FAILED model={model} -> {r['error']}")
                return None, errors
            last = (r.get("result") or {}).get("configOptions")
            if last:
                self.last_opts[sid] = last
        for key, value in params:
            r = self.call("session/set_config_option", {"sessionId": sid, "configId": key, "value": value})
            if "error" in r:
                errors.append(f"{key}={value}: {json.dumps(r['error'])}")
                self.log(f"session {sid}: FAILED {key}={value} -> {r['error']}")
                continue
            last = (r.get("result") or {}).get("configOptions") or last
            if last:
                self.last_opts[sid] = last
        self.log(f"session {sid}: requested {model} {params} -> Cursor confirmed {self.summary(self.last_opts.get(sid))}")
        return self.last_opts.get(sid), errors

    @staticmethod
    def decorate(opts):
        """Copy of configOptions where the model option shows the confirmed composite ID."""
        opts = copy.deepcopy(opts)
        model_opt = next((o for o in opts if o.get("id") == "model"), None)
        if not model_opt:
            return opts, None
        base = model_opt.get("currentValue")
        params = [(o.get("id"), o.get("currentValue")) for o in opts
                  if o.get("category") in PARAM_CATEGORIES and o.get("currentValue") is not None]
        if not base or not params:
            return opts, base
        comp = f"{base}[{','.join(f'{k}={v}' for k, v in params)}]"
        model_opt["currentValue"] = comp
        options = model_opt.get("options")
        if isinstance(options, list) and not any(x.get("value") == comp for x in iter_options(options)):
            name = next((x.get("name") for x in iter_options(options) if x.get("value") == base), base)
            entry = {"value": comp, "name": f"{name} ({', '.join(f'{k}={v}' for k, v in params)})"}
            idx = next((i for i, x in enumerate(options) if isinstance(x, dict) and x.get("value") == base), None)
            if idx is not None:
                options.insert(idx + 1, entry)
            else:
                options.append(entry)
        return opts, comp

    def transform(self, msg, sid=None):
        """Rewrite outgoing (to Canvas) messages that carry model state."""
        try:
            res = msg.get("result")
            if isinstance(res, dict) and isinstance(res.get("configOptions"), list):
                if sid:
                    self.last_opts[sid] = res["configOptions"]
                res["configOptions"], comp = self.decorate(res["configOptions"])
                models = res.get("models")
                if comp and isinstance(models, dict):
                    models["currentModelId"] = comp
                    avail = models.get("availableModels")
                    if isinstance(avail, list) and not any(m.get("modelId") == comp for m in avail):
                        avail.insert(0, {"modelId": comp, "name": comp})
            if msg.get("method") == "session/update":
                params = msg.get("params") or {}
                upd = params.get("update") or {}
                if isinstance(upd.get("configOptions"), list):
                    self.last_opts[params.get("sessionId")] = upd["configOptions"]
                    upd["configOptions"], _ = self.decorate(upd["configOptions"])
        except Exception as e:  # never break the stream over cosmetics
            self.log(f"transform error: {e!r}")
        return msg

    # ------------------------------------------------------------- handlers
    def after_session(self, msg, method, params):
        sid = None
        try:
            res = msg.get("result")
            sid = (res or {}).get("sessionId") if isinstance(res, dict) else None
            sid = sid or (params or {}).get("sessionId")
            if isinstance(res, dict) and isinstance(res.get("configOptions"), list):
                self.last_opts[sid] = res["configOptions"]
            self.log(f"{method} {sid}: Cursor default {self.summary(self.last_opts.get(sid))}")
            if "error" not in msg and sid and (self.default_model or self.default_params):
                opts, _ = self.apply(sid, self.default_model, self.default_params)
                if opts and isinstance(res, dict):
                    res["configOptions"] = opts
            msg = self.transform(msg)
        except Exception as e:
            self.log(f"after_session error: {e!r}")
        self.release(msg)

    def handle_set_model(self, msg, sid, value):
        base, params = parse_composite(value)
        opts, errors = self.apply(sid, base, params)
        if opts is None:
            reply = {"jsonrpc": "2.0", "id": msg.get("id"),
                     "error": {"code": -32602, "message": "Invalid params",
                               "data": {"message": "; ".join(errors)}}}
        elif msg.get("method") == "session/set_model":
            reply = {"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}
        else:
            reply = self.transform({"jsonrpc": "2.0", "id": msg.get("id"), "result": {"configOptions": opts}})
        self.to_canvas(reply)

    def agent_reader(self):
        for raw in iter(self.agent.stdout.readline, b""):
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                self.to_canvas(raw if raw.endswith(b"\n") else raw + b"\n")
                continue
            mid = msg.get("id")
            if "method" not in msg and mid in self.pending:
                slot = self.pending[mid]
                slot[1] = msg
                slot[0].set()
                continue
            if "method" not in msg and mid in self.intercept:
                method, params = self.intercept.pop(mid)
                with self.out_lock:
                    self.hold += 1
                threading.Thread(target=self.after_session, args=(msg, method, params), daemon=True).start()
                continue
            sid = self.req_sid.pop(mid, None) if "method" not in msg else None
            self.to_canvas(self.transform(msg, sid))
        rc = self.agent.wait()
        self.log(f"agent exited rc={rc}")
        with self.out_lock:
            for data in self.buffer:
                self._write(data)
        os._exit(rc if rc is not None and rc >= 0 else 1)

    def canvas_reader(self):
        for raw in iter(sys.stdin.buffer.readline, b""):
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                self.to_agent(raw if raw.endswith(b"\n") else raw + b"\n")
                continue
            method = msg.get("method")
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            if method == "initialize":
                caps = params.setdefault("clientCapabilities", {})
                meta = caps.get("_meta") if isinstance(caps.get("_meta"), dict) else {}
                meta["parameterizedModelPicker"] = True
                caps["_meta"] = meta
                msg["params"] = params
                self.log("initialize: parameterizedModelPicker enabled")
            elif method in ("session/new", "session/load") and "id" in msg:
                self.intercept[msg["id"]] = (method, params)
            elif method == "session/set_config_option" and "id" in msg:
                sid = params.get("sessionId")
                if params.get("configId") == "model":
                    base, parts = parse_composite(str(params.get("value")))
                    if parts:
                        self.log(f"Canvas set model={params.get('value')}")
                        threading.Thread(target=self.handle_set_model,
                                         args=(msg, sid, params.get("value")), daemon=True).start()
                        continue
                self.req_sid[msg["id"]] = sid
            elif method == "session/set_model" and "id" in msg:
                self.log(f"Canvas set_model={params.get('modelId')}")
                threading.Thread(target=self.handle_set_model,
                                 args=(msg, params.get("sessionId"), str(params.get("modelId"))), daemon=True).start()
                continue
            self.to_agent(msg)
        self.log("canvas closed stdin")
        try:
            self.agent.stdin.close()
        except Exception:
            pass

    def run(self):
        def stop(signum, _frame):
            self.log(f"signal {signum}")
            try:
                self.agent.terminate()
            except Exception:
                pass
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        threading.Thread(target=self.agent_reader, daemon=True).start()
        self.canvas_reader()
        try:
            rc = self.agent.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.agent.kill()
            rc = 1
        time.sleep(0.2)
        os._exit(rc if rc is not None and rc >= 0 else 1)


def main(argv):
    if "--" not in argv:
        sys.stderr.write("usage: cursor-acp-params.py [--model M] [--set k=v ...] [--log PATH] -- <agent acp command>\n")
        return 2
    i = argv.index("--")
    opts, cmd = argv[:i], argv[i + 1:]
    model, params = None, []
    log_path = os.path.expanduser("~/.openhands/acp-wrapper.log")
    it = iter(opts)
    for a in it:
        if a == "--model":
            model, extra = parse_composite(next(it))
            params.extend(extra)
        elif a == "--set":
            k, _, v = next(it).partition("=")
            params.append((k, v))
        elif a == "--log":
            log_path = next(it)
    if not cmd:
        sys.stderr.write("missing agent command after --\n")
        return 2
    Proxy(cmd, model, params, log_path).run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
