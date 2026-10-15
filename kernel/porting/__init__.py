"""TruckOS porting layer — cross-platform support for the kernel.

Public API:
    from truck import kernel
    kernel.porting.PORT_MATRIX          # certified target matrix (20+)
    kernel.porting.detect_host()        # HAL bound to this machine
    kernel.porting.certification_report()
    kernel.porting.scaffold_port(...)   # template for new platforms
    kernel.porting.kernel_languages()   # AST-based language probe
"""

from .ports import (PORT_MATRIX, VENDORS, DESKTOPS_SERVERS, PHONES,  # noqa: F401
                    PortMatrix, Target, Urgency, certification_report)
from .hal import HAL, HostHAL, detect_host                          # noqa: F401
from .langs import kernel_languages, format_languages           # noqa: F401
from .template import list_templates, scaffold_port                 # noqa: F401
