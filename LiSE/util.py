# This file is part of LiSE, a framework for life simulation games.
# Copyright (c) 2013 Zachary Spector,  zacharyspector@gmail.com
"""Common utility functions and data structures.

"""
from collections import Mapping


def dispatch(d, key, *args):
    assert(isinstance(d, Mapping))
    if key in d:
        for f in d[key]:
            f(*args)
    if None in d:
        for f in d[None]:
            f(*args)


def listen(l, f, k=None):
    if not callable(f):
        raise TypeError('listeners must be callable')
    if isinstance(l, Mapping):
        l = l[k]
    if f not in l:
        l.append(f)


def listener(l, f=None, k=None):
    if f:
        listen(l, f, k)
        return
    return lambda fun: listen(l, fun, k)


try:
    from sqlalchemy.exc import OperationalError as alchemyOpError
    from sqlite3 import OperationalError as liteOpError
    OperationalError = (alchemyOpError, liteOpError)
except ImportError:
    from sqlite3 import OperationalError


try:
    from sqlalchemy.exc import IntegrityError as alchemyIntegError
    from sqlite3 import IntegrityError as liteIntegError
    IntegrityError = (alchemyIntegError, liteIntegError)
except ImportError:
    from sqlite3 import IntegrityError


class RedundantRuleError(ValueError):
    """Error condition for when you try to run a rule on a (branch,
    tick) it's already been executed.

    """
    pass


class CacheError(ValueError):
    """Error condition for something going wrong with a cache"""
    pass


class TravelException(Exception):
    """Exception for problems with pathfinding. Not necessarily an error
    because sometimes somebody SHOULD get confused finding a path.

    """
    def __init__(
            self,
            message,
            path=None,
            followed=None,
            traveller=None,
            branch=None,
            tick=None,
            lastplace=None
    ):
        """Store the message as usual, and also the optional arguments:

        ``path``: a list of Place names to show such a path as you found

        ``followed``: the portion of the path actually followed

        ``traveller``: the Thing doing the travelling

        ``branch``: branch during travel

        ``tick``: tick at time of error (might not be the tick at the
        time this exception is raised)

        ``lastplace``: where the traveller was, when the error happened

        """
        self.path = path
        self.followed = followed
        self.traveller = traveller
        self.branch = branch
        self.tick = tick
        self.lastplace = lastplace
        super().__init__(message)


class CompositeDict(Mapping):
    """Read-only mapping that looks up values in a first dict if
    available, then a second dict if possible.

    Assumes the dicts have no overlap.

    """
    def __init__(self, d1, d2):
        """Store dictionaries"""
        self.d1 = d1
        self.d2 = d2

    def __iter__(self):
        """Iterate over both dictionaries' keys"""
        for k in self.d1:
            yield k
        for k in self.d2:
            yield k

    def __len__(self):
        """Sum the lengths of both dictionaries"""
        return len(self.d1) + len(self.d2)

    def __getitem__(self, k):
        """Get an item from ``d1`` if possible, then ``d2``"""
        try:
            return self.d1[k]
        except KeyError:
            return self.d2[k]


# ==Caching==
from gorm.xjson import JSONWrapper, JSONListWrapper
from collections import MutableMapping, MutableSequence


class JSONReWrapper(MutableMapping):
    """Like JSONWrapper with a cache."""
    def __init__(self, outer, key, initval=None):
        self._inner = JSONWrapper(outer, key)
        self._v = initval if initval else dict(self._inner)
        if not isinstance(self._v, dict):
            raise TypeError(
                "JSONReWrapper only wraps dicts"
            )

    def __iter__(self):
        return iter(self._v)

    def __len__(self):
        return len(self._v)

    def __eq__(self, other):
        return self._v == other

    def __getitem__(self, k):
        r = self._v[k]
        if isinstance(r, dict):
            return JSONReWrapper(self, k, r)
        if isinstance(r, list):
            return JSONListReWrapper(self, k, r)
        return r

    def __setitem__(self, k, v):
        self._inner[k] = v
        self._v[k] = v

    def __delitem__(self, k):
        del self._inner[k]
        del self._v[k]

    def __repr__(self):
        return repr(self._v)


class JSONListReWrapper(MutableSequence):
    """Like JSONListWrapper with a cache."""
    def __init__(self, outer, key, initval=None):
        self._inner = JSONListWrapper(outer, key)
        self._v = initval if initval else list(self._inner)
        if not isinstance(self._v, list):
            raise TypeError(
                "JSONListReWrapper only wraps lists"
            )

    def __iter__(self):
        return iter(self._v)

    def __len__(self):
        return len(self._v)

    def __eq__(self, other):
        return self._v == other

    def __getitem__(self, i):
        r = self._v[i]
        if isinstance(r, dict):
            return JSONReWrapper(self, i, r)
        if isinstance(r, list):
            return JSONListReWrapper(self, i, r)
        return r

    def __setitem__(self, i, v):
        self._inner[i] = v
        self._v[i] = v

    def __delitem__(self, i, v):
        del self._inner[i]
        del self._v[i]

    def insert(self, i, v):
        self._inner.insert(i, v)
        self._v.insert(i, v)

    def __repr__(self):
        return repr(self._v)


def encache(cache, k, v, branch, tick):
    """Put ``k=v`` into ``cache`` and delete anything later than it"""
    if k not in cache:
        cache[k] = {}
    if branch not in cache[k]:
        cache[k][branch] = {}
    for t in list(cache[k][branch].keys()):
        if t > tick:
            del cache[k][branch][t]
    if isinstance(v, JSONListWrapper):
        v = JSONListReWrapper(v.outer, v.outkey, list(v))
    elif isinstance(v, JSONWrapper):
        v = JSONReWrapper(v.outer, v.outkey, dict(v))
    cache[k][branch][tick] = v


def needcache(cache, k, branch, tick):
    """Return whether ``k`` lacks a value in ``cache`` for the given
    ``branch`` and ``tick``.

    """
    return (
        k not in cache or
        branch not in cache[k] or
        tick not in cache[k][branch]
    )


def fillcache(engine, real, cache):
    """Copy all the present values in ``real`` into ``cache``, indexed
    appropriately.

    """
    (branch, tick) = engine.time
    for k in real:
        if k not in cache:
            cache[k] = {}
        if branch not in cache[k]:
            cache[k][branch] = {}
        if tick not in cache[k][branch]:
            cache[k][branch][tick] = real[k]


def fire_time_travel_triggers(
        engine,
        real,
        cache,
        dispatcher,
        branch_then,
        tick_then,
        branch_now,
        tick_now
):
    """For each key in ``cache`` whose value has changed between the two
    times given, call ``dispatcher`` with the key and its current value.

    ``real`` is the mapping whose values ``cache`` caches. It will be
    used when the key is in the cache, but not at either of the times
    given.

    If a key has never been cached, it won't trigger any of its
    listeners, but this is unlikely, because caching happens whenever
    the key is used for any purpose.

    """
    for k in cache:
        if (
                branch_then in cache[k] and
                tick_then in cache[k][branch_then] and
                cache[k][branch_then][tick_then] is not None
        ):
            # key was set then
            if (
                    branch_now in cache[k] and
                    tick_now in cache[k][branch_now] and
                    cache[k][branch_now][tick_now] is not None
            ):
                # key is set now
                val_then = cache[k][branch_then][tick_then]
                val_now = cache[k][branch_now][tick_now]
                if val_then != val_now:
                    # key's value changed between then and now
                    dispatcher(k, val_now)
            else:
                # No cached info on the value right now; account for
                # the common case that a single tick has passed and
                # nothing has changed
                if (
                    branch_then == branch_now and
                    tick_then + 1 == tick_now
                ):
                    cache[k][branch_now][tick_now] \
                        = cache[k][branch_now][tick_then]
                    continue
                # otherwise, fetch from db
                try:
                    if branch_now not in cache[k]:
                        cache[k][branch_now] = {}
                    cache[k][branch_now][tick_now] = real[k]
                    if (
                        cache[k][branch_then][tick_then] !=
                        cache[k][branch_now][tick_now]
                    ):
                        dispatcher(k, cache[k][branch_now][tick_now])
                except KeyError:
                    dispatcher(k, None)
        else:
            # key might not have been set then -- if it was, our
            # listeners never heard of it, or it'd be cached
            if (
                    branch_now in cache[k] and
                    tick_now in cache[k][branch_now]
            ):
                if cache[k][branch_now][tick_now] is None:
                    # and they still never heard of it
                    continue
                else:
                    # key is set now
                    dispatcher(k, cache[k][branch_now][tick_now])


def keycache_iter(keycache, branch, tick, get_iterator):
    if branch not in keycache:
        keycache[branch] = {}
    if tick not in keycache[branch]:
        keycache[branch][tick] = set(get_iterator())
    yield from keycache[branch][tick]


def cache_get(cache, keycache, branch, tick, key, getter):
    """Utility function to retrieve something from a branch/tick-indexed
    cache if possible, and if not, retrieve it using ``getter`` (and
    add it to the cache).

    """
    if key not in cache:
        cache[key] = {}
    if branch not in cache[key]:
        cache[key][branch] = {}
    if tick not in cache[key][branch]:
        cache[key][branch][tick] = getter(key)
    return cache[key][branch][tick]


def cache_set(cache, keycache, branch, tick, key, value, setter):
    """Utility function to both set ``key = value`` using ``setter`` and
    add ``value`` to the cache, indexed with ``branch`` and then
    ``tick``.

    """
    encache(cache, key, value, branch, tick)
    if branch in keycache:
        try:
            if tick not in keycache[branch]:
                keycache[branch][tick] = set(
                    keycache[branch][
                        max(t for t in keycache[branch]
                            if t < tick)
                    ]
                )
            keycache[branch][tick].add(key)
        except ValueError:
            pass
    setter(key, value)


def cache_del(cache, keycache, branch, tick, key, deleter):
    """Utility function to both delete the key from the
    branch/tick-indexed cache, and delete it using ``deleter``.

    """
    encache(cache, key, None, branch, tick)
    if branch in keycache:
        try:
            if tick not in keycache[branch]:
                keycache[branch][tick] = set(
                    keycache[branch][
                        max(t for t in keycache[branch]
                            if t < tick)
                        ]
                )
            keycache[branch][tick].remove(key)
        except ValueError:
            pass
    deleter(key)


def path_len(graph, path, weight=None):
    """Return the number of ticks it will take to follow ``path``,
    assuming the portals' ``weight`` attribute is how long it will
    take to go through that portal--if unspecified, 1 tick.

    """
    n = 0
    path = list(path)  # local copy
    prevnode = path.pop(0)
    while path:
        nextnode = path.pop(0)
        edge = graph.edge[prevnode][nextnode]
        n += edge[weight] if weight and hasattr(edge, weight) else 1
        prevnode = nextnode
    return n
