"""Executor boundary. Production kinds are registered by their operation modules, lazily."""

from __future__ import annotations

import importlib
import os
from typing import Protocol


class JobFailure(Exception):
    def __init__(self, code: str, stage: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.error = {"code": code, "stage": stage, "message": message, "retryable": retryable}


class JobCancelled(Exception):
    """Cooperative executor cancellation; the worker still confirms prompt outcomes."""


class Executor(Protocol):
    gpu: bool
    heavy_cpu: bool

    def need_gib(self, params: dict) -> float | None: ...
    def validate(self, params: dict) -> tuple[dict, dict]: ...
    def run(self, job: dict, ctx) -> dict: ...
    def stage(self, job: dict) -> dict: ...


EXECUTORS: dict[str, Executor] = {}


def executor(kind: str) -> Executor:
    if kind not in EXECUTORS:
        from sonoranlab.jobs.kinds import MODULES

        for name in MODULES:
            EXECUTORS.update(importlib.import_module(f"sonoranlab.jobs.kinds.{name}").EXECUTORS)
    # Test-only, just like SONORANLAB_PROC: never accept module names in tool parameters.
    if module := os.environ.get("SONORANLAB_JOB_EXECUTORS"):
        EXECUTORS.update(importlib.import_module(module).EXECUTORS)
    try:
        return EXECUTORS[kind]
    except KeyError:
        from sonoranlab.api.registry import ToolError

        raise ToolError("invalid_params", f"Unknown job kind: {kind}", field="kind") from None
