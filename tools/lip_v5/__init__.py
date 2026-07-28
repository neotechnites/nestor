"""
lip_v5 — presence-portfolio maker (spec-lip-v5.md).  STAGED-INERT.

Import order matters exactly once: `config` and `runtime` have no intra-package dependencies,
everything else depends on them, and `ws_feed` is vendored from v4 with `runtime` standing in
for its old host module.  Nothing here imports `lip_maker_v4`: v4 is FROZEN and deployed
separately, so a live coupling would mean a v4 edit silently changing v5.
"""

__version__ = "5.0.0-staged-inert"
