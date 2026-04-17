"""
core/auth.py — re-export shim.

Blueprints import auth decorators from here. The actual implementations
live in api/middleware.py. This indirection keeps the Blueprint import
paths clean and decoupled from the api/ package structure.
"""
from api.middleware import require_auth, require_super_admin  # noqa: F401
