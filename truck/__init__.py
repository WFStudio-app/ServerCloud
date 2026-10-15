"""TruckOS core package — kernel space, modules and update daemons.

Repo layout (main plane):

    kernel/         ring-0 sources + VERSION.data / RELEASE.data markers
    truck_modules/  kernel modules: trk, sysconfig, update engine/markers
    security/       security database + SECURITY.data marker
    upd_file/       .txt/.data mirror of the markers served to Auto-Update
    servercloud/    application userland (Terminal / LLM Local / Scripts)

Canonical imports:

    import truck                      # loads everything below
    from truck import kernel           # or: import kernel  (via alias)
    from truck.modules import trk, update_engine

Legacy dotted imports (still valid after this move):

    from servercloud.kernel import truck_kernel
    from servercloud.truck_modules import trk
"""

import os
import sys
import types
import importlib.util

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _load_pkg(name, dirname):
    """Load <root>/<dirname>/__init__.py as package `name`."""
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(_ROOT, dirname)
    init = os.path.join(path, "__init__.py")
    if not os.path.exists(init):
        mod = types.ModuleType(name)          # namespace-only package
        mod.__path__ = [path]
        mod.__package__ = name
        sys.modules[name] = mod
        return mod
    spec = importlib.util.spec_from_file_location(
        name, init, submodule_search_locations=[path])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod                    # registered BEFORE exec
    spec.loader.exec_module(mod)
    return mod


# Order matters: modules first (kernel code does `from truck.modules ...`),
# then kernel, then the data-only security namespace.
modules = _load_pkg("truck.modules", "truck_modules")
kernel = _load_pkg("truck.kernel", "kernel")
security = _load_pkg("truck.security", "security")

# Make plain `import kernel`, `import truck_modules`, `import security`
# work from the repo root by mapping the bare directory names onto the
# packages already loaded above.
sys.modules.setdefault("kernel", kernel)
sys.modules.setdefault("truck_modules", modules)
sys.modules.setdefault("security", security)

# --- legacy aliases so old dotted imports keep working ----------------------
try:
    import servercloud  # noqa: F401  (real app package)
except ImportError:
    pass
sys.modules.setdefault("servercloud.kernel", kernel)
sys.modules.setdefault("servercloud.truck_modules", modules)
sys.modules.setdefault("servercloud.security", security)
for _sub in ("trk", "sysconfig", "update_engine", "update_markers"):
    sys.modules.setdefault(f"servercloud.truck_modules.{_sub}",
                           getattr(modules, _sub))
sys.modules.setdefault("servercloud.truck_modules.truck_update",
                       modules.update_engine)
# attribute-style legacy name: `from servercloud.truck_modules import truck_update`
if not hasattr(modules, "truck_update"):
    modules.truck_update = modules.update_engine

trk = modules.trk
sysconfig = modules.sysconfig
SysConfig = modules.SysConfig
update_engine = modules.update_engine
update_markers = modules.update_markers

__all__ = ["kernel", "modules", "security", "trk", "sysconfig", "SysConfig",
           "update_engine", "update_markers"]
