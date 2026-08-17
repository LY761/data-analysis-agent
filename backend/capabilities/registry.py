"""能力和 Web 模块注册表。"""

from __future__ import annotations

import inspect
from collections import Counter
from typing import Any

from capabilities.models import CapabilityDefinition, ModuleDefinition, RuntimeKind


class CapabilityRegistryError(Exception):
    pass


class CapabilityNotFoundError(CapabilityRegistryError):
    pass


class CapabilityUnavailableError(CapabilityRegistryError):
    pass


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, CapabilityDefinition] = {}
        self._modules: dict[str, ModuleDefinition] = {}

    def register_capability(self, definition: CapabilityDefinition) -> None:
        if definition.capability_id in self._capabilities:
            raise CapabilityRegistryError(f"能力已注册: {definition.capability_id}")
        self._capabilities[definition.capability_id] = definition

    def register_module(self, definition: ModuleDefinition) -> None:
        if definition.module_id in self._modules:
            raise CapabilityRegistryError(f"模块已注册: {definition.module_id}")
        self._modules[definition.module_id] = definition

    def get_capability(self, capability_id: str) -> CapabilityDefinition:
        try:
            return self._capabilities[capability_id]
        except KeyError as error:
            raise CapabilityNotFoundError(f"能力不存在: {capability_id}") from error

    def get_module(self, module_id: str) -> ModuleDefinition:
        try:
            return self._modules[module_id]
        except KeyError as error:
            raise CapabilityNotFoundError(f"模块不存在: {module_id}") from error

    def list_capabilities(self, runtime: RuntimeKind | None = None) -> list[CapabilityDefinition]:
        definitions = self._capabilities.values()
        if runtime is not None:
            definitions = (definition for definition in definitions if definition.runtime == runtime)
        return sorted(definitions, key=lambda definition: definition.capability_id)

    def list_modules(self) -> list[ModuleDefinition]:
        return sorted(self._modules.values(), key=lambda definition: definition.module_id)

    async def execute(self, capability_id: str, inputs: dict[str, Any] | None = None) -> Any:
        definition = self.get_capability(capability_id)
        if not definition.executable or definition.handler is None:
            raise CapabilityUnavailableError(f"能力当前不可通过统一入口执行: {capability_id}")

        result = definition.handler(dict(inputs or {}))
        if inspect.isawaitable(result):
            result = await result
        return result

    def catalog(self) -> dict[str, Any]:
        capabilities = self.list_capabilities()
        modules = self.list_modules()
        runtime_counts = Counter(definition.runtime.value for definition in capabilities)
        availability_counts = Counter(definition.availability.value for definition in capabilities)
        return {
            "schema_version": "1.0",
            "principles": {
                "deterministic_first": True,
                "workflow_controls_side_effects": True,
                "agent_direct_side_effects": False,
            },
            "summary": {
                "module_count": len(modules),
                "capability_count": len(capabilities),
                "runtime_counts": dict(runtime_counts),
                "availability_counts": dict(availability_counts),
            },
            "modules": [definition.to_public_dict() for definition in modules],
            "capabilities": [definition.to_public_dict() for definition in capabilities],
        }
