# This file is part of LiSE, a framework for life simulation games.
# Copyright (c) Zachary Spector,  zacharyspector@gmail.com
"""The "engine" of LiSE is an object relational mapper with special
stores for game data and entities, as well as properties for manipulating the
flow of time.

"""
from random import Random
from collections import defaultdict
from functools import partial
from sqlite3 import connect
from json import dumps, loads, JSONEncoder
from gorm import ORM as gORM
from gorm.pickydict import StructuredDefaultDict
from gorm.window import WindowDict
from gorm.reify import reify
from .xcollections import (
    StringStore,
    FunctionStore,
    GlobalVarMapping,
    CharacterMapping
)
from .character import Character
from .node import Node
from .portal import Portal
from .rule import AllRuleBooks, AllRules
from .query import Query, QueryEngine
from .util import getatt, EntityStatAccessor


class DummyEntity(dict):
    def __init__(self, engine):
        self.engine = engine


class AvatarnessCache(object):
    """A cache for remembering when a node is an avatar of a character."""
    def __init__(self, db):
        self.db = db
        # character: graph: node: branch: tick: is_avatar
        self.db_order = StructuredDefaultDict(3, WindowDict)
        """Avatarness cached in the way it's stored in the database."""
        # graph: node: character: branch: tick: is_avatar
        self.user_order = StructuredDefaultDict(3, WindowDict)
        """Avatarness cached for iterating over the users of a node.

        That is, characters that have a particular node as an avatar.

        """
        for row in db.avatarness_dump():
            self.remember(*row)

    def remember(self, character, graph, node, branch, tick, is_avatar):
        self.db_order[character][graph][node][branch][tick] = is_avatar
        self.user_order[graph][node][character][branch][tick] = is_avatar


class AbstractEngine(object):
    @reify
    def json_dump_hints(self):
        return {}

    @reify
    def json_load_hints(self):
        return {}

    def __getattr__(self, att):
        if hasattr(self, 'method') and att in self.method:
            return partial(self.method[att], self)
        raise AttributeError

    @classmethod
    def listify(cls, obj):
        if isinstance(obj, list):
            return ["list"] + [cls.listify(v) for v in obj]
        elif isinstance(obj, tuple):
            return ["tuple"] + [cls.listify(v) for v in obj]
        elif isinstance(obj, dict):
            return ["dict"] + [
                [cls.listify(k), cls.listify(v)]
                for (k, v) in obj.items()
            ]
        elif isinstance(obj, cls.char_cls):
            return ['character', obj.name]
        elif isinstance(obj, cls.node_cls):
            return ['node', obj.character.name, obj.name]
        elif isinstance(obj, cls.portal_cls):
            return ['portal', obj.character.name, obj._origin, obj._destination]
        else:
            return obj

    def delistify(self, obj):
        if isinstance(obj, list) or isinstance(obj, tuple):
            if obj == [] or obj == ["list"]:
                return []
            elif obj == ["tuple"]:
                return tuple()
            elif obj == ['dict']:
                return {}
            elif obj[0] == 'list':
                return [self.delistify(p) for p in obj[1:]]
            elif obj[0] == 'tuple':
                return tuple(self.delistify(p) for p in obj[1:])
            elif obj[0] == 'dict':
                return {
                    self.delistify(k): self.delistify(v)
                    for (k, v) in obj[1:]
                }
            elif obj[0] == 'character':
                return self.character[self.delistify(obj[1])]
            elif obj[0] == 'node':
                return self.character[self.delistify(obj[1])].node[self.delistify(obj[2])]
            elif obj[0] == 'portal':
                return self.character[self.delistify(obj[1])].portal[self.delistify(obj[2])][self.delistify(obj[3])]
            else:
                raise ValueError("Unknown sequence type: {}".format(obj[0]))
        else:
            return obj

    @classmethod
    def get_encoder(cls):
        if not hasattr(cls, '_json_encoder'):
            class Encoder(JSONEncoder):
                def encode(self, o):
                    return super().encode(cls.listify(o))

                def default(self, o):
                    try:
                        from numpy import sctypes
                    except ImportError:
                        return super().default(o)
                    t = type(o)
                    if t in sctypes['int']:
                        return int(o)
                    elif t in sctypes['float']:
                        return float(o)
                    else:
                        return super().default(o)
            cls._json_encoder = Encoder
        return cls._json_encoder

    def json_dump(self, obj):
        return dumps(obj,  cls=self.get_encoder())
#        try:
#            if obj not in self.json_dump_hints:
#                self.json_dump_hints[obj] = dumps(obj, cls=self.get_encoder())
#            return self.json_dump_hints[obj]
#        except TypeError:
#            return dumps(obj, cls=self.get_encoder())

    def json_load(self, s):
        if s is None:
            return None
        return self.delistify(loads(s))
#        if s not in self.json_load_hints:
#            self.json_load_hints[s] = self.delistify(loads(s))
#        return self.json_load_hints[s]


class Engine(AbstractEngine, gORM):
    """LiSE, the Life Simulator Engine.

    Each instance of LiSE maintains a connection to a database
    representing the state of a simulated world. Simulation rules
    within this world are described by lists of Python functions, some
    of which make changes to the world.

    The top-level data structure within LiSE is the character. Most
    data within the world model is kept in some character or other;
    these will quite frequently represent people, but can be readily
    adapted to represent any kind of data that can be comfortably
    described as a graph or a JSON object. Every change to a character
    will be written to the database.

    LiSE tracks history as a series of ticks. In each tick, each
    simulation rule is evaluated once for each of the simulated
    entities it's been applied to. World changes in a given tick are
    remembered together, such that the whole world state can be
    rewound: simply set the properties ``branch`` and ``tick`` back to
    what they were just before the change you want to undo.

    Properties:

    - ``branch``: The fork of the timestream that we're on.
    - ``tick``: Units of time that have passed since the sim started.
    - ``time``: ``(branch, tick)``
    - ``character``: A mapping of :class:`Character` objects by name.
    - ``rule``: A mapping of all rules that have been made.
    - ``rulebook``: A mapping of lists of rules. They are followed in
      their order.  A whole rulebook full of rules may be assigned to
      an entity at once.
    - ``trigger``: A mapping of functions that might trigger a rule.
    - ``prereq``: A mapping of functions a rule might require to return 
      ``True`` for it to run.
    - ``action``: A mapping of functions that might manipulate the world
      state as a result of a rule running.
    - ``function``: A mapping of generic functions stored in the same
      database as the previous.
    - ``string``: A mapping of strings, probably shown to the user of the simulation at some point.
    - ``language``: Identifies the language used by
      ``string``. There's a different ``string`` mapping for each
      ``language``.
    - ``eternal``: Mapping of arbitrary serializable objects. It isn't sensitive to sim-time. A good place to keep game settings.
    - ``universal``: Another mapping of arbitrary serializable
      objects, but this one *is* sensitive to sim-time. Each tick, the
      state of the randomizer is saved here under the key
      ``'rando_state'``.
    - ``rando``: The randomizer used by all of the rules.

    """
    char_cls = Character
    node_cls = Node
    portal_cls = Portal

    @reify
    def _rulebooks_cache(self):
        r = defaultdict(list)
        for (rulebook, rule) in self.rule.db.rulebooks_rules():
            r[rulebook].append(rule)
        return r

    def _rulebook_set(self, rulebook, i, rule):
        self.rule.db.rulebook_set(rulebook, i, rule)
        cache = self._rulebooks_cache[rulebook]
        while len(cache) <= i:
            cache.append(None)
        cache[i] = rule

    def _rulebook_insert(self, rulebook, i, rule):
        self._rulebooks_cache[rulebook].insert(i, rule)
        self.db.rulebook_ins(rulebook, i, rule)

    def _rulebook_del_rule(self, rulebook, i):
        self.rule.db.rulebook_del(rulebook, i)
        del self._rulebooks_cache[rulebook][i]

    def _del_rulebook(self, rulebook):
        for (character, character_rulebooks) in \
             self._characters_rulebooks_cache.items():
            if rulebook not in character_rulebooks.values():
                continue
            for (which, rb) in character_rulebooks.items():
                if rb == rulebook:
                    raise ValueError(
                        "Rulebook still in use by {} as {}".format(
                            character, which
                        ))
        for (character, nodes) in self._nodes_rulebooks_cache.items():
            if rulebook not in nodes.values():
                continue
            for (node, rb) in nodes.items():
                if rb == rulebook:
                    raise ValueError(
                        "Rulebook still in use by node "
                        "{} in character {}".format(
                            node, character
                        ))
        for (character, origins) in self._portals_rulebooks_cache.items():
            for (origin, destinations) in origins.items():
                if rulebook not in destinations.values():
                    continue
                for (destination, rb) in destinations:
                    if rb == rulebook:
                        raise ValueError(
                            "Rulebook still in use by portal "
                            "{}->{} in character {}".format(
                                origin, destination, character
                            ))
        self.rule.db.rulebook_del_all(rulebook)
        del self._rulebooks_cache[rulebook]

    class crc_default_dict(defaultdict):
        def __missing__(self, k):
            self[k] = value = {
                'character': (k, 'character'),
                'avatar': (k, 'avatar'),
                'character_thing': (k, 'character_thing'),
                'character_place': (k, 'character_place'),
                'character_node': (k, 'character_node'),
                'character_portal': (k, 'character_portal')
            }
            return value

    @reify
    def _characters_rulebooks_cache(self):
        r = self.crc_default_dict()
        for (
                character,
                character_rulebook,
                avatar_rulebook,
                character_thing_rulebook,
                character_place_rulebook,
                character_node_rulebook,
                character_portal_rulebook
        ) in self.db.characters_rulebooks():
            r[character] = {
                'character': character_rulebook,
                'avatar': avatar_rulebook,
                'character_thing': character_thing_rulebook,
                'character_place': character_place_rulebook,
                'character_node': character_node_rulebook,
                'character_portal': character_portal_rulebook
            }
        return r

    def _set_character_rulebook(self, character, which, rulebook):
        if which not in (
                'character',
                'avatar',
                'character_thing',
                'character_place',
                'character_node',
                'character_portal'
        ):
            raise ValueError("Not a character rulebook: {}".format(which))
        self.db.upd_rulebook_char(which, rulebook, character)
        self._characters_rulebooks_cache[character][which] = rulebook

    @reify
    def _nodes_rulebooks_cache(self):
        r = defaultdict(dict)
        for (character, node, rulebook) in self.db.nodes_rulebooks():
            r[character][node] = rulebook
        return r

    def _set_node_rulebook(self, character, node, rulebook):
        self._nodes_rulebooks_cache[character][node] = rulebook
        self.engine.db.set_node_rulebook(character, node, rulebook)

    @reify
    def _portals_rulebooks_cache(self):
        r = StructuredDefaultDict(2, dict)
        for (character, nodeA, nodeB, rulebook) in self.db.portals_rulebooks():
            r[character][nodeA][nodeB] = rulebook
        return r

    def _set_portal_rulebook(self, character, nodeA, nodeB, rulebook):
        self._portals_rulebooks_cache[character][nodeA][nodeB] = rulebook
        self.db.set_portal_rulebook(character, nodeA, nodeB, rulebook)

    @reify
    def _avatarness_cache(self):
        return AvatarnessCache(self.db)

    def _remember_avatarness(self, character, graph, node, is_avatar=True, branch=None, tick=None):
        """Use this to record a change in avatarness.

        Should be called whenever a node that wasn't an avatar of a
        character now is, and whenever a node that was an avatar of a
        character now isn't.

        ``character`` is the one using the node as an avatar,
        ``graph`` is the character the node is in.

        """
        branch = branch or self.branch
        tick = tick or self.tick
        self._avatarness_cache.remember(
            character,
            graph,
            node,
            branch,
            tick,
            is_avatar
        )
        self.db.avatar_set(
            character,
            graph,
            node,
            branch,
            tick,
            is_avatar
        )

    @reify
    def _active_rules_cache(self):
        # rulebook: rule: branch: tick: active
        r = StructuredDefaultDict(3, WindowDict)
        for (rulebook, rule, branch, tick, active) in \
                self.db.dump_active_rules():
            r[rulebook][rule][branch][tick] = active
        return r

    def _set_rule_activeness(
            self, rulebook, rule, active, branch=None, tick=None
    ):
        branch = branch or self.branch
        tick = tick or self.tick
        self._active_rules_cache[rulebook][rule][branch][tick] = active
        # note the use of the world DB, not the code DB
        self.db.set_rule_activeness(rulebook, rule, branch, tick, active)

    @reify
    def _node_rules_handled_cache(self):
        # character: node: rulebook: rule: branch: ticks handled
        r = StructuredDefaultDict(5, set)
        for (character, node, rulebook, rule, branch, tick) \
                in self.db.dump_node_rules_handled():
            r[character][node][rulebook][rule][branch].add(tick)
        return r

    @reify
    def _portal_rules_handled_cache(self):
        # character: nodeA: nodeB: rulebook: rule: branch: ticks handled
        r = StructuredDefaultDict(6, set)
        for (character, nodeA, nodeB, idx, rulebook, rule, branch, tick) \
                in self.db.dump_portal_rules_handled():
            r[character][nodeA][nodeB][rulebook][rule][branch].add(tick)
        return r

    @reify
    def _character_rules_handled_cache(self):
        r = StructuredDefaultDict(4, set)
        for (character, rulebook, rule, branch, tick) in \
                self.db.handled_character_rules():
            r[character][rulebook][rule][branch].add(tick)
        return r

    @reify
    def _avatar_rules_handled_cache(self):
        r = StructuredDefaultDict(4, set)
        for (character, rulebook, rule, branch, tick) in \
                self.db.handled_avatar_rules():
            r[character][rulebook][rule][branch].add(tick)
        return r

    @reify
    def _character_thing_rules_handled_cache(self):
        r = StructuredDefaultDict(4, set)
        for (character, rulebook, rule, branch, tick) in \
                self.db.handled_character_thing_rules():
            r[character][rulebook][rule][branch].add(tick)
        return r

    @reify
    def _character_place_rules_handled_cache(self):
        r = StructuredDefaultDict(4, set)
        for (character, rulebook, rule, branch, tick) in \
                self.db.handled_character_place_rules():
            r[character][rulebook][rule][branch].add(tick)
        return r

    @reify
    def _character_node_rules_handled_cache(self):
        r = StructuredDefaultDict(4, set)
        for (character, rulebook, rule, branch, tick) in \
                self.db.handled_character_node_rules():
            r[character][rulebook][rule][branch].add(tick)
        return r

    @reify
    def _character_portal_rules_handled_cache(self):
        r = StructuredDefaultDict(4, set)
        for (character, rulebook, rule, branch, tick) in \
                self.db.handled_character_portal_rules():
            r[character][rulebook][rule][branch].add(tick)
        return r

    @reify
    def _things_cache(self):
        # character: thing: branch: tick: (location, next_location)
        r = StructuredDefaultDict(2, WindowDict)
        for (character, thing, branch, tick, loc, nextloc) in \
                self.db.things_dump():
            r[character][thing][branch][tick] = (loc, nextloc)
        return r

    @reify
    def _time_listeners(self):
        return []

    @reify
    def _next_tick_listeners(self):
        return []

    @reify
    def codedb(self):
        return connect(self._codedb)

    @reify
    def worlddb(self):
        if hasattr(self.db, 'alchemist'):
            return self.db.alchemist.conn.connection
        else:
            return self.db.connection

    def __init__(
            self,
            worlddb,
            codedb=None,
            connect_args={},
            alchemy=False,
            commit_modulus=None,
            random_seed=None,
            sql_rule_polling=False,
            logfun=None
    ):
        """Store the connections for the world database and the code database;
        set up listeners; and start a transaction

        """
        super().__init__(
            worlddb,
            query_engine_class=QueryEngine,
            connect_args=connect_args,
            alchemy=alchemy,
            json_dump=self.json_dump,
            json_load=self.json_load,
        )
        if logfun is None:
            from logging import getLogger
            logger = getLogger(__name__)

            def logfun(level, msg):
                getattr(logger, level)(msg)
        self.log = logfun
        self._sql_polling = sql_rule_polling
        self.commit_modulus = commit_modulus
        self.random_seed = random_seed
        if codedb:
            self._code_qe = QueryEngine(
                codedb, connect_args, alchemy, self.json_dump, self.json_load
            )
            self._code_qe.initdb()
        else:
            self._code_qe = self.db
        self._rules_iter = self._follow_rules()
        # set up the randomizer
        self.rando = Random()
        if 'rando_state' in self.universal:
            self.rando.setstate(self.universal['rando_state'])
        else:
            self.rando.seed(self.random_seed)
            self.universal['rando_state'] = self.rando.getstate()
        if '__init__' in self.method:
            self.method['__init__'](self)

    betavariate = getatt('rando.betavariate')
    choice = getatt('rando.choice')
    expovariate = getatt('rando.expovariate')
    gammavariate = getatt('rando.gammavariate')
    gauss = getatt('rando.gauss')
    getrandbits = getatt('rando.getrandbits')
    lognormvariate = getatt('rando.lognormvariate')
    normalvariate = getatt('rando.normalvariate')
    paretovariate = getatt('rando.paretovariate')
    randint = getatt('rando.randint')
    random = getatt('rando.random')
    randrange = getatt('rando.randrange')
    sample = getatt('rando.sample')
    shuffle = getatt('rando.shuffle')
    triangular = getatt('rando.triangular')
    uniform = getatt('rando.uniform')
    vonmisesvariate = getatt('rando.vonmisesvariate')
    weibullvariate = getatt('rando.weibullvariate')

    @reify
    def action(self):
        return FunctionStore(self, self._code_qe, 'actions')

    @reify
    def prereq(self):
        return FunctionStore(self, self._code_qe, 'prereqs')

    @reify
    def trigger(self):
        return FunctionStore(self, self._code_qe, 'triggers')

    @reify
    def function(self):
        return FunctionStore(self, self._code_qe, 'functions')

    @reify
    def method(self):
        return FunctionStore(self, self._code_qe, 'methods')

    @property
    def stores(self):
        return (
            self.action,
            self.prereq,
            self.trigger,
            self.function,
            self.string
        )

    @reify
    def rule(self):
        return AllRules(self, self._code_qe)

    @reify
    def rulebook(self):
        return AllRuleBooks(self, self._code_qe)

    @reify
    def string(self):
        return StringStore(self._code_qe)

    @reify
    def universal(self):
        return GlobalVarMapping(self)

    @reify
    def character(self):
        return CharacterMapping(self)

    def debug(self, msg):
        """Log a message at level 'debug'"""
        self.log('debug', msg)

    def info(self, msg):
        """Log a message at level 'info'"""
        self.log('info', msg)

    def warning(self, msg):
        """Log a message at level 'warning'"""
        self.log('warning', msg)

    def error(self, msg):
        """Log a message at level 'error'"""
        self.log('error', msg)

    def critical(self, msg):
        """Log a message at level 'critical'"""
        self.log('critical', msg)

    def coinflip(self):
        """Return True or False with equal probability."""
        return self.choice((True, False))

    def roll_die(self, d):
        """Roll a die with ``d`` faces. Return the result."""
        return self.randint(1, d)

    def dice(self, n, d):
        """Roll ``n`` dice with ``d`` faces, and yield the results.

        This is an iterator. You'll get the result of each die in
        successon.

        """
        for i in range(0, n):
            yield self.roll_die(d)

    def dice_check(self, n, d, target, comparator=lambda x, y: x <= y):
        """Roll ``n`` dice with ``d`` sides, sum them, and return whether they
        are <= ``target``.

        If ``comparator`` is provided, use it instead of <=.

        """
        return comparator(sum(self.dice(n, d)), target)

    def percent_chance(self, pct):
        """Given a ``pct``% chance of something happening right now, decide at
        random whether it actually happens, and return ``True`` or
        ``False`` as appropriate.

        Values not between 0 and 100 are treated as though they
        were 0 or 100, whichever is nearer.

        """
        if pct <= 0:
            return False
        if pct >= 100:
            return True
        return pct / 100 < self.random()

    def commit(self):
        """Commit to both the world and code databases, and begin a new
        transaction for the world database

        """
        for store in self.stores:
            store.commit()
        super().commit()

    def close(self):
        """Commit changes and close the database."""
        self.commit()
        super().close()

    def __enter__(self):
        """Return myself. For compatibility with ``with`` semantics."""
        return self

    def __exit__(self, *args):
        """Close on exit."""
        self.close()

    def time_listener(self, v):
        """Call a function whenever my ``branch`` or ``tick`` changes.

        The function will be called with the old branch and tick
        followed by the new branch and tick.

        """
        if not callable(v):
            raise TypeError("Need a function")
        if v not in self._time_listeners:
            self._time_listeners.append(v)
        return v

    def time_unlisten(self, v):
        """Don't call this function when the time changes."""
        if v in self._time_listeners:
            self._time_listeners.remove(v)
        return v

    def next_tick_listener(self, v):
        """Call a function when time passes.

        It will only be called when the time changes as a result of
        all the rules being processed for a tick. This is what it
        means for time to 'pass', rather than for you to change the
        time yourself.

        """
        if not callable(v):
            raise TypeError("Need a function")
        if v not in self._next_tick_listeners:
            self._next_tick_listeners.append(v)
        return v

    def next_tick_unlisten(self, v):
        """Don't call this function when time passes."""
        if v in self._next_tick_listeners:
            self._next_tick_listeners.remove(v)
        return v

    @property
    def branch(self):
        if self._obranch is not None:
            return self._obranch
        return self.db.globl['branch']

    @branch.setter
    def branch(self, v):
        """Set my gorm's branch and call listeners"""
        (b, t) = self.time
        if v == b:
            return
        if v not in self._branches:
            parent = b
            child = v
            assert(parent in self._branches)
            self._branch_parents[child] = parent
            self._branches[parent][child] = {}
            self._branches[child] = self._branches[parent][child]
            self._branches_start[child] = t
        self._obranch = v
        self.db.globl['branch'] = v
        if not hasattr(self, 'locktime'):
            for time_listener in self._time_listeners:
                time_listener(b, t, v, t)

    @reify
    def _branch_parents(self):
        return {}

    @reify
    def _branches(self):
        return defaultdict(dict)

    @reify
    def _branches_start(self):
        return {}

    @property
    def tick(self):
        return self.rev

    @tick.setter
    def tick(self, v):
        """Update gorm's ``rev``, and call listeners"""
        if not isinstance(v, int):
            raise TypeError("tick must be integer")
        (branch_then, tick_then) = self.time
        if v == self.tick:
            return
        self._orev = v
        self.rev = v
        if not hasattr(self, 'locktime'):
            for time_listener in self._time_listeners:
                time_listener(branch_then, tick_then, branch_then, v)

    @property
    def time(self):
        """Return tuple of branch and tick"""
        return (self.branch, self.tick)

    @time.setter
    def time(self, v):
        """Set my ``branch`` and ``tick``, and call listeners"""
        (branch_then, tick_then) = self.time
        (branch_now, tick_now) = v
        relock = hasattr(self, 'locktime')
        self.locktime = True
        # setting tick and branch in this order makes it practical to
        # track the timestream genealogy
        self.tick = tick_now
        self.branch = branch_now
        if not relock:
            del self.locktime
        if not hasattr(self, 'locktime'):
            for time_listener in self._time_listeners:
                time_listener(
                    branch_then, tick_then, branch_now, tick_now
                )

    def _rule_active(self, rulebook, rule):
        cache = self._active_rules_cache[rulebook][rule]
        for (branch, tick) in self._active_branches(*self.time):
            if branch in cache:
                return cache[branch][tick]
        return False

    def _poll_char_rules(self):
        if self._sql_polling:
            yield from self.db.poll_char_rules(*self.time)
            return

        def handled(rulebook, rule):
            cache = self._character_rules_handled_cache
            return (
                rulebook in cache and
                rule in cache[rulebook] and
                self.branch in cache[rulebook][rule] and
                self.tick in cache[rulebook][rule][self.branch]
            )

        for char in self.character:
            for (
                    rulemap,
                    rulebook
            ) in self._characters_rulebooks_cache[char].items():
                for rule in self._rulebooks_cache[rulebook]:
                    if (
                        self._rule_active(rulebook, rule) and not
                        handled(rulebook, rule)
                    ):
                        yield (rulemap, char, rulebook, rule)

    def _poll_node_rules(self):
        if self._sql_polling:
            yield from self.db.poll_node_rules(*self.time)
            return
        cache = self._node_rules_handled_cache

        def handled(char, node, rulebook, rule):
            return (
                char in cache and
                node in cache[char] and
                rulebook in cache[char][node] and
                rule in cache[char][rule][rulebook] and
                self.branch in cache[char][rule][rulebook] and
                self.tick in cache[char][rule][rulebook][self.branch]
            )

        for chara in self.character.values():
            char = chara.name
            for node in chara.node:
                if (
                        char in self._nodes_rulebooks_cache and
                        node in self._nodes_rulebooks_cache[char]
                ):
                    rulebook = self._nodes_rulebooks_cache[char][node]
                else:
                    rulebook = (char, node)
                for rule in self._rulebooks_cache[rulebook]:
                    if (
                        self._rule_active(rulebook, rule) and not
                        handled(char, node, rulebook, rule)
                    ):
                        yield (char, node, rulebook, rule)

    def _poll_portal_rules(self):
        if self._sql_polling:
            yield from self.db.poll_portal_rules(*self.time)
            return

        def handled(char, nodeA, nodeB, rulebook, rule):
            cache = self._portal_rules_handled_cache
            return (
                char in cache and
                nodeA in cache[char] and
                nodeB in cache[char][nodeA] and
                rulebook in cache[char][nodeA][nodeB] and
                rule in cache[char][nodeA][nodeB][rulebook] and
                self.branch in cache[char][nodeA][nodeB][rulebook][rule] and
                self.tick in cache[char][nodeA][nodeB][rulebook][rule][
                    self.branch
                ]
            )

        cache = self._portals_rulebooks_cache
        for chara in self.character.values():
            for nodeA in chara.portal:
                for nodeB in chara.portal[nodeA]:
                    if (
                            chara.name in cache and
                            nodeA in cache[chara.name] and
                            nodeB in cache[chara.name][nodeA]
                    ):
                        rulebook = cache[chara.name][nodeA][nodeB]
                    else:
                        rulebook = (chara.name, nodeA, nodeB)
                    for rule in self._rulebooks_cache[rulebook]:
                        if (
                            self._rule_active(rulebook, rule) and not
                            handled(chara, nodeA, nodeB, rulebook, rule)
                        ):
                            yield (
                                chara,
                                nodeA,
                                nodeB,
                                rulebook,
                                rule
                            )

    def _poll_rules(self):
        """Iterate over tuples containing rules yet unresolved in the current tick.

        The tuples are of the form: ``(ruletype, character, entity,
        rulebook, rule)`` where ``ruletype`` is what kind of entity
        the rule is about (character', 'thing', 'place', or
        'portal'), and ``entity`` is the :class:`Place`,
        :class:`Thing`, or :class:`Portal` that the rule is attached
        to. For character-wide rules it is ``None``.

        """
        for (
                rulemap, character, rulebook, rule
        ) in self._poll_char_rules():
            try:
                yield (
                    rulemap,
                    self.character[character],
                    None,
                    rulebook,
                    self.rule[rule]
                )
            except KeyError:
                continue
        for (
                character, node, rulebook, rule
        ) in self._poll_node_rules():
            try:
                c = self.character[character]
                n = c.node[node]
            except KeyError:
                continue
            typ = 'thing' if hasattr(n, 'location') else 'place'
            yield typ, c, n, rulebook, self.rule[rule]
        for (
                character, a, b, i, rulebook, rule
        ) in self._poll_portal_rules():
            try:
                c = self.character[character]
                yield 'portal', c.portal[a][b], rulebook, self.rule[rule]
            except KeyError:
                continue

    def _handled_thing_rule(self, char, thing, rulebook, rule, branch, tick):
        self._node_rules_handled_cache[
            char][thing][rulebook][rule][branch].add(tick)
        self.db.handled_thing_rule(
            char, thing, rulebook, rule, branch, tick
        )

    def _handled_place_rule(self, char, place, rulebook, rule, branch, tick):
        self._node_rules_handled_cache[
            char][place][rulebook][rule][branch].add(tick)
        self.db.handled_place_rule(
            char, place, rulebook, rule, branch, tick
        )

    def _handled_portal_rule(
            self, char, nodeA, nodeB, rulebook, rule, branch, tick
    ):
        self._portal_rules_handled_cache[
            char][nodeA][nodeB][rulebook][rule][branch].add(tick)
        self.db.handled_portal_rule(
            char, nodeA, nodeB, rulebook, rule, branch, tick
        )

    def _handled_character_rule(
            self, typ, char, rulebook, rule, branch, tick
    ):
        {
            'character': self._character_rules_handled_cache,
            'avatar': self._avatar_rules_handled_cache,
            'character_thing': self._character_thing_rules_handled_cache,
            'character_place': self._character_place_rules_handled_cache,
            'character_node': self._character_node_rules_handled_cache,
            'character_portal': self._character_portal_rules_handled_cache,
        }[typ][char][rulebook][rule][branch].add(tick)
        self.db.handled_character_rule(
            typ, char, rulebook, rule, branch, tick
        )

    def _follow_rules(self):
        """For each rule in play at the present tick, call it and yield a
        tuple describing the results.

        Tuples are of the form: ``(returned, rulename, ruletype,
        rulebook)`` where ``returned`` is whatever the rule itself
        returned upon being called, and ``ruletype`` is what sort of
        entity the rule applies to.

        """
        (branch, tick) = self.time
        for (typ, character, entity, rulebook, rule) in self._poll_rules():
            def follow(*args):
                return (rule(self, *args), rule.name, typ, rulebook)

            if typ in ('thing', 'place', 'portal'):
                yield follow(character, entity)
                if typ == 'thing':
                    self._handled_thing_rule(
                        character.name,
                        entity.name,
                        rulebook,
                        rule.name,
                        branch,
                        tick
                    )
                elif typ == 'place':
                    self._handled_place_rule(
                        character.name,
                        entity.name,
                        rulebook,
                        rule.name,
                        branch,
                        tick
                    )
                else:
                    self._handled_portal_rule(
                        character.name,
                        entity.origin.name,
                        entity.destination.name,
                        rulebook,
                        rule.name,
                        branch,
                        tick
                    )
            else:
                if typ == 'character':
                    yield follow(character)
                elif typ == 'avatar':
                    for avatar in character.avatars():
                        yield follow(character, avatar)
                elif typ == 'character_thing':
                    for thing in character.thing.values():
                        yield follow(character, thing)
                elif typ == 'character_place':
                    for place in character.place.values():
                        yield follow(character, place)
                elif typ == 'character_node':
                    for node in character.node.values():
                        yield follow(character, node)
                elif typ == 'character_portal':
                    for portal in character.portal.values():
                        yield follow(character, portal)
                else:
                    raise ValueError('Unknown type of rule')
                self._handled_character_rule(
                    typ, character.name, rulebook, rule.name, branch, tick
                )

    def advance(self):
        """Follow the next rule if available, or advance to the next tick."""
        try:
            r = next(self._rules_iter)
        except StopIteration:
            self.tick += 1
            self._rules_iter = self._follow_rules()
            self.universal['rando_state'] = self.rando.getstate()
            if self.commit_modulus and self.tick % self.commit_modulus == 0:
                self.commit()
            r = None
        return r

    def next_tick(self):
        """Make time move forward in the simulation.

        Calls ``advance`` repeatedly, appending its results to a list until
        the tick has ended.  Return the list.

        """
        curtick = self.tick
        r = []
        while self.tick == curtick:
            result = self.advance()
            self.debug("[next_tick]: {}".format(result))
            r.append(result)
        # The last element is always None, but is not a sentinel; any
        # rule may return None.
        for listener in self._next_tick_listeners:
            listener(self.branch, self.tick, r)
        return r[:-1]

    def new_character(self, name, **kwargs):
        """Create and return a new :class:`Character`."""
        self.add_character(name, **kwargs)
        return self.character[name]

    def add_character(self, name, data=None, **kwargs):
        """Create a new character.

        You'll be able to access it as a :class:`Character` object by
        looking up ``name`` in my ``character`` property.

        ``data``, if provided, should be a networkx-compatible graph
        object. Your new character will be a copy of it.

        Any keyword arguments will be set as stats of the new character.

        """
        self.new_digraph(name, data, **kwargs)
        ch = Character(self, name)
        if data is not None:
            for a in data.adj:
                for b in data.adj[a]:
                    assert(
                        a in ch.adj and
                        b in ch.adj[a]
                    )
        if hasattr(self.character, '_cache'):
            self.character._cache[name] = ch

    def del_character(self, name):
        """Remove the Character from the database entirely.

        This also deletes all its history. You'd better be sure.

        """
        self.db.del_character(name)
        self.del_graph(name)
        del self.character[name]

    def _is_thing(self, character, node):
        if character not in self._things_cache or \
           node not in self._things_cache[character]:
            return False
        cache = self._things_cache[character][node]
        for (branch, tick) in self._active_branches():
            if branch in cache:
                return cache[branch][tick]
        return False

    def _set_thing_loc_and_next(
            self, character, node, loc, nextloc=None, branch=None, tick=None
    ):
        branch = branch or self.branch
        tick = tick or self.tick
        self.db.thing_loc_and_next_set(
            character,
            node,
            branch,
            tick,
            loc,
            nextloc
        )
        self._things_cache[character][node][branch][tick] = (loc, nextloc)

    def _node_exists(self, character, node):
        if character not in self._nodes_cache or \
           node not in self._nodes_cache[character]:
            return False
        cache = self._nodes_cache[character][node]
        for (branch, tick) in self._active_branches():
            if branch in cache:
                return cache[branch][tick]
        return False

    def _exist_node(self, character, node, exist=True, branch=None, tick=None):
        branch = branch or self.branch
        tick = tick or self.tick
        self.db.exist_node(
            character,
            node,
            branch,
            tick,
            exist
        )
        self._nodes_cache[character][node][branch][tick] = exist

    def _exist_edge(self, character, nodeA, nodeB, exist=True, branch=None, tick=None):
        branch = branch or self.branch
        tick = tick or self.tick
        self.db.exist_edge(
            character,
            nodeA,
            nodeB,
            0,
            branch,
            tick,
            exist
        )
        self._edges_cache[character][nodeA][nodeB][0][branch][tick] = exist

    def alias(self, v, stat='dummy'):
        r = DummyEntity(self)
        r[stat] = v
        return EntityStatAccessor(r, stat, engine=self)

    def entityfy(self, v, stat='dummy'):
        if (
                isinstance(v, Node) or
                isinstance(v, Portal) or
                isinstance(v, Query) or
                isinstance(v, EntityStatAccessor)
        ):
            return v
        return self.alias(v, stat)

    def ticks_when(self, query):
        return query()
