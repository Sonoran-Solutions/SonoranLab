"""Area modules export TOOLS; later tickets only extend this list."""

from . import campaign, classreq, deterministic, generation, jobs, projects, registry_tools, repair, system

MODULES = (system, projects, registry_tools, classreq, jobs, generation, deterministic, repair, campaign)
