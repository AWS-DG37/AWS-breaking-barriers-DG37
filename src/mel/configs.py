"""Agent configuration versions: the saved, immutable settings every run is tied to."""

from __future__ import annotations

from mel.bundle import Bundle
from mel.config import Settings


def active_config(store, settings: Settings) -> tuple[str, Bundle]:
    """The active version. Until one is activated, the YAML configuration is saved (or
    matched to its existing version) and activated; afterwards YAML is only an import
    source. Activation only ever switches versions, so this happens once per database."""
    key = store.active_config_id()
    if key is None:
        key = store.save_config(Bundle.load(settings).definition, note="Seeded from YAML")
        store.activate_config(key)
    return key, config_bundle(store, key)


def config_bundle(store, config_id: str) -> Bundle:
    record = store.config(config_id)
    if record is None:
        raise ValueError(f"Unknown configuration version {config_id}")
    return Bundle.from_definition(record["definition"])
