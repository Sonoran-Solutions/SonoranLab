"""Area modules export TOOLS; later tickets only extend this list."""

from . import generation, jobs, projects, registry_tools, system

MODULES = (system, projects, registry_tools, jobs, generation)
