"""能力目录的类型定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable


CapabilityHandler = Callable[[dict[str, Any]], Any | Awaitable[Any]]


class RuntimeKind(str, Enum):
    DOMAIN_SERVICE = "domain_service"
    WORKFLOW = "workflow"
    AGENT = "agent"


class Availability(str, Enum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    PLANNED = "planned"
    RESERVED = "reserved"


class MaturityLevel(str, Enum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class RiskLevel(str, Enum):
    READ_ONLY = "A"
    INTERNAL_ACTION = "B"
    EXTERNAL_MESSAGE = "C"
    BUSINESS_CHANGE = "D"
    FINANCIAL_ACTION = "E"


@dataclass(frozen=True, kw_only=True)
class CapabilityDefinition:
    capability_id: str
    name: str
    category: str
    description: str
    maturity: MaturityLevel
    availability: Availability
    risk_level: RiskLevel = RiskLevel.READ_ONLY
    tags: tuple[str, ...] = ()
    handler: CapabilityHandler | None = field(default=None, repr=False, compare=False)
    execution_endpoint: str = ""
    runtime: RuntimeKind = field(init=False)

    @property
    def executable(self) -> bool:
        return self.handler is not None and self.availability in {
            Availability.AVAILABLE,
            Availability.PARTIAL,
        }

    def execution_details(self) -> dict[str, Any]:
        return {}

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.capability_id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "runtime": self.runtime.value,
            "maturity": self.maturity.value,
            "availability": self.availability.value,
            "risk_level": self.risk_level.value,
            "tags": list(self.tags),
            "executable": self.executable,
            "execution_endpoint": self.execution_endpoint,
            "execution": self.execution_details(),
        }


@dataclass(frozen=True, kw_only=True)
class DomainServiceDefinition(CapabilityDefinition):
    runtime: RuntimeKind = field(default=RuntimeKind.DOMAIN_SERVICE, init=False)
    input_fields: tuple[str, ...] = ()
    output_fields: tuple[str, ...] = ()
    deterministic: bool = True

    def execution_details(self) -> dict[str, Any]:
        return {
            "deterministic": self.deterministic,
            "input_fields": list(self.input_fields),
            "output_fields": list(self.output_fields),
        }


@dataclass(frozen=True, kw_only=True)
class WorkflowDefinition(CapabilityDefinition):
    runtime: RuntimeKind = field(default=RuntimeKind.WORKFLOW, init=False)
    triggers: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    supports_dry_run: bool = True

    def execution_details(self) -> dict[str, Any]:
        return {
            "triggers": list(self.triggers),
            "steps": list(self.steps),
            "supports_dry_run": self.supports_dry_run,
        }


@dataclass(frozen=True, kw_only=True)
class AgentScenarioDefinition(CapabilityDefinition):
    runtime: RuntimeKind = field(default=RuntimeKind.AGENT, init=False)
    tools: tuple[str, ...] = ()
    guardrails: tuple[str, ...] = ()

    def execution_details(self) -> dict[str, Any]:
        return {
            "tools": list(self.tools),
            "guardrails": list(self.guardrails),
        }


@dataclass(frozen=True, kw_only=True)
class ModuleDefinition:
    module_id: str
    name: str
    group: str
    description: str
    runtimes: tuple[RuntimeKind, ...]
    maturity: MaturityLevel
    availability: Availability
    web_path: str
    capability_ids: tuple[str, ...] = ()

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.module_id,
            "name": self.name,
            "group": self.group,
            "description": self.description,
            "runtimes": [runtime.value for runtime in self.runtimes],
            "maturity": self.maturity.value,
            "availability": self.availability.value,
            "web_path": self.web_path,
            "capability_ids": list(self.capability_ids),
        }
