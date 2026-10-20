"""JSON-only repair handlers; loaded only when the repair verb is executed."""

from __future__ import annotations

import argparse
import json
import subprocess

from sonoranlab.repair import EditorUnavailable, Refused
from sonoranlab.repair import workspace as ws
from sonoranlab.repair.authorization import authorize, lease
from sonoranlab.repair.compare import diff, read_json
from sonoranlab.repair.confine import admit_repair


def run(args: argparse.Namespace) -> int:
    try:
        action = args.repair_action
        if action == "prepare":
            result = ws.prepare(args.parent_job, args.profile, args.character, args.anim, args.out)
        elif action == "authorize":
            args.repair = admit_repair(args.repair)
            result = authorize(args.repair, read_json(args.spec))
        elif action in ("session", "op", "read"):
            from sonoranlab.repair.session import detach, request, serve

            if action == "session":
                operation = args.session_action
                if operation in ("start", "stop", "checkpoint"):
                    args.repair = admit_repair(args.repair)
                if operation == "start":
                    result = detach(args.repair) if args.detach else serve(args.repair)
                else:
                    payload = {"action": operation}
                    if operation == "stop":
                        payload["checkpoint"] = args.checkpoint
                    result = request(args.repair, payload)
            else:
                result = request(args.repair, {**read_json(args.spec), "action": action})
        elif action == "relay":
            args.repair = admit_repair(args.repair)
            from sonoranlab.repair.relay import relay

            result = relay(args.repair, args.agent_dir)
        elif action == "abort":
            args.repair = admit_repair(args.repair)
            from sonoranlab.repair.session import abort

            result = abort(args.repair)
        elif action == "export":
            args.repair = admit_repair(args.repair)
            with lease(args.repair):
                result = ws.export(args.repair)
        elif action == "finalize":
            args.repair = admit_repair(args.repair)
            from sonoranlab.repair.finalize import finalize

            result = finalize(args.repair, args.out)
        elif action == "diff":
            result = diff(args.repair)
        else:
            result = ws.inspect(args.repair)
        code = 0 if result["ok"] else 1
    except EditorUnavailable as e:
        result, code = {"ok": False, "error": "editor_unavailable", "message": str(e)}, 1
    except (Refused, FileExistsError) as e:
        result, code = {"ok": False, "error": getattr(e, "code", "refused"), "message": str(e)}, 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as e:
        result, code = {"ok": False, "error": "io_or_usage_error", "message": str(e)}, 2
    print(json.dumps(result, sort_keys=True))
    return code
