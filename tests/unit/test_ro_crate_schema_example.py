"""Validate the worked example in the RO-Crate schema spec.

The spec's "Complete example" JSON-LD is the documented contract for what
`ro_crate.py` produces. Without a test it silently drifts from the generator
(it already gained bag File entities). These tests parse that block straight
out of the spec and check it is well-formed, flattened, and self-consistent —
so the documentation stays honest.
"""

import json
from pathlib import Path

import pytest

SPEC = Path(__file__).resolve().parents[2] / "specs" / "ro_crate_schema.md"


def _example_doc() -> dict:
    """Extract and parse the JSON-LD under the spec's 'Complete example'."""
    text = SPEC.read_text()
    assert "## Complete example" in text, "spec is missing its example section"
    after = text.split("## Complete example", 1)[1]
    block = after.split("```json", 1)[1].split("```", 1)[0]
    return json.loads(block)  # fails the test if the example isn't valid JSON


def _graph_by_id(doc: dict) -> dict:
    return {e["@id"]: e for e in doc["@graph"]}


def test_example_is_well_formed():
    doc = _example_doc()
    assert doc["@context"][0] == "https://w3id.org/ro/crate/1.1/context"
    assert doc["@context"][1]["sosa"] == "http://www.w3.org/ns/sosa/"
    by_id = _graph_by_id(doc)
    descriptor = by_id["ro-crate-metadata.json"]
    assert descriptor["@type"] == "CreativeWork"
    assert descriptor["about"] == {"@id": "./"}
    assert by_id["./"]["@type"] == "Dataset"


def test_example_documents_bag_file_checksums():
    """The example must show bag files as File entities with sha256, listed in
    the bag Dataset's hasPart (the behaviour ro_crate.py now emits)."""
    doc = _example_doc()
    bag = next(e for e in doc["@graph"]
               if e.get("@type") == "Dataset" and e["@id"].startswith("bags/"))
    parts = {p["@id"] for p in bag.get("hasPart", [])}
    assert parts, "bag Dataset must list its files in hasPart"
    bag_files = [e for e in doc["@graph"]
                 if e.get("@type") == "File" and e["@id"].startswith("bags/")]
    assert bag_files, "example must include bag File entities"
    for entity in bag_files:
        assert entity["sha256"], f"{entity['@id']} missing sha256"
        assert entity["@id"] in parts, f"{entity['@id']} not in bag hasPart"


def test_example_is_flattened_and_references_resolve():
    """Every contextual (#fragment) reference must resolve to a defined entity
    — the flattened-JSON-LD invariant the rocrate library requires."""
    doc = _example_doc()
    ids = {e["@id"] for e in doc["@graph"]}
    refs: set[str] = set()

    def walk(value) -> None:
        if isinstance(value, dict):
            if list(value.keys()) == ["@id"]:
                refs.add(value["@id"])
            else:
                for sub in value.values():
                    walk(sub)
        elif isinstance(value, list):
            for sub in value:
                walk(sub)

    for entity in doc["@graph"]:
        walk(entity)

    dangling = {r for r in refs if r.startswith("#") and r not in ids}
    assert not dangling, f"unresolved fragment references: {sorted(dangling)}"


def test_example_loads_with_rocrate(tmp_path):
    """The documented example must itself be a loadable RO-Crate."""
    rocrate = pytest.importorskip("rocrate.rocrate")
    doc = _example_doc()
    (tmp_path / "ro-crate-metadata.json").write_text(json.dumps(doc))
    crate = rocrate.ROCrate(str(tmp_path))  # raises if the JSON-LD is malformed
    ids = {e.id for e in crate.get_entities()}
    assert "./" in ids
    assert any(i.startswith("bags/") and i.endswith((".db3", ".mcap"))
               for i in ids), "expected a bag storage File entity"
