# -*- coding: utf-8 -*-
"""test_spec_schema.py - spec/mqtt.json must match spec/mqtt.schema.json

The contract file is the interface every consumer is generated from, and until now nothing
checked its *shape*: test_mqtt_contract.py checks it against the Python library and the
firmware, gen_* checks it against the generated headers, but a topic that forgot the
{device_id} token or a numeric field that lost its unit was only caught by a hand-written
assertion about that particular key.

The schema is the other half: it describes what any valid contract looks like, so a new
topic or field is covered the moment it is written. The last three tests exist to keep the
schema from becoming decorative - each one breaks a copy of the contract in a way the
schema claims to catch and asserts that it does.

Needs the `jsonschema` package (requirements-dev.txt). Without it the tests skip rather
than fail, so a machine with only the runtime dependencies still gets the rest of the suite.
"""
from __future__ import annotations

import copy
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = ROOT / "spec" / "mqtt.json"
SCHEMA = ROOT / "spec" / "mqtt.schema.json"

try:
    import jsonschema
except ImportError:                                              # pragma: no cover
    jsonschema = None


def load():
    return json.loads(SPEC.read_text(encoding="utf-8"))


def errors_for(doc: dict) -> list:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return list(jsonschema.Draft202012Validator(schema).iter_errors(doc))


@unittest.skipIf(jsonschema is None,
                 "jsonschema is not installed: pip install -r requirements-dev.txt")
class TestContractSchema(unittest.TestCase):
    def test_the_schema_is_a_valid_draft_2020_12_schema(self):
        # A schema with a typo in its own keywords validates everything and proves nothing.
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        self.assertEqual(schema["$schema"],
                         "https://json-schema.org/draft/2020-12/schema")

    def test_the_contract_matches_the_schema(self):
        problems = sorted(errors_for(load()), key=lambda e: list(e.path))
        self.assertEqual(problems, [], "\n".join(
            "%s: %s" % ("/".join(str(p) for p in e.path), e.message) for e in problems))

    # ---- the schema has to bite -------------------------------------------------
    def test_rejects_a_topic_without_the_device_token(self):
        # Two heaters on one broker would share a topic. The property is stated in the
        # schema rather than in a per-topic assertion, because topics are added freely.
        doc = load()
        doc["topics"]["state"]["topic"] = "btwh/state"
        self.assertTrue(errors_for(doc), "a topic without {device_id} was accepted")

    def test_rejects_a_numeric_field_without_a_unit(self):
        doc = load()
        field = next(f for f in doc["state_fields"] if f["type"] == "float")
        field.pop("unit")
        self.assertTrue(errors_for(doc), "%r lost its unit and was accepted" % field["key"])

    def test_rejects_an_unknown_retain_flag(self):
        # retain is the one closed block: a misspelled flag there is a flag that silently
        # does nothing, and the failure shows up as a broker that keeps answering after the
        # bridge is gone.
        doc = load()
        doc["retain"]["atributes"] = False
        self.assertTrue(errors_for(doc), "an unknown retain key was accepted")

    def test_reports_every_problem_at_once(self):
        # One error must not hide the next: a contract edit that breaks three things should
        # not need three runs to discover them.
        doc = copy.deepcopy(load())
        doc["topics"]["state"]["topic"] = "btwh/state"
        doc["retain"]["atributes"] = False
        self.assertGreaterEqual(len(errors_for(doc)), 2)


if __name__ == "__main__":
    unittest.main()
