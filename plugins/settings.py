import json
import os

import pcbnew

SETTINGS_FILE = os.path.join(pcbnew.SETTINGS_MANAGER.GetUserSettingsPath(), "vtools.json")

# All lengths are in mm.
DEFAULTS = {
    "position": None,
    "fencing": {
        "grid": 0.25,
        "distance": 0.5,
        "min_distance": 0.3,
        "use_min_distance": False,
        "net": "GND",
        "hole_size": 0.2,
        "diameter": 0.4,
        "same_net": False,
    },
    "stitching": {
        "grid": 1,
        "clearance": 0.5,
        "min_clearance": 0.3,
        "net": "GND",
        "hole_size": 0.2,
        "diameter": 0.4,
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
