"""Factory + registry: tạo provider theo tên, không cần biết lớp cụ thể.

Thêm provider mới: viết lớp con LLMProvider, gắn @register_provider("ten"), rồi import module đó
trong bot/providers/__init__.py.
"""
import os

from .base import LLMProvider

_REGISTRY: dict[str, type[LLMProvider]] = {}


def register_provider(name: str):
    def decorator(cls: type[LLMProvider]) -> type[LLMProvider]:
        if name in _REGISTRY:
            raise ValueError(f"Provider '{name}' đã được đăng ký")
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorator


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


def create_provider(
    name: str, model: str | None = None, api_key: str | None = None, base_url: str | None = None
) -> LLMProvider:
    """Tạo provider. Thiếu model thì dùng model mặc định của provider; thiếu api_key thì đọc
    biến môi trường riêng của provider (ví dụ ANTHROPIC_API_KEY, OPENAI_API_KEY)."""
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Không có provider '{name}'. Có: {', '.join(available_providers())}")
    model = model or cls.default_model
    if not model:
        raise ValueError(f"Provider '{name}' cần chỉ định model (LLM_MODEL hoặc --model)")
    api_key = api_key or (os.environ.get(cls.api_key_env) if cls.api_key_env else None)
    return cls(model=model, api_key=api_key, base_url=base_url)
