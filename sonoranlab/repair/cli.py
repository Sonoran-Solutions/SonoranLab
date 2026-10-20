"""JSON-only repair handlers; loaded only when the repair verb is executed."""

from __future__ import annotations

import argparse
import json
import subprocess

from sonoranlab.repair import EditorUnavailable, Refused
from sonoranlab.repair import workspace as ws
from sonoranlab.repair.compare import diff


def run(args: argparse.Namespace) -> int:
    try:
        action = args.repair_action
        if action == "prepare":
            result = ws.prepare(args.parent_job, args.profile, args.character, args.anim, args.out)
        elif action == "finalize":
            from sonoranlab.repair.finalize import finalize

            result = finalize(args.repair, args.out)
        elif action == "diff":
            result = diff(args.repair)
        else:
            result = {"export": ws.export, "inspect": ws.inspect, "abort": ws.abort}[action](args.repair)
        code = 0 if result["ok"] else 1
    except EditorUnavailable as e:
        result, code = {"ok": False, "error": "editor_unavailable", "message": str(e)}, 1
    except (Refused, FileExistsError) as e:
        result, code = {"ok": False, "error": getattr(e, "code", "refused"), "message": str(e)}, 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as e:
        result, code = {"ok": False, "error": "io_or_usage_error", "message": str(e)}, 2
    print(json.dumps(result, sort_keys=True))
    return code
