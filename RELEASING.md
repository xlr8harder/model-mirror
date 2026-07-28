# Releasing model-mirror

Normal Git pushes are development builds. A release is an immutable package
version plus the matching annotated Git tag.

## One-time PyPI setup

1. Create the `model-mirror` project on PyPI, or configure a pending publisher
   for the first release.
2. Add a PyPI trusted publisher for:
   - owner: `xlr8harder`
   - repository: `model-mirror`
   - workflow: `release.yml`
   - environment: `pypi`
3. Create a protected `pypi` environment in the GitHub repository.

The release workflow uses OpenID Connect trusted publishing. Do not add a
long-lived PyPI token to the repository.

## Release

Select the next version according to the change:

- patch: compatible bug or documentation fix
- minor: compatible user-facing feature
- major: incompatible stable-interface change

While the project is pre-1.0, substantial CLI or metadata compatibility changes
normally warrant a minor release.

```bash
uv version 0.2.0
uv run coverage run -m pytest -q
uv run coverage report -m
uv build --no-sources
uv run --isolated --no-project --with dist/*.whl tests/smoke_test.py
uv run --isolated --no-project --with dist/*.tar.gz tests/smoke_test.py
git add pyproject.toml uv.lock
git commit -m "Release 0.2.0"
git tag -a v0.2.0 -m v0.2.0
git push origin main
git push origin v0.2.0
```

The tag must exactly equal `v` plus the version in `pyproject.toml`. The GitHub
workflow repeats the full test, build, and smoke-test sequence before publishing
the wheel and source distribution.

## User installation and updates

```bash
uv tool install model-mirror
uv tool install 'model-mirror[torrent]'
model-mirror --version
uv tool upgrade model-mirror
```
