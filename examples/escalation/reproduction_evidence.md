# Fixed cache experiment

The service uses a single in-memory dictionary with capacity 100. No evictions,
concurrent requests or restarts occurred. The dictionary was empty initially.

The implementation is:

    def fetch(key):
        if key in cache:
            return cache[key]
        value = backend(key.lower())
        cache[key.lower()] = value
        return value

Observed requests and cumulative backend-call counts:

| request | backend calls | cache keys afterward |
| --- | --- | --- |
| fetch("ALPHA") | 1 | ["alpha"] |
| fetch("ALPHA") | 2 | ["alpha"] |
| fetch("alpha") | 2 | ["alpha"] |
| fetch("Alpha") | 3 | ["alpha"] |

All four return the same value. Keys are intended to be case-insensitive.
