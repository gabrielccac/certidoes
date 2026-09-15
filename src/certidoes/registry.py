from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

_SPECS = {
    "receita": ("certidoes.sources.receita", "ReceitaRequest"),
    "trf1": ("certidoes.sources.trf1", "Trf1Request"),
    "tjdft": ("certidoes.sources.tjdft", "TjdftRequest"),
    "fazenda": ("certidoes.sources.fazenda", "FazendaRequest"),
    "trt10": ("certidoes.sources.trt10", "Trt10Request"),
    "tst": ("certidoes.sources.tst", "TstRequest"),
}


@dataclass(frozen=True)
class SourceSpec:
    """One registered certificate source: request type + generator function."""

    name: str
    request_cls: Any
    generate: Callable


_CACHE: dict[str, SourceSpec] = {}


def get_source(name: str) -> SourceSpec:
    """Resolve a source, importing its module on first use.

    Lazy on purpose: the ingest API image only installs the transport
    dependencies, not the per-source extras (OCR, browser drivers).
    """
    if name in _CACHE:
        return _CACHE[name]
    try:
        module_path, request_name = _SPECS[name]
    except KeyError:
        raise ValueError(f"unknown source {name!r}") from None
    import importlib

    module = importlib.import_module(module_path)
    spec = SourceSpec(name, getattr(module, request_name), getattr(module, "generate"))
    _CACHE[name] = spec
    return spec


def known_sources() -> tuple[str, ...]:
    return tuple(_SPECS)
