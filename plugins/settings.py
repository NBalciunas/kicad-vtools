import json
import os

import pcbnew

SETTINGS_FILE = os.path.join(pcbnew.SETTINGS_MANAGER.GetUserSettingsPath(), "vtools.json")

# All lengths are in mm. The window shows them in the selected units.
DEFAULTS = {
    "units": "mm",
    "position": None,
    "fencing": {
        "via_spacing": 1.27,
        "via_gap": 0.5,
        "use_min_gap": False,
        "min_gap": 0.3,
        "via_diameter": None,
        "via_drill": None,
        "net_name": "GND",
        "staggered": False,
        "same_net": False,
        "fill_gaps": True,
        "group_name": "Via Fencing",
    },
}


def load_settings():
    settings = json.loads(json.dumps(DEFAULTS))
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as file:
            saved = json.load(file)
    except (OSError, ValueError):
        return settings
    for key, value in saved.items():
        if isinstance(settings.get(key), dict) and isinstance(value, dict):
            settings[key].update(value)
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
