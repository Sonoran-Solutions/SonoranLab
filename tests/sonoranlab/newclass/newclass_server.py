"""Toy admission stub shared by the test process and the stdio server subprocess (importable without pytest)."""

from sonoranlab import classreq
from sonoranlab.api.tools import classreq as classreq_tool

real_check = classreq_tool.check_class_request


def toy_check(*args, **kwargs):
    """The 24 px toy stills are below the class-request cell range: drop only the `still_*` pixel findings, keep
    missing stills, clip, profile-row and registry findings (real admission is covered in tests/.../classreq)."""
    result = real_check(*args, **kwargs)
    result["findings"] = [f for f in result["findings"] if not f["code"].startswith("still_")]
    result["status"] = classreq._status(result["findings"])
    return result


def install(ctx=None) -> None:
    """Admission stub, plus (with ctx) the synthetic repository for every module that bound `Context` at import."""
    import importlib
    import sys

    classreq_tool.check_class_request = toy_check
    if ctx is None:
        return
    boundary = importlib.import_module("sonoranlab.api.registry")
    for name in ("sonoranlab.jobs.kinds.smoke", "sonoranlab.jobs.kinds.deterministic", "newclass_fakes"):
        importlib.import_module(name)
    for name, module in list(sys.modules.items()):
        if (name.startswith("sonoranlab.") or name == "newclass_fakes") and getattr(module, "Context", None) is type(
            ctx
        ):
            module.Context = lambda *a, **k: ctx
    boundary.Context = lambda *a, **k: ctx
