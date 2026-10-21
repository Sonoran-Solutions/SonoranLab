"""Production job kinds. Each area module exports EXECUTORS: dict[kind, Executor]; add it to MODULES (lazy import)."""

MODULES: tuple[str, ...] = ("generation", "deterministic")
