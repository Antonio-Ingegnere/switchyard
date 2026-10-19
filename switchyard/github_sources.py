"""GitHub REST resolver. Only issue/PR descriptions and conversation comments."""
from datetime import datetime, timezone
import json
import os
import re
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, HTTPRedirectHandler, build_opener

from .source_resolution import SourceReference, ResolvedSource, SourceResolutionError

HTTP_TIMEOUT = 10
MAX_RESPONSE_BYTES = 1048576
MAX_COMMENT_PAGES = 2
COMMENTS_PER_PAGE = 10
MAX_BODY_BYTES = 8192
MAX_COMMENT_BYTES = 2048
MAX_TITLE_BYTES = 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_get(url, headers, timeout, max_bytes):
    """No redirects/retries; bounded reads and deadline, including slow streams."""
    deadline = time.monotonic() + timeout
    with build_opener(_NoRedirect()).open(Request(url, headers=headers), timeout=timeout) as response:
        chunks, size = [], 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError()
            chunk = response.read1(min(65536, max_bytes + 1 - size))
            if not chunk:
                return b''.join(chunks)
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                raise OverflowError()


class GitHubSourceResolver:
    identity = 'github-rest/1'

    def __init__(self, transport=None, *, environment=None, clock=None):
        self.transport = transport or http_get
        self.environment = os.environ if environment is None else environment
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())

    def reference(self, candidate):
        match = re.fullmatch(r'https://github\.com/([A-Za-z0-9_-]{1,100})/([A-Za-z0-9_.-]{1,100})/(issues|pull)/([0-9]{1,12})/?(?:[?#][^\s]*)?', candidate, re.I)
        if not match or int(match[4]) == 0:
            return None
        owner, repo, kind, number = match.groups()
        url = f'https://github.com/{owner.lower()}/{repo.lower()}/{kind.lower()}/{int(number)}'
        return SourceReference('github_issue' if kind.lower() == 'issues' else 'github_pull_request', candidate, url)

    def resolve(self, reference):
        token = self.environment.get('GH_TOKEN') or self.environment.get('GITHUB_TOKEN') or ''
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'Switchyard-source-resolver/1',
                   'X-GitHub-Api-Version': '2022-11-28'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        owner, repo, kind, number = reference.canonical_url.split('/')[-4:]
        api = f'https://api.github.com/repos/{owner}/{repo}'

        def fetch(url):
            failure = None
            try:
                raw = self.transport(url, headers, HTTP_TIMEOUT, MAX_RESPONSE_BYTES)
                if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
                    raise OverflowError()
                value = json.loads(raw)
            except HTTPError as exc:
                exc.close()
                failure = {401: ('authentication_required', 'Set a valid GH_TOKEN or GITHUB_TOKEN.'),
                           403: ('forbidden_or_rate_limited', 'Check token permissions or retry after the GitHub rate limit resets.'),
                           404: ('not_found', 'Check the URL and repository access; private repositories may require a token.')}.get(
                               exc.code, ('http_error', 'GitHub rejected the request; check availability and URL.'))
            except (TimeoutError, socket.timeout):
                failure = ('timeout', 'GitHub timed out; retry when connectivity is restored.')
            except URLError as exc:
                failure = ('timeout' if isinstance(exc.reason, TimeoutError) else 'network_error',
                           'Cannot reach GitHub; check connectivity and retry.')
            except OverflowError:
                failure = ('response_size_limit', 'GitHub response exceeds the bounded read limit; narrow the source.')
            except (ValueError, UnicodeError, TypeError, RecursionError):
                failure = ('malformed_response', 'GitHub returned invalid JSON; retry or check the source.')
            except OSError:
                failure = ('network_error', 'Cannot reach GitHub; check connectivity and retry.')
            # Outside handlers: exceptions contain neither remote bodies nor credential-bearing chains.
            if failure:
                raise SourceResolutionError(reference, *failure)
            return value

        truncated = []

        def text(value, field, limit, nullable=False):
            if value is None and nullable:
                value = ''
            if type(value) is not str:
                raise SourceResolutionError(reference, 'malformed_response', f'GitHub returned an invalid {field} field.')
            if token:
                value = value.replace(token, '[REDACTED]')
            try:
                data = value.encode('utf-8')
            except UnicodeError:
                data = None
            if data is None:
                raise SourceResolutionError(reference, 'malformed_response', 'GitHub returned invalid Unicode.')
            if len(data) > limit:
                truncated.append(dict(field=field, original_bytes=len(data), retained_limit_bytes=limit))
                value = data[:limit].decode('utf-8', errors='ignore')
            return value

        payload = fetch(f'{api}/{"issues" if kind == "issues" else "pulls"}/{number}')
        if (type(payload) is not dict or not {'number', 'title', 'body', 'state', 'comments'} <= payload.keys()
                or type(payload.get('number')) is not int or payload['number'] != int(number)
                or type(payload.get('comments')) is not int or payload['comments'] < 0):
            raise SourceResolutionError(reference, 'malformed_response', 'GitHub returned invalid resource identity or comment count.')
        content = dict(repository=f'{owner}/{repo}', number=int(number), canonical_url=reference.canonical_url,
                       title=text(payload.get('title'), 'title', MAX_TITLE_BYTES),
                       body=text(payload.get('body'), 'body', MAX_BODY_BYTES, True),
                       state=text(payload.get('state'), 'state', 32), comments=[])
        for page in range(1, MAX_COMMENT_PAGES + 1):
            if len(content['comments']) >= payload['comments']:
                break
            comments = fetch(f'{api}/issues/{number}/comments?per_page={COMMENTS_PER_PAGE}&page={page}')
            if type(comments) is not list or len(comments) > COMMENTS_PER_PAGE:
                raise SourceResolutionError(reference, 'malformed_response', 'GitHub returned an invalid comments page.')
            for comment in comments:
                if type(comment) is not dict or type(comment.get('id')) is not int or 'body' not in comment:
                    raise SourceResolutionError(reference, 'malformed_response', 'GitHub returned an invalid comment.')
                content['comments'].append(dict(id=comment['id'], body=text(comment.get('body'), f"comments[{len(content['comments'])}].body", MAX_COMMENT_BYTES, True)))
            if len(comments) < COMMENTS_PER_PAGE:
                break
        content['truncation'] = dict(fields=truncated, comments_available=payload['comments'],
                                     comments_retained=len(content['comments']),
                                     comments_truncated=payload['comments'] > len(content['comments']))
        return ResolvedSource(reference, content, self.identity, self.clock())
