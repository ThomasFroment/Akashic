# Packaging

## Distribution decision

The app must be self-contained on Windows: no separately installed Python, ML runtime or helper
application. The external prerequisites are Windows (including its OCR language packs) and the
NVIDIA driver.

Use a relocatable interpreter with locked dependencies. This keeps native libraries in a known
layout instead of maintaining a growing freezer configuration for each ML and Windows binding.
A copied development virtual environment is not a portable distribution.

Models and the inference runtime can be provisioned separately through explicit verified
downloads or local copies. Once provisioned, operation must be fully offline. Startup must not
fetch missing files, and the bundle must not depend on the development checkout or user caches.

## Validation

The build entry point is [packaging/build.ps1](../packaging/build.ps1); read it for current
options and output layout. Preserve locked artifact sources and integrity checks through the
build, including native GPU dependencies.

A dependency export or install dry run is not a complete bundle test. Validate relocation,
process startup, UI assets, storage migrations and real inference on a clean Windows install.
Earlier portable smoke checks predate the expanded speech stack; a fresh complete build and
clean-machine validation remain open.
