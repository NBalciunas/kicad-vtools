import json
import os

import pcbnew

SETTINGS_FILE = os.path.join(pcbnew.SETTINGS_MANAGER.GetUserSettingsPath(), "vtools.json")

# All lengths are in mm.
DEFAULTS = {
    "position": None,
    "fencing": {
        "net": "GND",
        "grid": 0.6,
        "distance": 0.5,
        "use_min_distance": False,
        "min_distance": 0.3,
        "diameter": None,
        "hole_size": None,
        "same_net": False,
    },
}


def load_settings():
    # The function uses only the keys that are in DEFAULTS. It ignores old or unknown keys.
    settings = json.loads(json.dumps(DEFAULTS))
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as file:
            saved = json.load(file)
    except (OSError, ValueError):
        return settings
    for key, value in saved.items():
        if isinstance(settings.get(key), dict) and isinstance(value, dict):
            settings[key].update({name: item for name, item in value.items() if name in settings[key]})
        elif key in settings:
            settings[key] = value
    return settings


def save_settings(settings):
    # If the plugin cannot write the file, it continues without saved settings.
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as file:
            json.dump(settings, file, indent=2)
    except OSError:
        pass
