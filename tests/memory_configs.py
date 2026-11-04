"""In-memory configuration versions for store fakes; mirrors Store's version rules."""

from uuid import uuid4

from mel.bundle import definition_sha256


class MemoryConfigs:
    def __init__(self):
        super().__init__()
        self.configs = {}
        self.active = None

    def save_config(self, definition, *, parent_id=None, note=None, name=None):
        digest = definition_sha256(definition)
        for key, record in self.configs.items():
            if record["definition_sha256"] == digest:
                return key
        key = str(uuid4())
        self.configs[key] = {
            "config_id": key,
            "parent_id": parent_id,
            "note": note,
            "name": name,
            "status": "draft",
            "definition_sha256": digest,
            "definition": definition,
        }
        return key

    def config(self, key):
        return self.configs.get(key)

    def active_config_id(self):
        return self.active

    def name_config(self, key, name, note=None):
        if key not in self.configs:
            raise ValueError("unknown config_id")
        self.configs[key].update(name=name, status="saved")

    def activate_config(self, key):
        if key not in self.configs:
            raise ValueError("unknown config_id")
        if self.configs[key]["status"] == "draft":
            raise ValueError("save this version before activating it")
        self.active = key
