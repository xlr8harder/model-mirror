"""Mirror and validate Hugging Face model archives."""

from importlib.metadata import version

__all__ = ["__version__"]

__version__ = version("model-mirror-cli")
