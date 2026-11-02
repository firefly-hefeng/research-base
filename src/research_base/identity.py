"""Identifiers observed in provider metadata and official repository links."""
import re
from urllib.parse import unquote, urlsplit


def observed_identifiers(identifiers=None, *, doi=None, landing_url=None, fulltext_urls=None):
    ids = {str(k).lower(): str(v).strip() for k,v in (identifiers or {}).items() if v}
    if doi and str(doi).lower().startswith('10.48550/arxiv.'):
        ids.setdefault('arxiv', str(doi).lower().split('arxiv.',1)[1])
    for url in [landing_url] + list(fulltext_urls or []):
        if not isinstance(url,str):
            continue
        parsed = urlsplit(url)
        if parsed.scheme not in ('http','https') or parsed.hostname not in ('arxiv.org','www.arxiv.org','export.arxiv.org'):
            continue
        match = re.fullmatch(r'/(?:abs|pdf)/(.+?)(?:\.pdf)?/?', unquote(parsed.path))
        if not match:
            continue
        value = match[1]
        if re.fullmatch(r'(?:\d{4}\.\d{4,5}|[A-Za-z.-]+/\d{7})(?:v\d+)?',value):
            ids.setdefault('arxiv',value)
    if ids.get('arxiv'):
        ids.setdefault('arxiv_work',re.sub(r'v\d+$','',ids['arxiv']))
    return ids
