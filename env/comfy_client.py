"""Run a ComfyUI API workflow from stdin and emit one JSON result."""

import argparse
import json
import math
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None, stdin=None, stdout=None) -> int:
    started = time.monotonic()
    result = {
        "status": "http_error", "exit_code": 6, "prompt_id": None,
        "wall_s": 0.0, "images": [], "errors": [], "detail": "",
    }

    def finish(code, status, detail):
        result.update(exit_code=code, status=status, detail=detail,
                      wall_s=round(time.monotonic() - started, 3))
        print(json.dumps(result), file=stdout if stdout is not None else sys.stdout)
        return code

    try:
        parser = ArgumentParser(description=__doc__, add_help=False)
        parser.add_argument("--base", default="http://127.0.0.1:8188")
        parser.add_argument("--timeout", type=float, default=1800)
        parser.add_argument("--http-timeout", type=float, default=15)
        parser.add_argument("--poll", type=float, default=0.5)
        parser.add_argument("--expect-images", type=int, default=1)
        parser.add_argument("--output-dir")
        args = parser.parse_args(argv)
        for name in ("timeout", "http_timeout", "poll"):
            value = getattr(args, name)
            if not math.isfinite(value) or value < 0 or (name != "poll" and value == 0):
                raise ValueError(f"Invalid --{name.replace('_', '-')}")
        if args.expect_images < 0:
            raise ValueError("--expect-images must be nonnegative")
        deadline = started + args.timeout

        def request(path, body=None, *, cleanup=False):
            remaining = deadline - time.monotonic()
            if not cleanup and remaining <= 0:
                raise TimeoutError("Overall deadline exceeded")
            # Cap request time at the remaining run budget; cleanup gets a short grace.
            timeout = min(args.http_timeout, 0.1 if cleanup else remaining)
            req = urllib.request.Request(
                args.base.rstrip("/") + path,
                data=None if body is None else json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                if cleanup:
                    return None
                return json.load(response)

        def timed_out():
            if result["prompt_id"] is not None:
                # Both calls are scoped to our prompt: a bare /interrupt would stop whatever job is running,
                # which may belong to another client by the time our deadline fires.
                for path, body in (("/interrupt", {"prompt_id": result["prompt_id"]}),
                                   ("/queue", {"delete": [result["prompt_id"]]})):
                    try:
                        request(path, body, cleanup=True)
                    except Exception:
                        pass
            return finish(4, "timeout", "Overall deadline exceeded")

        workflow = json.load(stdin if stdin is not None else sys.stdin)
        if not isinstance(workflow, dict):
            raise ValueError("Workflow must be a JSON object")
        if time.monotonic() >= deadline:
            return timed_out()
        try:
            submitted = request("/prompt", {"prompt": workflow, "client_id": str(uuid.uuid4())})
        except urllib.error.HTTPError as exc:
            with exc:
                detail = exc.read(8000).decode("utf-8", errors="replace")[:2000]
            return finish(2, "rejected", detail)
        except (urllib.error.URLError, OSError):
            if time.monotonic() >= deadline:
                return timed_out()
            raise
        if not isinstance(submitted, dict) or not isinstance(submitted.get("prompt_id"), str) or not submitted["prompt_id"]:
            raise ValueError("Missing or invalid prompt_id in /prompt response")
        result["prompt_id"] = submitted["prompt_id"]
        history_path = "/history/" + urllib.parse.quote(result["prompt_id"], safe="")
        while True:
            if time.monotonic() >= deadline:
                return timed_out()
            try:
                history = request(history_path)
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, OSError):
                history = {}
            if time.monotonic() >= deadline:
                return timed_out()
            if not isinstance(history, dict):
                raise ValueError("History response must be a JSON object")
            if result["prompt_id"] in history:
                entry = history[result["prompt_id"]]
                break
            time.sleep(min(args.poll, max(0, deadline - time.monotonic())))

        status = entry.get("status", {})
        result["errors"] = [message for message in status.get("messages", [])
                            if message[0] in ("execution_error", "execution_interrupted")]
        for output in entry.get("outputs", {}).values():
            for image in output.get("images", []):
                subfolder, filename = image.get("subfolder", ""), image["filename"]
                if not isinstance(subfolder, str) or not isinstance(filename, str):
                    raise ValueError("Image subfolder and filename must be strings")
                result["images"].append(f"{subfolder}/{filename}" if subfolder else filename)
        if status.get("status_str") != "success" or status.get("completed") is not True or result["errors"]:
            return finish(3, "execution_error", "Execution did not complete successfully")
        if len(result["images"]) < args.expect_images:
            return finish(5, "missing_outputs", f"Expected at least {args.expect_images} images; got {len(result['images'])}")
        if args.output_dir:
            root = Path(args.output_dir).resolve()
            for image in result["images"]:
                path = (root / image).resolve()
                if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size == 0:
                    return finish(5, "missing_outputs", f"Missing, empty, or outside output directory: {image}")
        if time.monotonic() >= deadline:
            return timed_out()
        return finish(0, "success", "Execution completed and outputs validated")
    except Exception as exc:
        return finish(6, "http_error", f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
