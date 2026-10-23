"""QFLX-74: 04g pins LimeTorrents (5 of 6 fake malware releases) to the lowest
Prowlarr priority. Offline: exercises plan() only."""
import importlib.util
from pathlib import Path

_P = Path(__file__).resolve().parents[2] / "scripts" / "configure" / "04g-prowlarr-demote-indexers.py"
_spec = importlib.util.spec_from_file_location("demote", _P)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def _ind(i, defn, prio=25):
    return {"id": i, "name": defn.title(), "definitionName": defn, "priority": prio}


def test_limetorrents_is_demoted_to_the_lowest_priority():
    assert m.DEMOTE["limetorrents"] == m.LOWEST == 50


def test_plan_targets_only_listed_indexers_at_the_wrong_priority():
    todo = m.plan([_ind(2, "limetorrents"), _ind(4, "thepiratebay"), _ind(21, "Knaben")])
    assert [(i["id"], w) for i, w in todo] == [(2, 50)]


def test_plan_is_idempotent_and_case_insensitive():
    assert m.plan([_ind(2, "limetorrents", 50)]) == []
    assert len(m.plan([_ind(2, "LimeTorrents")])) == 1
