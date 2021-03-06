# This file is part of LiSE, a framework for life simulation games.
# Copyright (c) Zachary Spector,  zacharyspector@gmail.com
"""Proxy objects to access LiSE entities from another process.

Each proxy class is meant to emulate the equivalent LiSE class,
and any change you make to a proxy will be made in the corresponding
entity in the LiSE core.

"""
import sys
import logging
from os import getpid
from collections import (
    Mapping,
    MutableMapping,
    MutableSequence
)
from functools import partial
from threading import Thread, Lock
from multiprocessing import Process, Pipe, Queue, ProcessError
from queue import Empty
from blinker import Signal

from allegedb.cache import HistoryError
from .engine import AbstractEngine
from .character import Facade
from allegedb.xjson import JSONReWrapper, JSONListReWrapper
from .util import reify, getatt
from allegedb.cache import PickyDefaultDict, StructuredDefaultDict
from .handle import EngineHandle
from .xcollections import AbstractLanguageDescriptor


class CachingProxy(MutableMapping, Signal):
    def __init__(self, engine_proxy):
        super().__init__()
        self.engine = engine_proxy
        self.exists = True

    def __bool__(self):
        return bool(self.exists)

    def __iter__(self):
        yield from self._cache

    def __len__(self):
        return len(self._cache)

    def __contains__(self, k):
        return k in self._cache

    def __getitem__(self, k):
        if k not in self:
            raise KeyError("No such key: {}".format(k))
        return self._cache[k]

    def __setitem__(self, k, v):
        self._set_item(k, v)
        self._cache[k] = self._cache_munge(k, v)
        self.send(self, key=k, value=v)

    def __delitem__(self, k):
        if k not in self:
            raise KeyError("No such key: {}".format(k))
        self._del_item(k)
        del self._cache[k]
        self.send(self, key=k, value=None)

    def _apply_delta(self, delta):
        for (k, v) in delta.items():
            if v is None:
                if k in self._cache:
                    del self._cache[k]
                    self.send(self, key=k, value=None)
            elif k not in self._cache or self._cache[k] != v:
                self._cache[k] = v
                self.send(self, key=k, value=v)

    def _cache_munge(self, k, v):
        raise NotImplementedError("Abstract method")

    def _set_item(self, k, v):
        raise NotImplementedError("Abstract method")

    def _del_item(self, k):
        raise NotImplementedError("Abstract method")


class CachingEntityProxy(CachingProxy):
    def _cache_munge(self, k, v):
        if isinstance(v, dict):
            return JSONReWrapper(self, k, v)
        elif isinstance(v, list):
            return JSONListReWrapper(self, k, v)
        return v

    def __repr__(self):
        return "{}({}) {}".format(
            self.__class__.__name__, self._cache, self.name
        )


class RulebookProxyDescriptor(object):
    def __get__(self, inst, cls):
        if inst is None:
            return self
        try:
            proxy = inst._get_rulebook_proxy()
        except KeyError:
            proxy = RuleBookProxy(inst.engine, inst._get_default_rulebook_name())
            inst._set_rulebook_proxy(proxy)
        return proxy

    def __set__(self, inst, val):
        if hasattr(val, 'name'):
            if not isinstance(val, RuleBookProxy):
                raise TypeError
            rb = val
            val = val.name
        elif val in inst.engine._rulebooks_cache:
            rb = inst.engine._rulebooks_cache[val]
        else:
            rb = RuleBookProxy(inst.engine, val)
        inst._set_rulebook(val)
        inst._set_rulebook_proxy(rb)
        inst.send(inst, rulebook=rb)


class NodeProxy(CachingEntityProxy):
    rulebook = RulebookProxyDescriptor()
    @property
    def character(self):
        return self.engine.character[self._charname]

    @property
    def _cache(self):
        return self.engine._node_stat_cache[self._charname][self.name]

    def _get_default_rulebook_name(self):
        return self._charname, self.name

    def _get_rulebook_proxy(self):
        return self.engine._char_node_rulebooks_cache[self._charname][self.name]

    def _set_rulebook_proxy(self, rb):
        self.engine._char_node_rulebooks_cache[self._charname][self.name] = rb

    def _set_rulebook(self, rb):
        self.engine.handle(
            'set_node_rulebook',
            char=self._charname, node=self.name, rulebook=rb, silent=True,
            branching=True
        )

    def __init__(self, engine_proxy, charname, nodename):
        self._charname = charname
        self.name = nodename
        super().__init__(engine_proxy)

    def __iter__(self):
        yield from super().__iter__()
        yield 'character'
        yield 'name'

    def __eq__(self, other):
        return (
            isinstance(other, NodeProxy) and
            self._charname == other._charname and
            self.name == other.name
        )

    def __hash__(self):
        return hash((self._charname, self.name))

    def __contains__(self, k):
        if k in ('character', 'name'):
            return True
        return super().__contains__(k)

    def __getitem__(self, k):
        if k == 'character':
            return self._charname
        elif k == 'name':
            return self.name
        return super().__getitem__(k)

    def _get_state(self):
        return self.engine.handle(
            command='node_stat_copy',
            char=self._charname,
            node=self.name
        )

    def _set_item(self, k, v):
        if k == 'name':
            raise KeyError("Nodes can't be renamed")
        self.engine.handle(
            command='set_node_stat',
            char=self._charname,
            node=self.name,
            k=k, v=v,
            silent=True,
            branching=True
        )

    def _del_item(self, k):
        if k == 'name':
            raise KeyError("Nodes need names")
        self.engine.handle(
            command='del_node_stat',
            char=self._charname,
            node=self.name,
            k=k,
            silent=True,
            branching=True
        )

    def delete(self):
        self.engine.del_node(self._charname, self.name)


class PlaceProxy(NodeProxy):
    def __repr__(self):
        return "proxy to {}.place[{}]".format(
            self._charname,
            self.name
        )


class ThingProxy(NodeProxy):
    @property
    def location(self):
        return self.engine.character[self._charname].node[self._location]

    @location.setter
    def location(self, v):
        if isinstance(v, NodeProxy):
            if v.character != self.character:
                raise ValueError(
                    "Things can only be located in their character. "
                    "Maybe you want an avatar?"
                )
            locn = v.name
        elif v in self.character.node:
            locn = v
        else:
            raise TypeError("Location must be a node or the name of one")
        self._set_location(locn)

    @property
    def next_location(self):
        if self._next_location is None:
            return None
        return self.engine.character[self._charname].node[self._next_location]

    def __init__(
            self, engine, character, name, location, next_location,
            arrival_time, next_arrival_time
    ):
        if location is None:
            raise TypeError("Thing must have location")
        super().__init__(engine, character, name)
        self._location = location
        self._next_location = next_location
        self._arrival_time = arrival_time or engine.turn
        self._next_arrival_time = next_arrival_time

    def __iter__(self):
        yield from super().__iter__()
        yield from {
            'location',
            'next_location',
            'arrival_time',
            'next_arrival_time'
        }

    def __getitem__(self, k):
        if k in {
                'location',
                'next_location',
                'arrival_time',
                'next_arrival_time'
        }:
            return getattr(self, '_' + k)
        return super().__getitem__(k)

    def _apply_delta(self, delta):
        for (k, v) in delta.items():
            if v is None:
                if k in self._cache:
                    del self._cache[k]
                    self.send(self, key=k, val=None)
            elif k in {'location', 'next_location'}:
                setattr(self, '_'+k, v)
                self.send(self, key=k, val=v)
            elif k not in self._cache or self._cache[k] != v:
                self._cache[k] = v
                self.send(self, key=k, val=v)

    def _set_location(self, v):
        self._location = v
        self.engine.handle(
            command='set_thing_location',
            char=self.character.name,
            thing=self.name,
            loc=v,
            silent=True,
            branching=True
        )
        self.send(self, key='location', val=v)

    def __setitem__(self, k, v):
        if k == 'location':
            self._set_location(v)
        elif k in {'next_location', 'arrival_time', 'next_arrival_time'}:
            raise ValueError("Read-only")
        else:
            super().__setitem__(k, v)

    def __repr__(self):
        if self._next_location is not None:
            return "proxy to {}.thing[{}]@{}->{}".format(
                self._charname,
                self.name,
                self._location,
                self._next_location
            )
        return "proxy to {}.thing[{}]@{}".format(
            self._charname,
            self.name,
            self._location
        )

    def follow_path(self, path, weight=None):
        self.engine.handle(
            command='thing_follow_path',
            char=self._charname,
            thing=self.name,
            path=path,
            weight=weight,
            silent=True
        )

    def go_to_place(self, place, weight=None):
        if hasattr(place, 'name'):
            place = place.name
        self.engine.handle(
            command='thing_go_to_place',
            char=self._charname,
            thing=self.name,
            place=place,
            weight=weight,
            silent=True
        )

    def travel_to(self, dest, weight=None, graph=None):
        if hasattr(dest, 'name'):
            dest = dest.name
        if hasattr(graph, 'name'):
            graph = graph.name
        self.engine.handle(
            command='thing_travel_to',
            char=self._charname,
            thing=self.name,
            dest=dest,
            weight=weight,
            graph=graph,
            silent=True
        )

    def travel_to_by(self, dest, arrival_tick, weight=None, graph=None):
        if hasattr(dest, 'name'):
            dest = dest.name
        if hasattr(graph, 'name'):
            graph = graph.name
        self.engine.handle(
            command='thing_travel_to_by',
            char=self._charname,
            thing=self.name,
            dest=dest,
            arrival_tick=arrival_tick,
            weight=weight,
            graph=graph,
            silent=True
        )


class PortalProxy(CachingEntityProxy):
    rulebook = RulebookProxyDescriptor()

    def _get_default_rulebook_name(self):
        return self._charname, self._origin, self._destination

    def _get_rulebook_proxy(self):
        return self.engine._char_port_rulebooks_cache[self._charname][self._origin][self._destination]

    def _set_rulebook_proxy(self, rb):
        self.engine._char_port_rulebooks_cache[self._charname][self._origin][self._destination] = rb

    def _set_rulebook(self, rb):
        self.engine.handle(
            command='set_portal_rulebook',
            char=self._charname,
            orig=self._origin,
            dest=self._destination,
            rulebook=rb,
            silent=True
        )

    def _get_rulebook_name(self):
        return self.engine.handle(
            command='get_portal_rulebook',
            char=self._charname,
            orig=self._origin,
            dest=self._destination
        )

    @property
    def _cache(self):
        return self.engine._portal_stat_cache[self._charname][
            self._origin][self._destination]

    @property
    def character(self):
        return self.engine.character[self._charname]

    @property
    def origin(self):
        return self.character.node[self._origin]

    @property
    def destination(self):
        return self.character.node[self._destination]

    def _set_item(self, k, v):
        self.engine.handle(
            command='set_portal_stat',
            char=self._charname,
            orig=self._origin,
            dest=self._destination,
            k=k, v=v,
            silent=True,
            branching=True
        )

    def _del_item(self, k):
        self.engine_handle(
            command='del_portal_stat',
            char=self._charname,
            orig=self._origin,
            dest=self._destination,
            k=k,
            silent=True,
            branching=True
        )

    def __init__(self, engine_proxy, charname, origname, destname):
        self._charname = charname
        self._origin = origname
        self._destination = destname
        super().__init__(engine_proxy)

    def __eq__(self, other):
        return (
            hasattr(other, 'character') and
            hasattr(other, 'origin') and
            hasattr(other, 'destination') and
            self.character == other.character and
            self.origin == other.origin and
            self.destination == other.destination
        )

    def __repr__(self):
        return "proxy to {}.portal[{}][{}]".format(
            self._charname,
            self._origin,
            self._destination
        )

    def __getitem__(self, k):
        if k == 'origin':
            return self._origin
        elif k == 'destination':
            return self._destination
        elif k == 'character':
            return self._charname
        return super().__getitem__(k)

    def delete(self):
        self.engine.del_portal(self._charname, self._origin, self._destination)


class NodeMapProxy(MutableMapping, Signal):
    rulebook = RulebookProxyDescriptor()

    def _get_default_rulebook_name(self):
        return self._charname, 'character_node'

    def _get_rulebook_proxy(self):
        return self.engine._character_rulebooks_cache[self._charname]['node']

    def _set_rulebook_proxy(self, rb):
        self.engine._character_rulebooks_cache[self._charname]['node'] = rb

    def _set_rulebook(self, rb):
        self.engine.handle(
            'set_character_node_rulebook',
            char=self._charname,
            rulebook=rb,
            silent=True,
            branching=True
        )

    @property
    def character(self):
        return self.engine.character[self._charname]

    def __init__(self, engine_proxy, charname):
        super().__init__()
        self.engine = engine_proxy
        self._charname = charname

    def __iter__(self):
        yield from self.character.thing
        yield from self.character.place

    def __len__(self):
        return len(self.character.thing) + len(self.character.place)

    def __getitem__(self, k):
        if k in self.character.thing:
            return self.character.thing[k]
        else:
            return self.character.place[k]

    def __setitem__(self, k, v):
        self.character.place[k] = v

    def __delitem__(self, k):
        if k in self.character.thing:
            del self.character.thing[k]
        else:
            del self.character.place[k]


class ThingMapProxy(CachingProxy):
    rulebook = RulebookProxyDescriptor()

    def _get_default_rulebook_name(self):
        return self.name, 'character_thing'

    def _get_rulebook_proxy(self):
        return self.engine._character_rulebooks_cache[self.name]['thing']

    def _set_rulebook_proxy(self, rb):
        self.engine._character_rulebooks_cache[self.name]['thing'] = rb

    def _set_rulebook(self, rb):
        self.engine.handle(
            'set_character_thing_rulebook',
            char=self.name,
            rulebook=rb,
            silent=True,
            branching=True
        )

    @property
    def character(self):
        return self.engine.character[self.name]

    @property
    def _cache(self):
        return self.engine._things_cache[self.name]

    def __init__(self, engine_proxy, charname):
        self.name = charname
        super().__init__(engine_proxy)

    def __eq__(self, other):
        return self is other

    def _cache_munge(self, k, v):
        return ThingProxy(
            self.engine, self.name, *self.engine.handle(
                'get_thing_special_stats', char=self.name, thing=k
            )
        )

    def _set_item(self, k, v):
        self.engine.handle(
            command='set_thing',
            char=self.name,
            thing=k,
            statdict=v,
            silent=True,
            branching=True
        )
        self._cache[k] = ThingProxy(
            self.engine, self.name,
            v.pop('location'), v.pop('next_location', None),
            v.pop('arrival_time', None), v.pop('next_arrival_time', None)
        )
        self.engine._node_stat_cache[self.name][k] = v

    def _del_item(self, k):
        self.engine.handle(
            command='del_node',
            char=self.name,
            node=k,
            silent=True,
            branching=True
        )
        del self._cache[k]
        del self.engine._node_stat_cache[self.name][k]


class PlaceMapProxy(CachingProxy):
    rulebook = RulebookProxyDescriptor()

    def _get_default_rulebook_name(self):
        return self.name, 'character_place'

    def _get_rulebook_proxy(self):
        return self.engine._character_rulebooks_cache[self.name]['place']

    def _set_rulebook_proxy(self, rb):
        self.engine._character_rulebooks_cache[self.name]['place'] = rb

    def _set_rulebook(self, rb):
        self.engine.handle(
            'set_character_place_rulebook',
            char=self.name, rulebook=rb,
            silent=True, branching=True
        )

    @property
    def character(self):
        return self.engine.character[self.name]

    @property
    def _cache(self):
        return self.engine._character_places_cache[self.name]

    def __init__(self, engine_proxy, character):
        self.name = character
        super().__init__(engine_proxy)

    def __eq__(self, other):
        return self is other

    def _cache_munge(self, k, v):
        return PlaceProxy(
            self.engine, self.name, k
        )

    def _set_item(self, k, v):
        self.engine.handle(
            command='set_place',
            char=self.name,
            place=k, statdict=v,
            silent=True,
            branching=True
        )
        self.engine._node_stat_cache[self.name][k] = v

    def _del_item(self, k):
        self.engine.handle(
            command='del_node',
            char=self.name,
            node=k,
            silent=True,
            branching=True
        )
        del self.engine._node_stat_cache[self.name][k]


class SuccessorsProxy(CachingProxy):
    @property
    def _cache(self):
        return self.engine._character_portals_cache.successors[
            self._charname][self._orig]

    def __init__(self, engine_proxy, charname, origname):
        self._charname = charname
        self._orig = origname
        super().__init__(engine_proxy)

    def __eq__(self, other):
        return (
            isinstance(other, SuccessorsProxy) and
            self.engine is other.engine and
            self._charname == other._charname and
            self._orig == other._orig
        )

    def _get_state(self):
        return {
            node: self._cache[node] if node in self._cache else
            PortalProxy(self.engine, self._charname, self._orig, node)
            for node in self.engine.handle(
                command='node_successors',
                char=self._charname,
                node=self._orig
            )
        }

    def _apply_delta(self, delta):
        raise NotImplementedError(
            "Apply the delta on CharSuccessorsMappingProxy"
        )

    def _cache_munge(self, k, v):
        if isinstance(v, PortalProxy):
            assert v._origin == self._orig
            assert v._destination == k
            return v
        return PortalProxy(
            self.engine,
            self._charname,
            self._orig,
            k
        )

    def _set_item(self, dest, value):
        self.engine.handle(
            command='set_portal',
            char=self._charname,
            orig=self._orig,
            dest=dest,
            statdict=value,
            silent=True,
            branching=True
        )

    def _del_item(self, dest):
        self.engine.del_portal(self._charname, self._orig, dest)


class CharSuccessorsMappingProxy(CachingProxy):
    rulebook = RulebookProxyDescriptor()

    def _get_default_rulebook_anme(self):
        return self.name, 'character_portal'

    def _get_rulebook_proxy(self):
        return self.engine._character_rulebooks_cache[self.name]['portal']

    def _set_rulebook_proxy(self, rb):
        self.engine._character_rulebooks_cache[self.name]['portal'] = rb

    def _set_rulebook(self, rb):
        self.engine.handle(
            'set_character_portal_rulebook',
            char=self.character.name, rulebook=rb, silent=True, branching=True
        )

    @property
    def character(self):
        return self.engine.character[self.character.name]

    @property
    def _cache(self):
        return self.engine._character_portals_cache.successors[self.name]

    def __init__(self, engine_proxy, charname):
        self.name = charname
        super().__init__(engine_proxy)

    def __eq__(self, other):
        return (
            isinstance(other, CharSuccessorsMappingProxy) and
            other.engine is self.engine and
            other.name == self.name
        )

    def _cache_munge(self, k, v):
        return {
            vk: PortalProxy(self.engine, self.name, vk, vv)
            for (vk, vv) in v.items()
        }

    def __getitem__(self, k):
        if k not in self:
            raise KeyError("No portals from {}".format(k))
        return SuccessorsProxy(
            self.engine,
            self.name,
            k
        )

    def _apply_delta(self, delta):
        for ((o, d), ex) in delta.items():
            if ex:
                if d not in self._cache[o]:
                    self._cache[o][d] = PortalProxy(
                        self.engine,
                        self.name,
                        o, d
                    )
            else:
                if o in self._cache and d in self._cache[o]:
                    del self._cache[o][d]
                    if len(self._cache[o]) == 0:
                        del self._cache[o]

    def _set_item(self, orig, val):
        self.engine.handle(
            command='character_set_node_successors',
            character=self.name,
            node=orig,
            val=val,
            silent=True,
            branching=True
        )

    def _del_item(self, orig):
        for dest in self[orig]:
            self.engine.del_portal(self.name, orig, dest)


class PredecessorsProxy(MutableMapping):
    @property
    def character(self):
        return self.engine.character[self._charname]

    def __init__(self, engine_proxy, charname, destname):
        self.engine = engine_proxy
        self._charname = charname
        self.name = destname

    def __iter__(self):
        return iter(self.engine._character_portals_cache.predecessors[
            self._charname][self.name])

    def __len__(self):
        return len(self.engine._character_portals_cache.predecessors[
            self._charname][self.name])

    def __contains__(self, k):
        return k in self.engine._character_portals_cache.predecessors[
            self._charname][self.name]

    def __getitem__(self, k):
        return self.engine._character_portals_cache.predecessors[
            self._charname][self.name][k]

    def __setitem__(self, k, v):
        self.engine._place_stat_cache[self._charname][k] = v
        self.engine._character_portals_cache.store(
            self._charname,
            self.name,
            k,
            PortalProxy(self.engine, self._charname, k, self.name)
        )
        self.engine.handle(
            command='set_place',
            char=self._charname,
            place=k,
            statdict=v,
            silent=True,
            branching=True
        )
        self.engine.handle(
            'set_portal',
            (self._charname, k, self.name),
            silent=True, branching=True
        )

    def __delitem__(self, k):
        self.engine.del_portal(self._charname, k, self.name)


class CharPredecessorsMappingProxy(MutableMapping):
    def __init__(self, engine_proxy, charname):
        self.engine = engine_proxy
        self.name = charname
        self._cache = {}

    def __contains__(self, k):
        return k in self.engine._character_portals_cache.predecessors[self.name]

    def __iter__(self):
        return iter(self.engine._character_portals_cache.predecessors[self.name])

    def __len__(self):
        return len(self.engine._character_portals_cache.predecessors[self.name])

    def __getitem__(self, k):
        if k not in self:
            raise KeyError(
                "No predecessors to {} (if it even exists)".format(k)
            )
        if k not in self._cache:
            self._cache[k] = PredecessorsProxy(self.engine, self.name, k)
        return self._cache[k]

    def __setitem__(self, k, v):
        for pred, proxy in v.items():
            self.engine._character_portals_cache.store(
                self.name,
                pred,
                k,
                proxy
            )
        self.engine.handle(
            command='character_set_node_predecessors',
            char=self.name,
            node=k,
            preds=v,
            silent=True,
            branching=True
        )

    def __delitem__(self, k):
        for v in self[k]:
            self.engine.del_portal(self.name, k, v)
        if k in self._cache:
            del self._cache[k]


class CharStatProxy(CachingEntityProxy):
    @property
    def _cache(self):
        return self.engine._char_stat_cache[self.name]

    def __init__(self, engine_proxy, character):
        self.name = character
        super().__init__(engine_proxy)

    def __eq__(self, other):
        return (
            isinstance(other, CharStatProxy) and
            self.engine is other.engine and
            self.name == other.name
        )

    def _get(self, k=None):
        if k is None:
            return self
        return self._cache[k]

    def _get_state(self):
        return self.engine.handle(
            command='character_stat_copy',
            char=self.name
        )

    def _set_item(self, k, v):
        self.engine.handle(
            command='set_character_stat',
            char=self.name,
            k=k, v=v,
            silent=True,
            branching=True
        )

    def _del_item(self, k):
        self.engine.handle(
            command='del_character_stat',
            char=self.name,
            k=k,
            silent=True,
            branching=True
        )


class RuleProxy(Signal):
    @staticmethod
    def _nominate(v):
        ret = []
        for whatever in v:
            if hasattr(whatever, 'name'):
                ret.append(whatever.name)
            else:
                assert isinstance(whatever, str)
                ret.append(whatever)
        return ret

    @property
    def _cache(self):
        return self.engine._rules_cache[self.name]

    @property
    def triggers(self):
        return self._cache.setdefault('triggers', [])

    @triggers.setter
    def triggers(self, v):
        self._cache['triggers'] = v
        self.engine.handle('set_rule_triggers', rule=self.name, triggers=self._nominate(v), silent=True)
        self.send(self, triggers=v)

    @property
    def prereqs(self):
        return self._cache.setdefault('prereqs', [])

    @prereqs.setter
    def prereqs(self, v):
        self._cache['prereqs'] = v
        self.engine.handle('set_rule_prereqs', rule=self.name, prereqs=self._nominate(v), silent=True)
        self.send(self, prereqs=v)

    @property
    def actions(self):
        return self._cache.setdefault('actions', [])

    @actions.setter
    def actions(self, v):
        self._cache['actions'] = v
        self.engine.handle('set_rule_actions', rule=self.name, actions=self._nominate(v), silent=True)
        self.send(self, actions=v)

    def __init__(self, engine, rulename):
        super().__init__()
        self.engine = engine
        self.name = self._name = rulename

    def __eq__(self, other):
        return (
            hasattr(other, 'name') and
            self.name == other.name
        )


class RuleBookProxy(MutableSequence, Signal):
    @property
    def _cache(self):
        return self.engine._rulebooks_cache.setdefault(self.name, [])

    def __init__(self, engine, bookname):
        super().__init__()
        self.engine = engine
        self.name = bookname
        self._proxy_cache = engine._rule_obj_cache

    def __iter__(self):
        for k in self._cache:
            if k not in self._proxy_cache:
                self._proxy_cache[k] = RuleProxy(self.engine, k)
            yield self._proxy_cache[k]

    def __len__(self):
        return len(self._cache)

    def __getitem__(self, i):
        k = self._cache[i]
        if k not in self._proxy_cache:
            self._proxy_cache[k] = RuleProxy(self.engine, k)
        return self._proxy_cache[k]

    def __setitem__(self, i, v):
        if isinstance(v, RuleProxy):
            v = v._name
        self._cache[i] = v
        self.engine.handle(
            command='set_rulebook_rule',
            rulebook=self.name,
            i=i,
            rule=v,
            silent=True,
            branching=True
        )
        self.send(self, i=i, val=v)

    def __delitem__(self, i):
        del self._cache[i]
        self.engine.handle(
            command='del_rulebook_rule',
            rulebook=self.name,
            i=i,
            silent=True,
            branching=True
        )
        self.send(self, i=i, val=None)

    def insert(self, i, v):
        if isinstance(v, RuleProxy):
            v = v._name
        self._cache.insert(i, v)
        self.engine.handle(
            command='ins_rulebook_rule',
            rulebook=self.name,
            i=i,
            rule=v,
            silent=True,
            branching=True
        )
        for j in range(i, len(self)):
            self.send(self, i=j, val=self[j])


class AvatarMapProxy(Mapping):
    rulebook = RulebookProxyDescriptor()
    engine = getatt('character.engine')

    def _get_default_rulebook_name(self):
        return self.character.name, 'avatar'

    def _get_rulebook_proxy(self):
        return self.engine._character_rulebooks_cache[self.character.name]['avatar']

    def _set_rulebook_proxy(self, rb):
        self.engine._character_rulebooks_cache[self.character.name]['avatar'] = rb

    def _set_rulebook(self, rb):
        self.engine.handle(
            'set_avatar_rulebook',
            char=self.character.name, rulebook=rb, silent=True, branching=True
        )

    def __init__(self, character):
        self.character = character

    def __iter__(self):
        yield from self.character.engine._character_avatars_cache[
            self.character.name]

    def __len__(self):
        return len(self.character.engine._character_avatars_cache[
            self.character.name])

    def __contains__(self, k):
        return k in self.character.engine._character_avatars_cache[
            self.character.name]

    def __getitem__(self, k):
        if k not in self:
            raise KeyError("{} has no avatar in {}".format(
                self.character.name, k
            ))
        return self.GraphAvatarsProxy(
            self.character, self.character.engine.character[k]
        )

    def __getattr__(self, attr):
        vals = self.values()
        if not vals:
            raise AttributeError(
                "No attribute {}, and no graph to delegate to".format(attr)
            )
        elif len(vals) > 1:
            raise AttributeError(
                "No attribute {}, and more than one graph".format(attr)
            )
        else:
            return getattr(next(iter(vals)), attr)

    class GraphAvatarsProxy(Mapping):
        def __init__(self, character, graph):
            self.character = character
            self.graph = graph

        def __iter__(self):
            yield from self.character.engine._character_avatars_cache[
                self.character.name][self.graph.name]

        def __len__(self):
            return len(self.character.engine._character_avatars_cache[
                self.character.name][self.graph.name])

        def __contains__(self, k):
            cache = self.character.engine._character_avatars_cache[
                self.character.name]
            return self.graph.name in cache and k in cache[self.graph.name]

        def __getitem__(self, k):
            if k not in self:
                raise KeyError("{} has no avatar {} in graph {}".format(
                    self.character.name, k, self.graph.name
                ))
            return self.graph.node[k]

        def __getattr__(self, attr):
            vals = self.values()
            if not vals:
                raise AttributeError(
                    "No attribute {}, "
                    "and no avatar to delegate to".format(attr)
                )
            elif len(vals) > 1:
                raise AttributeError(
                    "No attribute {}, and more than one avatar"
                )
            else:
                return getattr(next(iter(vals)), attr)


class CharacterProxy(MutableMapping):
    rulebook = RulebookProxyDescriptor()

    def _get_default_rulebook_name(self):
        return self.name, 'character'

    def _get_rulebook_proxy(self):
        return self.engine._character_rulebooks_cache[self.name]['character']

    def _set_rulebook_proxy(self, rb):
        self.engine._character_rulebooks_cache[self.name]['character'] = rb

    def _set_rulebook(self, rb):
        self.engine.handle(
            'set_character_rulebook',
            char=self.name, rulebook=rb, silent=True, branching=True
        )

    @reify
    def avatar(self):
        return AvatarMapProxy(self)

    def __init__(self, engine_proxy, charname):
        self.engine = engine_proxy
        self.name = charname
        self.adj = self.succ = self.portal = CharSuccessorsMappingProxy(
            self.engine, self.name
        )
        self.pred = self.preportal = CharPredecessorsMappingProxy(
            self.engine, self.name
        )
        self.thing = ThingMapProxy(self.engine, self.name)
        self.place = PlaceMapProxy(self.engine, self.name)
        self.node = NodeMapProxy(self.engine, self.name)
        self.stat = CharStatProxy(self.engine, self.name)

    def __bool__(self):
        return True

    def __eq__(self, other):
        if hasattr(other, 'engine'):
            oe = other.engine
        else:
            return False
        return (
            self.engine is oe and
            hasattr(other, 'name') and
            self.name == other.name
        )

    def __iter__(self):
        yield from self.engine.handle(
            command='character_nodes',
            char=self.name
        )

    def __len__(self):
        return self.engine.handle(
            command='character_nodes_len',
            char=self.name
        )

    def __contains__(self, k):
        if k == 'name':
            return True
        return k in self.node

    def __getitem__(self, k):
        if k == 'name':
            return self.name
        return self.node[k]

    def __setitem__(self, k, v):
        self.node[k] = v

    def __delitem__(self, k):
        del self.node[k]

    def _apply_delta(self, delta):
        delta = delta.copy()
        for node, ex in delta.pop('nodes', {}).items():
            if ex:
                if node not in self.node:
                    nodeval = delta.get('node_val', {}).get(node, None)
                    if nodeval and 'location' in nodeval:
                        self.thing._cache[node] = prox = ThingProxy(
                            self.engine, self.name, node, nodeval['location'],
                            nodeval.get('next_location'), nodeval.get('arrival_time'),
                            nodeval.get('next_arrival_time')
                        )
                        self.thing.send(self.thing, key=node, value=prox)
                    else:
                        self.place._cache[node] = prox = PlaceProxy(
                            self.engine, self.name, node
                        )
                        self.place.send(self.place, key=node, value=prox)
                    self.node.send(self.node, key=node, value=prox)
            else:
                if node in self.place._cache:
                    del self.place._cache[node]
                    self.place.send(self.place, key=node, value=None)
                elif node in self.thing._cache:
                    del self.thing._cache[node]
                    self.thing.send(self.thing, key=node, value=None)
                else:
                    self.engine.warning("Diff deleted {} but it was never created here".format(node))
                self.node.send(self.node, key=node, value=None)
        self.portal._apply_delta(delta.pop('edges', {}))
        for (node, nodedelta) in delta.pop('node_val', {}).items():
            if node not in self.engine._node_stat_cache[self.name]:
                self.engine._node_stat_cache[self.name][node] = nodedelta
            else:
                self.node[node]._apply_delta(nodedelta)
        for (orig, destdelta) in delta.pop('edge_val', {}).items():
            for (dest, portdelta) in destdelta.items():
                if orig in self.portal and dest in self.portal[orig]:
                    self.portal[orig][dest]._apply_delta(portdelta)
                else:
                    self.engine._portal_stat_cache[
                        self.name][orig][dest] = portdelta
        if delta.pop('character_rulebook', self.rulebook.name) != self.rulebook.name:
            self._set_rulebook_proxy(self.engine._rulebooks_cache[delta.pop('character_rulebook')])
        if delta.pop('avatar_rulebook', self.avatar.rulebook.name) != self.avatar.rulebook.name:
            self.avatar._set_rulebook_proxy(self.engine._rulebooks_cache[delta.pop('avatar_rulebook')])
        if delta.pop('character_thing_rulebook', self.thing.rulebook.name) != self.thing.rulebook.name:
            self.thing._set_rulebook_proxy(self.engine._rulebooks_cache[delta.pop('character_thing_rulebook')])
        if delta.pop('character_place_rulebook', self.place.rulebook.name) != self.place.rulebook.name:
            self.place._set_rulebook_proxy(self.engine._rulebooks_cache[delta.pop('character_place_rulebook')])
        if delta.pop('character_portal_rulebook', self.portal.rulebook.name) != self.portal.rulebook.name:
            self.portal._set_rulebook_proxy(self.engine._rulebooks_cache[delta.pop('character_portal_rulebook')])
        for noden, rb in delta.pop('node_rulebooks', {}).items():
            node = self.node[noden]
            if node.rulebook.name != rb:
                node._set_rulebook_proxy(self.engine._rulebooks_cache[rb])
        portrb = delta.pop('portal_rulebooks', {})
        for orign in portrb:
            for destn, rb in portrb[orign].items():
                port = self.portal[orign][destn]
                if port.rulebook.name != rb:
                    port._set_rulebook_proxy(self.engine._rulebooks_cache[rb])
        self.stat._apply_delta(delta)

    def add_place(self, name, **kwargs):
        self[name] = kwargs

    def add_places_from(self, seq):
        self.engine.handle(
            command='add_places_from',
            char=self.name,
            seq=list(seq),
            silent=True,
            branching=True
        )
        for pln in seq:
            self.place._cache[pln] = PlaceProxy(
                self.engine, self.name, pln
            )

    def add_nodes_from(self, seq):
        self.add_places_from(seq)

    def add_thing(self, name, location, next_location=None, **kwargs):
        self.engine.handle(
            command='add_thing',
            char=self.name,
            thing=name,
            loc=location,
            next_loc=next_location,
            statdict=kwargs,
            silent=True,
            branching=True
        )
        self.thing._cache[name] = ThingProxy(
            self.engine, self.name, name, location, next_location,
            self.engine.tick, None
        )

    def add_things_from(self, seq):
        self.engine.handle(
            command='add_things_from',
            char=self.name,
            seq=list(seq),
            silent=True,
            branching=True
        )
        for thn in seq:
            self.thing._cache[thn] = ThingProxy(
                self.engine, self.name, thn
            )

    def new_place(self, name, **kwargs):
        self.add_place(name, **kwargs)
        return self.place[name]

    def new_thing(self, name, location, next_location=None, **kwargs):
        self.add_thing(name, location, next_location, **kwargs)
        return self.thing[name]

    def place2thing(self, node, location, next_location=None):
        self.engine.handle(
            command='place2thing',
            char=self.name,
            node=node,
            loc=location,
            next_loc=next_location,
            silent=True,
            branching=True
        )

    def add_portal(self, origin, destination, symmetrical=False, **kwargs):
        self.engine.handle(
            command='add_portal',
            char=self.name,
            orig=origin,
            dest=destination,
            symmetrical=symmetrical,
            statdict=kwargs,
            silent=True,
            branching=True
        )
        self.engine._character_portals_cache.store(
            self.name,
            origin,
            destination,
            PortalProxy(
                self.engine,
                self.name,
                origin,
                destination
            )
        )

    def add_portals_from(self, seq, symmetrical=False):
        l = list(seq)
        self.engine.handle(
            command='add_portals_from',
            char=self.name,
            seq=l,
            symmetrical=symmetrical,
            silent=True,
            branching=True
        )
        for (origin, destination) in l:
            if origin not in self.portal._cache:
                self.portal._cache[origin] = SuccessorsProxy(
                    self.engine,
                    self.name,
                    origin
                )
            self.portal[origin]._cache[destination] = PortalProxy(
                self.engine,
                self.name,
                origin,
                destination
            )

    def new_portal(self, origin, destination, symmetrical=False, **kwargs):
        self.add_portal(origin, destination, symmetrical, **kwargs)
        return self.portal[origin][destination]

    def portals(self):
        yield from self.engine.handle(
            command='character_portals',
            char=self.name
        )

    def add_avatar(self, graph, node):
        self.engine.handle(
            command='add_avatar',
            char=self.name,
            graph=graph,
            node=node,
            silent=True,
            branching=True
        )

    def del_avatar(self, graph, node):
        self.engine.handle(
            command='del_avatar',
            char=self.name,
            graph=graph,
            node=node,
            silent=True,
            branching=True
        )

    def avatars(self):
        yield from self.engine.handle(
            command='character_avatars',
            char=self.name
        )

    def facade(self):
        return Facade(self)


class CharacterMapProxy(MutableMapping, Signal):
    def __init__(self, engine_proxy):
        super().__init__()
        self.engine = engine_proxy

    def __iter__(self):
        return iter(self.engine._char_cache.keys())

    def __contains__(self, k):
        return k in self.engine._char_cache

    def __len__(self):
        return len(self.engine._char_cache)

    def __getitem__(self, k):
        return self.engine._char_cache[k]

    def __setitem__(self, k, v):
        self.engine.handle(
            command='set_character',
            char=k,
            data=v,
            silent=True,
            branching=True
        )
        self.engine._char_cache[k] = CharacterProxy(self.engine, k)
        self.send(self, key=k, val=v)

    def __delitem__(self, k):
        self.engine.handle(
            command='del_character',
            char=k,
            silent=True,
            branching=True
        )
        if k in self.engine._char_cache:
            del self.engine._char_cache[k]
        self.send(self, key=k, val=None)


class ProxyLanguageDescriptor(AbstractLanguageDescriptor):
    def _get_language(self, inst):
        if not hasattr(inst, '_language'):
            inst._language = inst.engine.handle(command='get_language')
        return inst._language

    def _set_language(self, inst, val):
        inst._language = val
        delta = inst.engine.handle(command='set_language', lang=val)
        cache = inst._cache
        for k, v in delta.items():
            if k in cache:
                if v is None:
                    del cache[k]
                elif cache[k] != v:
                    cache[k] = v
                    inst.send(inst, key=k, string=v)
            elif v is not None:
                cache[k] = v
                inst.send(inst, key=k, string=v)


class StringStoreProxy(Signal):
    language = ProxyLanguageDescriptor()

    def __init__(self, engine_proxy):
        super().__init__()
        self.engine = engine_proxy
        self._cache = self.engine.handle('strings_delta')

    def __getattr__(self, k):
        try:
            return self._cache[k]
        except KeyError:
            raise AttributeError

    def __setattr__(self, k, v):
        if k in ('_cache', 'engine', 'language', '_language', 'receivers',
                 '_by_receiver', '_by_sender', '_weak_senders'):
            super().__setattr__(k, v)
            return
        self._cache[k] = v
        self.engine.handle(command='set_string', k=k, v=v, silent=True)
        self.send(self, key=k, string=v)

    def __delattr__(self, k):
        del self._cache[k]
        self.engine.handle(command='del_string', k=k, silent=True)
        self.send(self, key=k, string=None)

    def lang_items(self, lang=None):
        if lang is None or lang == self.language:
            yield from self._cache.items()
        else:
            yield from self.engine.handle(
                command='get_string_lang_items', lang=lang
            )


class EternalVarProxy(MutableMapping):
    def __init__(self, engine_proxy):
        self.engine = engine_proxy
        self._cache = self.engine.handle('eternal_delta')

    def __contains__(self, k):
        return k in self._cache

    def __iter__(self):
        yield from self.engine.handle(command='eternal_keys')

    def __len__(self):
        return self.engine.handle(command='eternal_len')

    def __getitem__(self, k):
        return self.engine.handle(command='get_eternal', k=k)

    def __setitem__(self, k, v):
        self._cache[k] = v
        self.engine.handle(
            'set_eternal',
            k=k, v=v,
            silent=True
        )

    def __delitem__(self, k):
        del self._cache[k]
        self.engine.handle(
            command='del_eternal',
            k=k,
            silent=True
        )

    def _update_cache(self, data):
        for k, v in data.items():
            if v is None:
                del self._cache[k]
            else:
                self._cache[k] = v


class GlobalVarProxy(MutableMapping):
    def __init__(self, engine_proxy):
        self.engine = engine_proxy
        self._cache = self.engine.handle('universal_delta')

    def __iter__(self):
        return iter(self._cache)

    def __len__(self):
        return len(self._cache)

    def __getitem__(self, k):
        return self._cache[k]

    def __setitem__(self, k, v):
        self._cache[k] = v
        self.engine.handle('set_universal', k=k, v=v, silent=True, branching=True)

    def __delitem__(self, k):
        del self._cache[k]
        self.engine.handle('del_universal', k=k, silent=True, branching=True)

    def _update_cache(self, data):
        for k, v in data.items():
            if v is None:
                del self._cache[k]
            else:
                self._cache[k] = v


class AllRuleBooksProxy(Mapping):
    @property
    def _cache(self):
        return self.engine._rulebooks_cache

    def __init__(self, engine_proxy):
        self.engine = engine_proxy

    def __iter__(self):
        yield from self._cache

    def __len__(self):
        return len(self._cache)

    def __contains__(self, k):
        return k in self._cache

    def __getitem__(self, k):
        if k not in self:
            self.engine.handle('new_empty_rulebook', rulebook=k, silent=True)
            self._cache[k] = []
        return self._cache[k]


class AllRulesProxy(Mapping):
    @property
    def _cache(self):
        return self.engine._rules_cache

    def __init__(self, engine_proxy):
        self.engine = engine_proxy
        self._proxy_cache = {}

    def __iter__(self):
        return iter(self._cache)

    def __len__(self):
        return len(self._cache)

    def __contains__(self, k):
        return k in self._cache

    def __getitem__(self, k):
        if k not in self:
            raise KeyError("No rule: {}".format(k))
        if k not in self._proxy_cache:
            self._proxy_cache[k] = RuleProxy(self.engine, k)
        return self._proxy_cache[k]

    def new_empty(self, k):
        self.engine.handle(command='new_empty_rule', rule=k, silent=True)
        self._cache[k] = {'triggers': [], 'prereqs': [], 'actions': []}
        self._proxy_cache[k] = RuleProxy(self.engine, k)
        return self._proxy_cache[k]


class FuncProxy(object):
    __slots__ = 'store', 'func'

    def __init__(self, store, func):
        self.store = store
        self.func = func

    def __call__(self, *args, silent=False, cb=None, **kwargs):
        return self.store.engine.handle(
            'call_stored_function',
            store=self.store._store,
            func=self.func,
            args=args,
            kwargs=kwargs,
            silent=silent,
            cb=cb
        )

    def __str__(self):
        return self.store._cache[self.func]


class FuncStoreProxy(Signal):
    def __init__(self, engine_proxy, store):
        super().__init__()
        self.engine = engine_proxy
        self._store = store
        self._cache = self.engine.handle('source_delta', store=store)

    def __getattr__(self, k):
        if k in self._cache:
            return FuncProxy(self, k)
        else:
            raise AttributeError

    def __setattr__(self, func_name, source):
        if func_name in ('engine', '_store', '_cache', 'receivers',
                         '_by_sender', '_by_receiver', '_weak_senders'):
            super().__setattr__(func_name, source)
            return
        self.engine.handle(
            command='store_source',
            store=self._store,
            v=source,
            name=func_name,
            silent=True
        )
        self._cache[func_name] = source

    def __delattr__(self, func_name):
        self.engine.handle(
            command='del_source', store=self._store, k=func_name, silent=True
        )
        del self._cache[func_name]


class ChangeSignatureError(TypeError):
    pass


class PortalObjCache(object):
    def __init__(self):
        self.successors = StructuredDefaultDict(2, PortalProxy)
        self.predecessors = StructuredDefaultDict(2, PortalProxy)

    def store(self, char, u, v, obj):
        self.successors[char][u][v] = obj
        self.predecessors[char][v][u] = obj

    def delete(self, char, u, v):
        del self.successors[char][u][v]
        del self.predecessors[char][v][u]


class TimeSignal(Signal):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine

    def __iter__(self):
        yield self.engine.branch
        yield self.engine.tick

    def __len__(self):
        return 2

    def __getitem__(self, i):
        if i in ('branch', 0):
            return self.engine.branch
        if i in ('tick', 1):
            return self.engine.tick

    def __setitem__(self, i, v):
        if i in ('branch', 0):
            self.engine.time_travel(v, self.engine.tick)
        if i in ('tick', 1):
            self.engine.time_travel(self.engine.branch, v)


class TimeDescriptor(object):
    times = {}

    def __get__(self, inst, cls):
        if id(inst) not in self.times:
            self.times[id(inst)] = TimeSignal(inst)
        return self.times[id(inst)]

    def __set__(self, inst, val):
        inst.time_travel(*val)


class EngineProxy(AbstractEngine):
    """An engine-like object for controlling the actual LiSE engine in another process.

    Don't instantiate this directly. Use :class:`EngineProcessManager` instead.
    The ``start`` method will return an :class:`EngineProxy` instance.

    """
    char_cls = CharacterProxy
    thing_cls = ThingProxy
    place_cls = PlaceProxy
    portal_cls = PortalProxy
    time = TimeDescriptor()

    @property
    def branch(self):
        return self._branch

    @branch.setter
    def branch(self, v):
        self.time_travel(v, self.turn)

    @property
    def turn(self):
        return self._turn

    @turn.setter
    def turn(self, v):
        self.time_travel(self.branch, v)

    def __init__(
            self, handle_out, handle_in, logger,
            do_game_start=False,  install_modules=[]
    ):
        self._handle_out = handle_out
        self._handle_out_lock = Lock()
        self._handle_in = handle_in
        self._handle_in_lock = Lock()
        self._handle_lock = Lock()
        self.send(self.json_dump({'command': 'get_watched_btt'}))
        self._branch, self._turn, self._tick = self.json_load(self.recv()[-1])
        self.logger = logger
        self.method = FuncStoreProxy(self, 'method')
        self.eternal = EternalVarProxy(self)
        self.universal = GlobalVarProxy(self)
        self.character = CharacterMapProxy(self)
        self.string = StringStoreProxy(self)
        self.rulebook = AllRuleBooksProxy(self)
        self.rule = AllRulesProxy(self)
        self.action = FuncStoreProxy(self, 'action')
        self.prereq = FuncStoreProxy(self, 'prereq')
        self.trigger = FuncStoreProxy(self, 'trigger')
        self.function = FuncStoreProxy(self, 'function')

        for module in install_modules:
            self.handle('install_module',  module=module)  # not silenced
        if do_game_start:
            # not silenced; mustn't do anything before the game has started
            self.handle('do_game_start')

        self._node_stat_cache = StructuredDefaultDict(1, dict)
        self._portal_stat_cache = StructuredDefaultDict(2, dict)
        self._char_stat_cache = PickyDefaultDict(dict)
        self._things_cache = StructuredDefaultDict(1, ThingProxy)
        self._character_places_cache = StructuredDefaultDict(1, PlaceProxy)
        self._character_rulebooks_cache = StructuredDefaultDict(
            1, RuleBookProxy, kwargs_munger=lambda inst, k: {
                'engine': self,
                'bookname': (inst.key, k)
            }
        )
        self._char_node_rulebooks_cache = StructuredDefaultDict(
            1, RuleBookProxy, kwargs_munger=lambda inst, k: {
                'engine': self,
                'bookname': (inst.key, k)
            }
        )
        self._char_port_rulebooks_cache = StructuredDefaultDict(
            2, RuleBookProxy, kwargs_munger=lambda inst, k: {
                'engine': self,
                'bookname': (inst.parent.key, inst.key, k)
            }
        )
        self._character_portals_cache = PortalObjCache()
        self._character_avatars_cache = PickyDefaultDict(dict)
        self._rule_obj_cache = {}
        self._rules_cache = self.handle('all_rules_delta')
        for rule in self._rules_cache:
            self._rule_obj_cache[rule] = RuleProxy(self, rule)
        self._rulebook_obj_cache = {}
        self._rulebooks_cache = self.handle('all_rulebooks_delta')
        self._char_cache = {}
        deltas = self.handle('get_char_deltas', chars='all')
        for char in deltas:
            self._char_cache[char] = CharacterProxy(self, char)
            for origin, destinations in deltas[
                    char].pop('edge_val', {}).items():
                for destination,  stats in destinations.items():
                    self._portal_stat_cache[char][origin][destination] = stats
            for node,  stats in deltas[char].pop('node_val', {}).items():
                self._node_stat_cache[char][node] = stats
            self._character_avatars_cache[char] = deltas[char].pop('avatars', {})
            for rbtype, rb in deltas[char].pop('rulebooks', {}).items():
                if rb in self._rulebook_obj_cache:
                    self._character_rulebooks_cache[char][rbtype] \
                        = self._rulebook_obj_cache[rb]
                else:
                    self._character_rulebooks_cache[char][rbtype] \
                        = self._rulebook_obj_cache[rb] \
                        = RuleBookProxy(self, rb)
            for node, rb in deltas[char].pop('node_rulebooks', {}).items():
                if rb in self._rulebook_obj_cache:
                    self._char_node_rulebooks_cache[char][node] \
                        = self._rulebook_obj_cache[rb]
                else:
                    self._char_node_rulebooks_cache[char][node] \
                        = self._rulebook_obj_cache[rb] \
                        = RuleBookProxy(self, rb)
            for origin, destinations in deltas[
                    char].pop('portal_rulebooks', {}).items():
                for destination, rulebook in destinations.items():
                    if rulebook in self._rulebook_obj_cache:
                        self._char_port_rulebooks_cache[
                            char][origin][destination
                        ] = self._rulebook_obj_cache[rulebook]
                    else:
                        self._char_port_rulebooks_cache[
                            char][origin][destination] \
                            = self._rulebook_obj_cache[rulebook] \
                            = RuleBookProxy(self, rulebook)
            for node, ex in deltas[char].pop('nodes', {}).items():
                if ex:
                    noded = self._node_stat_cache[char].get(node)
                    if noded and 'location' in noded:
                        self._things_cache[char][node] = ThingProxy(
                            self, char, node, noded['location'],
                            noded.get('next_location'), noded.get('arrival_time'),
                            noded.get('next_arrival_time')
                        )
                    else:
                        self._character_places_cache[char][node] = PlaceProxy(
                            self, char, node
                        )
            for orig, dests in deltas[char].pop('edges', {}).items():
                for dest, ex in dests.items():
                    if ex:
                        self._character_portals_cache.store(
                            char, orig, dest, PortalProxy(self, char, orig, dest)
                        )
            self._char_stat_cache[char] = deltas[char]

    def delistify(self, obj):
        if not (isinstance(obj, list) or isinstance(obj, tuple)):
            return obj
        if obj[0] == 'character':
            name = self.delistify(obj[1])
            if name not in self._char_cache:
                self._char_cache[name] = CharacterProxy(self, name)
            return self._char_cache[name]
        elif obj[0] == 'place':
            charname = self.delistify(obj[1])
            nodename = self.delistify(obj[2])
            try:
                return self._character_places_cache[charname][nodename]
            except KeyError:
                return self._character_places_cache.setdefault(charname, {}).setdefault(
                    nodename, PlaceProxy(self, charname, nodename)
                )
        elif obj[0] == 'thing':
            charname, nodename, loc, nxtloc, arrt, nxtarrt = map(self.delistify, obj[1:])
            try:
                return self._character_things_cache[charname][nodename]
            except KeyError:
                return self._character_things_cache.setdefault(charname, {}).setdefault(
                    nodename, ThingProxy(self, charname, nodename, loc, nxtloc, arrt, nxtarrt)
                )
        elif obj[0] == 'portal':
            charname = self.delistify(obj[1])
            origname = self.delistify(obj[2])
            destname = self.delistify(obj[3])
            cache = self._character_portals_cache
            if not (
                    charname in cache and
                    origname in cache[charname] and
                    destname in cache[charname][origname]
            ):
                cache[charname][origname][destname] \
                    = PortalProxy(self, charname, origname, destname)
            return cache[charname][origname][destname]
        else:
            return super().delistify(obj)

    def send(self, obj, blocking=True, timeout=-1):
        self._handle_out_lock.acquire(blocking, timeout)
        self._handle_out.send(obj)
        self._handle_out_lock.release()

    def recv(self, blocking=True, timeout=-1):
        self._handle_in_lock.acquire(blocking, timeout)
        data = self._handle_in.recv()
        self._handle_in_lock.release()
        return data

    def debug(self, msg):
        self.logger.debug(msg)

    def info(self, msg):
        self.logger.info(msg)

    def warning(self, msg):
        self.logger.warning(msg)

    def error(self, msg):
        self.logger.error(msg)

    def critical(self, msg):
        self.logger.critical(msg)

    def handle(self, cmd=None, **kwargs):
        """Send a command to the LiSE core.

        The only positional argument should be the name of a
        method in :class:``EngineHandle``. All keyword arguments
        will be passed to it, with the exceptions of
        ``cb``, ``branching``, and ``silent``.

        With ``silent=True``, don't wait for a result; return
        ``None`` immediately. This is best for when you want to make
        some change to the game state and already know what effect it
        will have.

        With ``branching=True``, handle paradoxes by creating new
        branches of history. I will switch to the new branch if needed.
        If I have an attribute ``branching_cb``, I'll call it if and
        only if the branch changes upon completing a command with
        ``branching=True``.

        With a function ``cb``, I will call ``cb`` when I get
        a result. If ``silent=True`` this will happen in a thread.
        ``cb`` will be called with keyword arguments ``command``,
        the same command you asked for; ``result``, the value returned
        by it, possibly ``None``; and the present ``branch``,
        ``turn``, and ``tick``, possibly different than when you called
        ``handle``.

        """
        if 'command' in kwargs:
            cmd = kwargs['command']
        elif cmd:
            kwargs['command'] = cmd
        else:
            raise TypeError("No command")
        branching = kwargs.get('branching', False)
        cb = kwargs.pop('cb', None)
        self._handle_lock.acquire()
        if 'silent' not in kwargs:
            kwargs['silent'] = False
        if kwargs['silent']:
            if branching or cb:
                # I'll still execute the command asynchronously,
                # and *this* method won't return anything, but
                # the subprocess should still return a value, so don't
                # silence *that*
                del kwargs['silent']
            self.send(self.json_dump(kwargs))
            if branching:
                self._branching_thread = Thread(
                    target=self._branching, args=[cb], daemon=True
                )
                self._branching_thread.start()
                return
            if cb:
                self._callback_thread = Thread(
                    target=self._callback, args=[cb], daemon=True
                )
                self._callback_thread.start()
                return
        else:
            self.send(self.json_dump(kwargs))
            command, branch, turn, tick, result = self.recv()
            assert cmd == command, \
                "Sent command {} but received results for {}".format(
                    cmd, command
                )
            self._handle_lock.release()
            r = self.json_load(result)
            if (branch, turn, tick) != self.btt():
                self._branch = branch
                self._turn = turn
                self._tick = tick
                self.time.send(self, branch=branch, turn=turn, tick=tick)
            if cb:
                cb(command, branch, turn, tick, **r)
            return r
        self._handle_lock.release()

    def _callback(self, cb):
        command, branch, turn, tick, result = self.recv()
        self._handle_lock.release()
        cb(command, branch, turn, tick, **self.json_load(result))

    def _branching(self, cb=None):
        command, branch, turn, tick, result = self.recv()
        self._handle_lock.release()
        r = self.json_load(result)
        if branch != self._branch:
            self._branch = branch
            self._turn = turn
            self._tick = tick
            self.time.send(self, branch=branch, turn=turn, tick=tick)
            if hasattr(self, 'branching_cb'):
                self.branching_cb(command, branch, turn, tick, **r)
        if cb:
            cb(command, branch, turn, tick, **r)

    def _call_with_recv(self, *cbs, **kwargs):
        cmd, branch, turn, tick, res = self.recv()
        received = self.json_load(res)
        for cb in cbs:
            cb(cmd, branch, turn, tick, received, **kwargs)
        return received

    def _upd_caches(self, *args, **kwargs):
        deleted = set(self.character.keys())
        result, deltas = args[-1]
        self.eternal._update_cache(deltas.pop('eternal', {}))
        self.universal._update_cache(deltas.pop('universal', {}))
        # I think if you travel back to before a rule was created it'll show up empty
        # That's ok I guess
        for rule, delta in deltas.pop('rules', {}).items():
            if rule in self._rules_cache:
                self._rules_cache[rule].update(delta)
            else:
                delta.setdefault('triggers', [])
                delta.setdefault('prereqs', [])
                delta.setdefault('actions', [])
                self._rules_cache[rule] = delta
            if rule not in self._rule_obj_cache:
                self._rule_obj_cache[rule] = RuleProxy(self, rule)
            ruleproxy = self._rule_obj_cache[rule]
            ruleproxy.send(ruleproxy, **delta)
        rulebookdeltas = deltas.pop('rulebooks', {})
        self._rulebooks_cache.update(rulebookdeltas)
        for rulebook, delta in rulebookdeltas.items():
            if rulebook not in self._rulebook_obj_cache:
                self._rulebook_obj_cache = RuleBookProxy(self, rulebook)
            rulebookproxy = self._rulebook_obj_cache[rulebook]
            # the "delta" is just the rules list, for now
            rulebookproxy.send(rulebookproxy, rules=delta)
        for (char, chardelta) in deltas.items():
            if char not in self._char_cache:
                self._char_cache[char] = CharacterProxy(self, char)
            chara = self.character[char]
            chara._apply_delta(chardelta)
            deleted.discard(char)
        if kwargs.get('no_del'):
            return
        for char in deleted:
            del self._char_cache[char]

    def btt(self):
        return self._branch, self._turn, self._tick

    def _set_time(self, cmd, branch, turn, tick, res, **kwargs):
        self._branch = branch
        self._turn = turn
        self._tick = tick
        self.time.send(self, branch=branch, turn=turn, tick=tick)

    def _pull_async(self, chars, cb):
        if not callable(cb):
            raise TypeError("Uncallable callback")
        self.send(self.json_dump({
            'silent': False,
            'command': 'get_char_deltas',
            'chars': chars
        }))
        cbs = [self._upd_caches]
        if cb:
            cbs.append(cb)
        self._call_with_recv(cbs)

    def pull(self, chars='all', cb=None, sync=True):
        """Update the state of all my proxy objects from the real objects."""
        if sync:
            deltas = self.handle('get_char_deltas', chars=chars)
            self._upd_caches(deltas)
            if cb:
                cb(deltas)
        else:
            Thread(
                target=self._pull_async,
                args=(chars, cb)
            ).start()

    # TODO: make this into a Signal, like it is in the LiSE core
    def next_turn(self, cb=None, silent=False):
        if not callable(cb):
            raise TypeError("Uncallable callback")
        if silent:
            self.handle(command='next_turn', silent=True, cb=cb)
        elif cb:
            self.send(self.json_dump({
                'silent': False,
                'command': 'next_turn'
            }))
            args = [partial(self._upd_caches, no_del=True), self._set_time, cb]
            if silent:
                Thread(
                    target=self._call_with_recv,
                    args=args
                ).start()
            else:
                return self._call_with_recv(*args)
        else:
            ret = self.handle(command='next_turn')
            self.time.send(self, branch=ret['branch'], turn=ret['turn'], tick=ret['tick'])
            return ret

    def time_travel(self, branch, turn, tick=None, chars='all', cb=None, block=True):
        if cb and not chars:
            raise TypeError("Callbacks require char name")
        if cb is not None and not callable(cb):
            raise TypeError("Uncallable callback")
        if chars:
            args = [self._set_time, self._upd_caches]
            if cb:
                args.append(cb)
            self._time_travel_thread = Thread(
                target=self._call_with_recv,
                args=args,
                kwargs={'no_del': True}
            )
            self._time_travel_thread.start()
            self.send(self.json_dump({
                'command': 'time_travel',
                'silent': False,
                'branch': branch,
                'turn': turn,
                'tick': tick,
                'chars': chars
            }))
            if block:
                self._time_travel_thread.join()
        else:
            self.handle(
                command='time_travel',
                branch=branch,
                turn=turn,
                tick=tick,
                chars=[],
                silent=True
            )

    def add_character(self, char, data={}, **attr):
        if char in self._char_cache:
            raise KeyError("Character already exists")
        assert char not in self._char_stat_cache
        self._char_cache[char] = CharacterProxy(self, char)
        self._char_stat_cache[char] = attr
        placedata = data.get('place', data.get('node', {}))
        for place, stats in placedata.items():
            assert place not in self._character_places_cache[char]
            assert place not in self._node_stat_cache[char]
            self._character_places_cache[char][place] \
                = PlaceProxy(self.engine,  char,  place)
            self._node_stat_cache[char][place] = stats
        thingdata = data.get('thing',  {})
        for thing, stats in thingdata.items():
            assert thing not in self._things_cache[char]
            assert thing not in self._node_stat_cache[char]
            if 'location' not in stats:
                raise ValueError('Things must always have locations')
            if 'arrival_time' in stats or 'next_arrival_time' in stats:
                raise ValueError('The arrival_time stats are read-only')
            loc = stats.pop('location')
            nxtloc = stats.pop('next_location') \
                     if 'next_location' in stats else None
            self._things_cache[char][thing] \
                = ThingProxy(loc, nxtloc, self.engine.rev, None)
            self._node_stat_cache[char][thing] = stats
        portdata = data.get('edge', data.get('portal', data.get('adj',  {})))
        for orig, dests in portdata.items():
            assert orig not in self._character_portals_cache[char]
            assert orig not in self._portal_stat_cache[char]
            for dest, stats in dests.items():
                assert dest not in self._character_portals_cache[char][orig]
                assert dest not in self._portal_stat_cache[char][orig]
                self._character_portals_cache[char][orig][dest] \
                    = PortalProxy(self.engine, char, orig, dest)
                self._portal_stat_cache[char][orig][dest] = stats
        self.handle(
            command='add_character', char=char, data=data, attr=attr,
            silent=True, branching=True
        )

    def new_character(self, char, **attr):
        self.add_character(char, **attr)
        return self._char_cache[char]

    def del_character(self, char):
        if char not in self._char_cache:
            raise KeyError("No such character")
        del self._char_cache[char]
        del self._char_stat_cache[char]
        del self._character_places_cache[char]
        del self._things_cache[char]
        del self._character_portals_cache[char]
        self.handle(command='del_character', char=char, silent=True, branching=True)

    def del_node(self, char, node):
        if char not in self._char_cache:
            raise KeyError("No such character")
        if node not in self._character_places_cache[char] and \
           node not in self._things_cache[char]:
            raise KeyError("No such node")
        if node in self._things_cache[char]:
            del self._things_cache[char][node]
        if node in self._character_places_cache[char]:  # just to be safe
            del self._character_places_cache[char][node]
        self.handle(
            command='del_node',
            char=char,
            node=node,
            silent=True,
            branching=True
        )

    def del_portal(self, char, orig, dest):
        if char not in self._char_cache:
            raise KeyError("No such character")
        self._character_portals_cache.delete(char, orig, dest)
        self.handle(
            command='del_portal',
            char=char,
            orig=orig,
            dest=dest,
            silent=True,
            branching=True
        )

    def commit(self):
        self.handle('commit', silent=True)

    def close(self):
        self.handle(command='close')
        self.send('shutdown')


def subprocess(
    args, kwargs, handle_out_pipe, handle_in_pipe, logq, loglevel
):
    def log(typ, data):
        if typ == 'command':
            (cmd, kvs) = data
            logs = "LiSE proc {}: calling {}({})".format(
                getpid(),
                cmd,
                ",  ".join("{}={}".format(k,  v) for k,  v in kvs.items())
            )
        else:
            logs = "LiSE proc {}: returning {} (of type {})".format(
                getpid(),
                data,
                repr(type(data))
            )
        logq.put(('debug', logs))
    engine_handle = EngineHandle(args, kwargs, logq, loglevel=loglevel)

    while True:
        inst = handle_out_pipe.recv()
        if inst == 'shutdown':
            handle_out_pipe.close()
            handle_in_pipe.close()
            logq.close()
            return 0
        instruction = engine_handle.json_load(inst)
        silent = instruction.pop('silent',  False)
        cmd = instruction.pop('command')
        log('command', (cmd, instruction))

        branching = instruction.pop('branching', False)
        if branching:
            try:
                r = getattr(engine_handle, cmd)(**instruction)
            except HistoryError:
                engine_handle.increment_branch()
                r = getattr(engine_handle, cmd)(**instruction)
        else:
            r = getattr(engine_handle, cmd)(**instruction)
        if silent:
            continue
        log('result', r)
        handle_in_pipe.send((
            cmd, engine_handle.branch, engine_handle.turn, engine_handle.tick,
            engine_handle.json_dump(r)
        ))
        if hasattr(engine_handle, '_after_ret'):
            engine_handle._after_ret()
            del engine_handle._after_ret


class RedundantProcessError(ProcessError):
    """Raised when EngineProcessManager is asked to start a process that
    has already started.

    """


class EngineProcessManager(object):
    def start(self, *args, **kwargs):
        if hasattr(self, 'engine_proxy'):
            raise RedundantProcessError("Already started")
        (handle_out_pipe_recv, self._handle_out_pipe_send) = Pipe(duplex=False)
        (handle_in_pipe_recv, handle_in_pipe_send) = Pipe(duplex=False)
        self.logq = Queue()
        handlers = []
        logl = {
            'debug': logging.DEBUG,
            'info': logging.INFO,
            'warning': logging.WARNING,
            'error': logging.ERROR,
            'critical': logging.CRITICAL
        }
        loglevel = logging.INFO
        if 'loglevel' in kwargs:
            if kwargs['loglevel'] in logl:
                loglevel = logl[kwargs['loglevel']]
            else:
                loglevel = kwargs['loglevel']
            del kwargs['loglevel']
        if 'logger' in kwargs:
            self.logger = kwargs['logger']
            del kwargs['logger']
        else:
            self.logger = logging.getLogger(__name__)
            stdout = logging.StreamHandler(sys.stdout)
            stdout.set_name('stdout')
            handlers.append(stdout)
            handlers[0].setLevel(loglevel)
        if 'logfile' in kwargs:
            try:
                fh = logging.FileHandler(kwargs['logfile'])
                handlers.append(fh)
                handlers[-1].setLevel(loglevel)
            except OSError:
                pass
            del kwargs['logfile']
        do_game_start = kwargs.pop('do_game_start') \
                        if 'do_game_start' in kwargs else False
        install_modules = kwargs.pop('install_modules') \
                          if 'install_modules' in kwargs else []
        formatter = logging.Formatter(
            fmt='[{levelname}] LiSE.proxy({process})\t{message}',
            style='{'
        )
        for handler in handlers:
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
        self._p = Process(
            name='LiSE Life Simulator Engine (core)',
            target=subprocess,
            args=(
                args,
                kwargs,
                handle_out_pipe_recv,
                handle_in_pipe_send,
                self.logq,
                loglevel
            )
        )
        self._p.daemon = True
        self._p.start()
        self._logthread = Thread(
            target=self.sync_log_forever,
            name='log',
            daemon=True
        )
        self._logthread.start()
        self.engine_proxy = EngineProxy(
            self._handle_out_pipe_send,
            handle_in_pipe_recv,
            self.logger,
            do_game_start,
            install_modules
        )
        return self.engine_proxy

    def sync_log(self, limit=None, block=True):
        n = 0
        while limit is None or n < limit:
            try:
                (level, message) = self.logq.get(block=block)
                if isinstance(level, int):
                    level = {
                        10: 'debug',
                        20: 'info',
                        30: 'warning',
                        40: 'error',
                        50: 'critical'
                    }[level]
                getattr(self.logger, level)(message)
                n += 1
            except Empty:
                return

    def sync_log_forever(self):
        while True:
            self.sync_log(1)

    def shutdown(self):
        self.engine_proxy.close()
        self._p.join()
        del self.engine_proxy
