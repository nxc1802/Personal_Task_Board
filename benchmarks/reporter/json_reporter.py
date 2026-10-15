"""JSON Report Generator for Benchmark Framework."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Union

from benchmarks.models import BenchmarkScorecard


class JsonReporter:
    """Serializes BenchmarkScorecard into formatted JSON."""

    @classmethod
    def render(cls, scorecard: BenchmarkScorecard) -> str:
        return scorecard.model_dump_json(indent=2)

    @classmethod
    def write(cls, scorecard: BenchmarkScorecard, output_path: Union[str, Path]) -> Path:
        out_file = Path(output_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(cls.render(scorecard), encoding="utf-8")
        return out_file
