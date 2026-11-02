from research_base.identity import observed_identifiers
from research_base.workflows import seed_matches


def test_official_repository_alias_is_part_of_actual_observation():
    record={'title':'A Research Framework','doi':'10.5555/published','identifiers':{'openalex':'W123'},'landing_url':'https://arxiv.org/abs/2408.08067'}
    assert seed_matches({'arxiv':'2408.08067'},record)
    assert not seed_matches({'arxiv':'2408.08067v1'},record)


def test_similar_domain_and_title_cannot_create_repository_identifier():
    record={'title':'A Research Framework','landing_url':'https://arxiv.org.evil.example/abs/2408.08067'}
    assert not seed_matches({'arxiv':'2408.08067'},record)


def test_provider_explicit_version_not_overwritten_by_file_link():
    ids=observed_identifiers({'arxiv':'2408.08067v1'},fulltext_urls=['https://arxiv.org/pdf/2408.08067v2'])
    assert ids['arxiv']=='2408.08067v1'
