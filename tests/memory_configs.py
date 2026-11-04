"""In-memory configuration versions for store fakes; mirrors Store's version rules."""

from uuid import uuid4

from mel.bundle import definition_sha256


class MemoryConfigs:
    def __init__(self):
        super().__init__()
        self.configs = {}
        self.active = None

    def save_config(self, definition, *, parent_id=None, note=None):
        digest = definition_sha256(definition)
        for key, record in self.configs.items():
            if record["definition_sha256"] == digest:
                return key
        key = str(uuid4())
        self.configs[key] = {
            "config_id": key,
            "parent_id": parent_id,
            "note": note,
            "definition_sha256": digest,
            "definition": definition,
        }
        return key

    def config(self, key):
        return self.configs.get(key)

    def config_count(self):
        return len(self.configs)

    def active_config_id(self):
        return self.active

    def activate_config(self, key):
        if key not in self.configs:
            raise ValueError("unknown config_id")
        self.active = key
