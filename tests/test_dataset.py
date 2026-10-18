import gzip
import json
from pathlib import Path


def test_commit_dataset_is_readable():
    path = Path("data/commits.jsonl.gz")
    assert path.exists(), "Commit dataset is missing"
    with gzip.open(path, "rt", encoding="utf-8") as file:
        records = [json.loads(line) for line in file if line.strip()]
    assert records, "Dataset contains no commits"
    for record in records:
        assert record.get("h"), "Commit hash is missing"
        assert record.get("id"), "Contributor ID is missing"
        assert isinstance(record.get("t"), int)
        assert isinstance(record.get("f"), list)
        assert record["f"], "Commit has no tracked files"
