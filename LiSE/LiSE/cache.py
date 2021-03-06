from allegedb.cache import (
    Cache,
    PickyDefaultDict,
    StructuredDefaultDict,
    TurnDict,
    HistoryError
)
from .util import singleton_get


class EntitylessCache(Cache):
    def store(self, key, branch, turn, tick, value, *, planning=False):
        super().store(None, key, branch, turn, tick, value, planning=planning)

    def load(self, data, validate=False):
        return super().load(((None,) + row for row in data), validate)

    def retrieve(self, key, branch, turn, tick):
        return super().retrieve(None, key, branch, turn, tick)

    def iter_entities_or_keys(self, branch, turn, tick, *, forward=False):
        return super().iter_entities_or_keys(None, branch, turn, tick, forward=forward)
    iter_entities = iter_keys = iter_entities_or_keys

    def contains_entity_or_key(self, ke, branch, turn, tick, *, forward=False):
        return super().contains_entity_or_key(None, ke, branch, turn, tick, forward=forward)
    contains_entity = contains_key = contains_entity_or_key

    def retrieve(self, *args):
        return super().retrieve(*(None,)+args)


class AvatarnessCache(Cache):
    """A cache for remembering when a node is an avatar of a character."""
    def __init__(self, engine):
        Cache.__init__(self, engine)
        self.user_order = StructuredDefaultDict(3, TurnDict)
        self.user_shallow = PickyDefaultDict(TurnDict)
        self.graphs = StructuredDefaultDict(1, TurnDict)
        self.graphavs = StructuredDefaultDict(1, TurnDict)
        self.charavs = StructuredDefaultDict(1, TurnDict)
        self.soloav = StructuredDefaultDict(1, TurnDict)
        self.uniqav = StructuredDefaultDict(1, TurnDict)
        self.uniqgraph = StructuredDefaultDict(1, TurnDict)
        self.users = StructuredDefaultDict(1, TurnDict)

    def store(self, character, graph, node, branch, turn, tick, is_avatar, *, planning=False):
        if not is_avatar:
            is_avatar = None
        Cache.store(self, character, graph, node, branch, turn, tick, is_avatar, planning=False)
        userturns = self.user_order[graph][node][character][branch]
        if userturns.has_exact_rev(turn):
            userturns[turn][tick] = is_avatar
        else:
            userturns[turn] = {tick: is_avatar}
        usershal = self.user_shallow[(graph, node, character, branch)]
        if usershal.has_exact_rev(turn):
            usershal[turn][tick] = is_avatar
        else:
            usershal[turn] = {tick: is_avatar}
        charavs = self.charavs[character][branch]
        graphavs = self.graphavs[(character, graph)][branch]
        graphs = self.graphs[character][branch]
        uniqgraph = self.uniqgraph[character][branch]
        soloav = self.soloav[(character, graph)][branch]
        uniqav = self.uniqav[character][branch]
        users = self.users[graph, node][branch]

        def add_something(cache, what):
            if cache.has_exact_rev(turn):
                nucache = cache[turn][tick].copy()
                nucache.add(what)
                cache[turn][tick] = nucache
            elif turn in cache:
                cacheturn = cache[turn]
                nucache = cacheturn[cacheturn.end].copy()
                nucache.add(what)
                cache[turn] = {tick: nucache}
            else:
                cache[turn] = {tick: {what}}

        def remove_something(cache, what):
            if cache.has_exact_rev(turn):
                nucache = cache[turn][tick].copy()
                nucache.remove(what)
                cache[turn][tick] = nucache
            elif turn in cache:
                cacheturn = cache[turn]
                nucache = cacheturn[cacheturn.end].copy()
                nucache.remove(what)
                cache[turn] = {tick: nucache}
            else:
                raise ValueError
        if is_avatar:
            add_something(graphavs, node)
            add_something(charavs, (graph, node))
            add_something(graphs, graph)
            add_something(users, character)
        else:
            remove_something(graphavs, node)
            remove_something(charavs, (graph, node))
            if not graphavs[turn][tick]:
                remove_something(graphs, graph)
            if not charavs[turn][tick]:
                remove_something(users, character)
        graphav = singleton_get(graphavs[turn][tick])
        if soloav.has_exact_rev(turn):
            soloav[turn][tick] = graphav
        else:
            soloav[turn] = {tick: graphav}
        charav = singleton_get(charavs[turn][tick])
        if uniqav.has_exact_rev(turn):
            uniqav[turn][tick] = charav
        else:
            uniqav[turn] = {tick: charav}
        if not graphavs[turn][tick]:
            graphs[turn][tick].remove(graph)
            if len(graphs[turn][tick]) == 1:
                uniqgraph[turn][tick] = next(iter(graphs[turn][tick]))
            else:
                uniqgraph[turn][tick] = None
        if turn in graphavs and tick in graphavs[turn] and graphavs[turn][tick]:
            if soloav.has_exact_rev(turn):
                soloav[turn][tick] = None
            else:
                soloav[turn] = soloav.cls({tick: None})
        else:
            if soloav.has_exact_rev(turn):
                soloav[turn][tick] = node
            else:
                soloav[turn] = soloav.cls({tick: None})
        if turn in charavs and tick in charavs[turn] and charavs[turn][tick]:
            if uniqav.has_exact_rev(turn):
                uniqav[turn][tick] = None
            else:
                uniqav[turn] = uniqav.cls({tick: None})
        elif uniqav.has_exact_rev(turn):
            uniqav[turn][tick] = (graph, node)
        else:
            uniqav[turn] = uniqav.cls({tick: (graph, node)})
        if turn in graphs and tick in graphs[turn] and graphs[turn][tick]:
            if uniqgraph.has_exact_rev(turn):
                uniqgraph[turn][tick] = None
            else:
                uniqgraph[turn] = uniqgraph.cls({tick: None})
        elif uniqgraph.has_exact_rev(turn):
            uniqgraph[turn][tick] = graph
        else:
            uniqgraph[turn] = uniqgraph.cls({tick: graph})

    def get_char_graph_avs(self, char, graph, branch, turn, tick):
        return self._valcache_lookup(
            self.graphavs[(char, graph)], branch, turn, tick
        ) or set()

    def get_char_graph_solo_av(self, char, graph, branch, turn, tick):
        return self._valcache_lookup(
            self.soloav[(char, graph)], branch, turn, tick
        )

    def get_char_only_av(self, char, branch, turn, tick):
        return self._valcache_lookup(
            self.uniqav[char], branch, turn, tick
        )

    def get_char_only_graph(self, char, branch, turn, tick):
        return self._valcache_lookup(
            self.uniqgraph[char], branch, turn, tick
        )

    def get_char_graphs(self, char, branch, turn, tick):
        return self._valcache_lookup(
            self.graphs[char], branch, turn, tick
        ) or set()

    def _slow_iter_character_avatars(self, character, branch, turn, tick):
        for graph in self.iter_entities(character, branch, turn, tick):
            for node in self.iter_entities(character, graph, branch, turn, tick):
                yield graph, node

    def _slow_iter_users(self, graph, node, branch, turn, tick):
        if graph not in self.user_order:
            return
        for character in self.user_order[graph][node]:
            if (graph, node, character, branch) not in self.user_shallow:
                for (b, t, tc) in self.db._iter_parent_btt(branch, turn, tick):
                    if b in self.user_order[graph][node][character]:
                        isav = self.user_order[graph][node][character][b][t]
                        self.store(character, graph, node, branch, turn, tick, isav[tc])
                        break
                else:
                    self.store(character, graph, node, branch, turn, tick, None)
            try:
                if self.user_shallow[(graph, node, character, branch)][turn][tick]:
                   yield character
            except HistoryError:
                continue


class RulesHandledCache(object):
    def __init__(self, engine):
        self.engine = engine
        self.handled = {}
        self.unhandled = {}

    def get_rulebook(self, *args):
        raise NotImplementedError

    def iter_unhandled_rules(self, branch, turn, tick):
        raise NotImplementedError

    def store(self, *args, loading=False):
        entity = args[:-5]
        rulebook, rule, branch, turn, tick = args[-5:]
        shalo = self.handled.setdefault(entity + (rulebook, branch, turn), set())
        unhandl = self.unhandled.setdefault(entity, {}).setdefault(rulebook, {}).setdefault(branch, {})
        if turn not in unhandl:
            unhandl[turn] = list(self.unhandled_rulebook_rules(entity, rulebook, branch, turn, tick))
        try:
            unhandl[turn].remove(rule)
        except ValueError:
            if not loading:
                raise
        shalo.add(rule)

    def fork(self, branch, turn, tick):
        parent_branch, parent_turn, parent_tick, end_turn, end_tick = self.engine._branches[branch]
        unhandl = self.unhandled
        handl = self.handled
        rbcache = self.engine._rulebooks_cache
        unhandrr = self.unhandled_rulebook_rules
        getrb = self.get_rulebook
        for entity in unhandl:
            rulebook = getrb(*entity + (branch, turn, tick))
            if entity + (rulebook, branch, turn) in handl:
                raise HistoryError(
                    "Tried to fork history in a RulesHandledCache, "
                    "but it seems like rules have already been run where we're "
                    "forking to"
                )
            rules = set(rbcache.retrieve(rulebook, branch, turn, tick))
            unhandled_rules = unhandrr(entity, rulebook, parent_branch, parent_turn, tick)
            unhandled_rules_set = set(unhandled_rules)
            handl[entity + (rulebook, branch, turn)] = rules.difference(unhandled_rules_set)
            unhandl[entity][rulebook][branch][turn] = unhandled_rules

    def retrieve(self, *args):
        return self.handled[args]

    def unhandled_rulebook_rules(self, *args):
        entity = args[:-4]
        rulebook, branch, turn, tick = args[-4:]
        if (
            entity in self.unhandled and
            rulebook in self.unhandled[entity] and
            branch in self.unhandled[entity][rulebook] and
            turn in self.unhandled[entity][rulebook][branch]
        ):
            ret = self.unhandled[entity][rulebook][branch][turn]
        else:
            try:
                return [
                    rule for rule in
                    self.engine._rulebooks_cache.retrieve(rulebook, branch, turn, tick)
                    if rule not in self.handled.setdefault(entity + (rulebook, branch, turn), set())
                ]
            except KeyError:
                return []
        return ret


class CharacterRulesHandledCache(RulesHandledCache):
    def get_rulebook(self, character, branch, turn, tick):
        try:
            return self.engine._characters_rulebooks_cache.retrieve(character, branch, turn, tick)
        except KeyError:
            return character, 'character'

    def iter_unhandled_rules(self, branch, turn, tick):
        for character in self.engine.character:
            rb = self.get_rulebook(character, branch, turn, tick)
            for rule in self.unhandled_rulebook_rules(
                character, rb, branch, turn, tick
            ):
                yield character, rb, rule


class AvatarRulesHandledCache(RulesHandledCache):
    def get_rulebook(self, character, branch, turn, tick):
        try:
            return self.engine._avatars_rulebooks_cache.retrieve(character, branch, turn, tick)
        except KeyError:
            return character, 'avatar'

    def iter_unhandled_rules(self, branch, turn, tick):
        for character, char in self.engine.character.items():
            rulebook = self.get_rulebook(character, branch, turn, tick)
            for graph, avs in char.avatar.items():
                for avatar in avs:
                    try:
                        rules = self.unhandled_rulebook_rules((graph, avatar), rulebook, branch, turn, tick)
                    except KeyError:
                        continue
                    for rule in rules:
                        yield character, rulebook, graph, avatar, rule


class CharacterThingRulesHandledCache(RulesHandledCache):
    def get_rulebook(self, character, branch, turn, tick):
        try:
            return self.engine._characters_things_rulebooks_cache.retrieve(character, branch, turn, tick)
        except KeyError:
            return character, 'character_thing'

    def iter_unhandled_rules(self, branch, turn, tick):
        for character, char in self.engine.character.items():
            rulebook = self.get_rulebook(character, branch, turn, tick)
            for thing in char.thing:
                try:
                    rules = self.unhandled_rulebook_rules((character, thing), rulebook, branch, turn, tick)
                except KeyError:
                    continue
                for rule in rules:
                    yield character, thing, rulebook, rule


class CharacterPlaceRulesHandledCache(RulesHandledCache):
    def get_rulebook(self, character, branch, turn, tick):
        try:
            return self.engine._characters_places_rulebooks_cache.retrieve(character, branch, turn, tick)
        except KeyError:
            return character, 'character_place'

    def iter_unhandled_rules(self, branch, turn, tick):
        for character, char in self.engine.character.items():
            rulebook = self.get_rulebook(character, branch, turn, tick)
            for place in char.place:
                try:
                    rules = self.unhandled_rulebook_rules((character, place), rulebook, branch, turn, tick)
                except KeyError:
                    continue
                for rule in rules:
                    yield character, place, rulebook, rule


class CharacterPortalRulesHandledCache(RulesHandledCache):
    def get_rulebook(self, character, branch, turn, tick):
        try:
            return self.engine._characters_portals_rulebooks_cache.retrieve(character, branch, turn, tick)
        except KeyError:
            return character, 'character_portal'

    def iter_unhandled_rules(self, branch, turn, tick):
        for character, char in self.engine.character.items():
            try:
                rulebook = self.get_rulebook(character, branch, turn, tick)
            except KeyError:
                continue
            for orig in char.portal:
                for dest in char.portal[orig]:
                    try:
                        rules = self.unhandled_rulebook_rules((character, orig, dest), rulebook, branch, turn, tick)
                    except KeyError:
                        continue
                    for rule in rules:
                        yield character, orig, dest, rulebook, rule


class NodeRulesHandledCache(RulesHandledCache):
    def get_rulebook(self, character, node, branch, turn, tick):
        try:
            return self.engine._nodes_rulebooks_cache.retrieve(character, node, branch, turn, tick)
        except KeyError:
            return character, node

    def iter_unhandled_rules(self, branch, turn, tick):
        for character, char in self.engine.character.items():
            for node in char.node:
                try:
                    rulebook = self.get_rulebook(character, node, branch, turn, tick)
                    rules = self.unhandled_rulebook_rules((character, node), rulebook, branch, turn, tick)
                except KeyError:
                    continue
                for rule in rules:
                    yield character, node, rulebook, rule


class PortalRulesHandledCache(RulesHandledCache):
    def get_rulebook(self, character, orig, dest, branch, turn, tick):
        try:
            return self.engine._portals_rulebooks_cache.retrieve(character, orig, dest, branch, turn, tick)
        except KeyError:
            return character, orig, dest

    def iter_unhandled_rules(self, branch, turn, tick):
        for character, char in self.engine.character.items():
            for orig, dests in char.portal.items():
                for dest in dests:
                    try:
                        rulebook = self.get_rulebook(character, orig, dest, branch, turn, tick)
                        rules = self.unhandled_rulebook_rules((character, orig, dest), rulebook, branch, turn, tick)
                    except KeyError:
                        continue
                    for rule in rules:
                        yield character, orig, dest, rulebook, rule


class ThingsCache(Cache):
    def __init__(self, db):
        Cache.__init__(self, db)
        self._make_node = db.thing_cls

    def turn_before(self, character, thing, branch, turn):
        try:
            self.retrieve(character, thing, branch, turn, 0)
        except KeyError:
            pass
        return self.keys[(character,)][thing][branch].rev_before(turn)

    def turn_after(self, character, thing, branch, turn):
        try:
            self.retrieve(character, thing, branch, turn, 0)
        except KeyError:
            pass
        return self.keys[(character,)][thing][branch].rev_after(turn)
