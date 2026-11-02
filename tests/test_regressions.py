from pathlib import Path

import pytest

from research_base.core import ResearchBase
from research_base.materials import normalized_doi
from research_base.models import ResearchError


class EmptySources:
    def list_sources(self):
        return []


def test_registered_doi_keeps_balanced_parentheses():
    assert normalized_doi('https://doi.org/10.1234/(Example)') == '10.1234/(example)'


def test_export_does_not_mix_previous_collection_files(tmp_path):
    with ResearchBase(tmp_path / 'library', source_registry=EmptySources()) as base:
        collection = base.create_collection('Example')
        output = tmp_path / 'export'
        base.export_collection(collection['collection_id'], output)
        with pytest.raises(ResearchError) as error:
            base.export_collection(collection['collection_id'], output)
        assert error.value.code == 'INVALID_ARGUMENT'
        assert (output / 'manifest.json').exists()


def test_managed_rule_template_matches_packaged_template():
    from research_base.integration import TEMPLATE
    path = Path(__file__).resolve().parents[1] / 'templates' / 'AGENTS.research.md'
    assert path.read_text(encoding='utf-8').strip() == TEMPLATE.strip()
