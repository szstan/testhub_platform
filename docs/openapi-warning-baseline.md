# OpenAPI Warning Baseline

The CI performance helper runs `scripts/ci_schema_check.py` after Django checks and migration drift checks.

The schema gate fails on these regressions:

- `operationId` collisions
- Multiple generated names for the same choice set
- Duplicate-choice naming warnings
- New diagnostic categories that are not classified in the baseline

The current baseline explicitly allows existing serializer/APIView resolution diagnostics:

- Serializer method type hints that drf-spectacular cannot infer
- APIView serializer inference and queryset model inference
- Untyped path parameters and custom authenticators
- Existing duplicate component names emitted by serializers
- Existing serializer-class lookup exceptions during schema introspection

The allowed categories are intentionally broad enough to preserve the current API surface, but the baseline is fail-closed for new warning categories. When a warning is fixed, remove or narrow its matching allowance and keep the focused schema command in CI.

Run locally with:

```bash
python scripts/ci_schema_check.py
```
