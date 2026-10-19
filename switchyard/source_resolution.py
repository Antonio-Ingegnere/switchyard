"""Bounded control-plane source preparation, independent of agents and workflow routing."""
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Protocol

from .work_contract import WorkContractError, WorkWorkspace
from .workflow_bundle import ArtifactSpec


MAX_REFERENCES = 4
MAX_SOURCE_BYTES = 24000  # aggregate normalized JSON, before context JSON escaping
SOURCE_CATALOG = {'request': ArtifactSpec('text', source='request'),
                  'resolved_sources': ArtifactSpec('json')}


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


@dataclass(frozen=True)
class SourceReference:
    source_type: str
    original_reference: str
    canonical_url: str


@dataclass(frozen=True)
class ResolvedSource:
    reference: SourceReference
    content: dict
    resolver: str
    retrieved_at: str

    def document(self):
        return dict(schema_version=1, source_type=self.reference.source_type,
                    original_reference=self.reference.original_reference,
                    canonical_url=self.reference.canonical_url, content=self.content,
                    retrieved_at=self.retrieved_at, resolver=self.resolver,
                    content_sha256=hashlib.sha256(canonical_json(self.content).encode('utf-8')).hexdigest())


class SourceResolutionError(WorkContractError):
    def __init__(self, reference, classification, explanation):
        self.reference, self.classification = reference, classification
        super().__init__(f'{reference.source_type} {reference.canonical_url}: {classification}: {explanation}')


class SourceResolver(Protocol):
    def reference(self, candidate: str) -> SourceReference | None: ...
    def resolve(self, reference: SourceReference) -> ResolvedSource: ...


def detect_references(request, resolvers):
    found, seen = [], set()
    for match in re.finditer(r'https://[^\s<>"\x00-\x1f]+', request):
        candidate = match.group().rstrip('.,;:!?)]}')
        for resolver in resolvers:
            ref = resolver.reference(candidate)
            if ref is not None:
                if ref.canonical_url not in seen:
                    if len(found) == MAX_REFERENCES:
                        raise SourceResolutionError(ref, 'reference_limit', 'Use at most 4 supported references per request.')
                    seen.add(ref.canonical_url)
                    found.append((resolver, ref))
                break
    return found


def prepare_sources(workspace, resolvers=None):
    """Commit one immutable batch in a normal child WorkWorkspace; reuse on resume."""
    if 'request' not in workspace.artifacts or workspace.latest_artifact('request') is None:
        return None
    request = workspace.read_artifact('request')
    root = workspace.root / 'sources'
    if (root / 'manifest.jsonl').exists():
        store = WorkWorkspace.open(root, SOURCE_CATALOG)
        store.initialize_request(request)
        if store.latest_artifact('resolved_sources') is not None:
            store.read_artifact('resolved_sources')  # verify before any provider resolution
            return store
    if resolvers is None:
        from .github_sources import GitHubSourceResolver
        resolvers = (GitHubSourceResolver(),)
    references = detect_references(request, resolvers)
    if not references:
        return None
    documents = []
    for resolver, reference in references:
        documents.append(resolver.resolve(reference).document())
        content = canonical_json(dict(schema_version=1, sources=documents))
        if len(content.encode('utf-8')) > MAX_SOURCE_BYTES:
            raise SourceResolutionError(reference, 'source_size_limit',
                                        'Normalized sources exceed 24000 bytes; narrow the request or split references.')
    store = WorkWorkspace.open(root, SOURCE_CATALOG)
    store.initialize_request(request)
    store.write_artifact('resolved_sources', content)
    return store
