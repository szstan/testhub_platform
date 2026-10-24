"""Versioned plugin manifests and safe in-process registration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


class PluginError(RuntimeError):
    pass


@dataclass(frozen=True)
class PluginManifest:
    name: str
    version: str
    kind: str
    capabilities: tuple[str, ...] = ()
    schema: Mapping[str, Any] = field(default_factory=dict)
    permissions: tuple[str, ...] = ()
    enabled: bool = True


@dataclass
class RegisteredPlugin:
    manifest: PluginManifest
    implementation: Any = None


class PluginRegistry:
    """Explicit registry; plugin failures cannot break core imports."""

    def __init__(self):
        self._plugins: dict[tuple[str, str], RegisteredPlugin] = {}

    def register(self, manifest: PluginManifest, implementation=None):
        if not manifest.name or not manifest.version or not manifest.kind:
            raise PluginError('Plugin manifest requires name, version and kind')
        key = (manifest.name, manifest.version)
        if key in self._plugins:
            raise PluginError(f'Plugin already registered: {manifest.name}@{manifest.version}')
        self._plugins[key] = RegisteredPlugin(manifest, implementation)
        return manifest

    def disable(self, name: str, version: str):
        plugin = self.get(name, version)
        plugin.manifest = PluginManifest(
            name=plugin.manifest.name,
            version=plugin.manifest.version,
            kind=plugin.manifest.kind,
            capabilities=plugin.manifest.capabilities,
            schema=plugin.manifest.schema,
            permissions=plugin.manifest.permissions,
            enabled=False,
        )

    def get(self, name: str, version: str | None = None) -> RegisteredPlugin:
        candidates = [plugin for (plugin_name, _), plugin in self._plugins.items() if plugin_name == name]
        if version is not None:
            candidates = [plugin for plugin in candidates if plugin.manifest.version == version]
        if not candidates:
            raise PluginError(f'Plugin not found: {name}@{version or "*"}')
        return sorted(candidates, key=lambda item: item.manifest.version)[-1]

    def require(self, name: str, version: str | None = None, permissions=()):
        plugin = self.get(name, version)
        if not plugin.manifest.enabled:
            raise PluginError(f'Plugin disabled: {name}@{plugin.manifest.version}')
        missing = set(permissions) - set(plugin.manifest.permissions)
        if missing:
            raise PluginError(f'Plugin permission denied: {sorted(missing)}')
        return plugin

    def manifests(self, kind: str | None = None):
        values = [plugin.manifest for plugin in self._plugins.values()]
        if kind:
            values = [manifest for manifest in values if manifest.kind == kind]
        return sorted(values, key=lambda manifest: (manifest.kind, manifest.name, manifest.version))


DEFAULT_PLUGIN_REGISTRY = PluginRegistry()
for _manifest in (
    PluginManifest('execution.adapter', '1', 'executor', ('submit', 'cancel', 'retry', 'collect')),
    PluginManifest('execution.result-events', '1', 'reporter', ('events', 'artifacts', 'history')),
    PluginManifest('core.notifications', '1', 'notifier', ('email', 'webhook')),
):
    DEFAULT_PLUGIN_REGISTRY.register(_manifest)


__all__ = ['DEFAULT_PLUGIN_REGISTRY', 'PluginError', 'PluginManifest', 'PluginRegistry', 'RegisteredPlugin']
