"""Probabilistic record linkage with Splink (Fellegi-Sunter), running on DuckDB.

How it decides
--------------
For each *candidate pair* (found by blocking), Splink compares name, phone, email and date of
birth, and turns each comparison into a log-likelihood ratio: "how much more likely is this
agreement if the two records are the same person than if they are different people?". The
ratios add up to a match weight and then to a match probability.

Two details matter here:

* **Missing is not disagreement.** A record without a phone is not evidence against a record
  with one. Splink models a null as its own level, so unknown identifiers stay neutral.
* **Blocking is the recall ceiling.** A pair that no blocking rule proposes can never be linked,
  however similar the records are. The rules below propose pairs that share a phone, share an
  email, or share a date of birth plus one name token.

The model is trained once (day 0) and saved; daily runs only score.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import splink.comparison_library as cl
from splink import Linker, SettingsCreator, block_on
from splink.backends.duckdb import DuckDBAPI

from cip.config import Settings

logging.getLogger("splink").setLevel(logging.WARNING)

MODEL_COLUMNS = [
    "unique_id",
    "name_key",
    "phone_match",
    "email_match",
    "dob_norm",
    "tok_a",
    "tok_z",
]


def prepare(records: pd.DataFrame) -> pd.DataFrame:
    """Project staged records onto the columns the model uses (``unique_id`` is the record key)."""
    frame = records.rename(columns={"record_key": "unique_id"})
    return frame[MODEL_COLUMNS].reset_index(drop=True)


def _blocking_rules():
    return [
        block_on("phone_match"),
        block_on("email_match"),
        block_on("dob_norm", "tok_a"),
        block_on("dob_norm", "tok_z"),
    ]


def _settings() -> SettingsCreator:
    return SettingsCreator(
        link_type="dedupe_only",
        unique_id_column_name="unique_id",
        blocking_rules_to_generate_predictions=_blocking_rules(),
        comparisons=[
            cl.JaroWinklerAtThresholds("name_key", [0.97, 0.90, 0.80]),
            cl.ExactMatch("phone_match"),
            cl.ExactMatch("email_match"),
            cl.ExactMatch("dob_norm"),
        ],
    )


def _linker(frame: pd.DataFrame, settings: SettingsCreator | str) -> Linker:
    db = DuckDBAPI()
    return Linker(db.register(frame), settings, log_level=30)


def train(records: pd.DataFrame, cfg: Settings, model_path: Path) -> Path:
    """Estimate the model from the data itself (no labels needed) and save it."""
    linker = _linker(prepare(records), _settings())
    # prior: share of random pairs that match, anchored on rules that are right most of the time
    linker.training.estimate_probability_two_random_records_match(
        [block_on("phone_match", "name_key"), block_on("email_match")], recall=0.8
    )
    linker.training.estimate_u_using_random_sampling(max_pairs=cfg.em_max_pairs)
    # m-probabilities by EM; each blocking rule trains the comparisons it does not block on
    linker.training.estimate_parameters_using_expectation_maximisation(block_on("phone_match"))
    linker.training.estimate_parameters_using_expectation_maximisation(block_on("name_key"))
    model_path.parent.mkdir(parents=True, exist_ok=True)
    linker.misc.save_model_to_json(str(model_path), overwrite=True)
    return model_path


def score(records: pd.DataFrame, model_path: Path, threshold: float) -> pd.DataFrame:
    """Score candidate pairs with a saved model; keep pairs at or above ``threshold``."""
    linker = _linker(prepare(records), str(model_path))
    pairs = linker.inference.predict(threshold_match_probability=threshold).as_pandas_dataframe()
    return pairs[["unique_id_l", "unique_id_r", "match_probability", "match_weight"]].reset_index(
        drop=True
    )
