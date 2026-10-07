"""Small explicit validator, not a general JSON Schema engine."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load(name):
    return json.loads((ROOT / name).read_text())


def validate(tool, arguments, contract=None):
    contract = contract or load("contracts.json")
    if tool not in contract["tools"]:
        raise ValueError("Unknown tool")
    schema = contract["tools"][tool]["input"]
    if type(arguments) is not dict:
        raise ValueError("Arguments must be an object")
    if set(arguments) != set(schema["required"]):
        raise ValueError("Missing or extra arguments")
    for key, rule in schema["properties"].items():
        value = arguments[key]
        if rule["type"] == "integer":
            if type(value) is not int or value < rule.get("minimum", 0):
                raise ValueError(f"{key} must be an integer >= {rule.get('minimum', 0)}")
        elif rule["type"] == "string":
            if type(value) is not str or len(value) < rule.get("minLength", 0):
                raise ValueError(f"{key} must be a nonempty string")
        else:
            raise ValueError("Unsupported contract schema type")
