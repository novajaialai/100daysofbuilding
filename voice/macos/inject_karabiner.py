#!/usr/bin/env python3
"""Inject the 'Right-Command tap -> F18' rule into the active Karabiner profile.
Idempotent: re-running replaces the existing rule rather than duplicating it.
Requires Karabiner to have been launched once (so karabiner.json exists)."""
import json, os

p = os.path.expanduser("~/.config/karabiner/karabiner.json")
rule = {
    "description": "Right Command alone -> F18 (voice dictation); still Command when held with other keys",
    "manipulators": [{
        "type": "basic",
        "from": {"key_code": "right_command", "modifiers": {"optional": ["any"]}},
        "to": [{"key_code": "right_command", "lazy": True}],
        "to_if_alone": [{"key_code": "f18"}],
    }],
}
with open(p) as f:
    cfg = json.load(f)
profs = cfg.setdefault("profiles", [])
if not profs:
    profs.append({"name": "Default profile", "selected": True})
prof = next((x for x in profs if x.get("selected")), profs[0])
cm = prof.setdefault("complex_modifications", {})
rules = [r for r in cm.get("rules", []) if "F18 (voice dictation)" not in r.get("description", "")]
rules.insert(0, rule)
cm["rules"] = rules
with open(p, "w") as f:
    json.dump(cfg, f, indent=4, ensure_ascii=False)
print("injected into profile:", prof.get("name"), "| total rules:", len(rules))
