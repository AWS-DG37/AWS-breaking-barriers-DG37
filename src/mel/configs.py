"""Agent configuration versions: the saved, immutable settings every run is tied to."""

from __future__ import annotations

from mel.bundle import Bundle
from mel.config import Settings


def active_config(store, settings: Settings) -> tuple[str, Bundle]:
    """The active version. An empty registry is seeded once from the YAML configuration,
    which becomes the first active version; afterwards YAML is only an import source."""
    if store.active_config_id() is None and store.config_count() == 0:
        key = store.save_config(Bundle.load(settings).definition, note="Seeded from YAML")
        store.activate_config(key)
    key = store.active_config_id()
    if key is None:
        raise ValueError("No active configuration version; activate one first")
    return key, config_bundle(store, key)


def config_bundle(store, config_id: str) -> Bundle:
    record = store.config(config_id)
    if record is None:
        raise ValueError(f"Unknown configuration version {config_id}")
    return Bundle.from_definition(record["definition"])
