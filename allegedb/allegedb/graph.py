# This file is part of allegedb, an object relational mapper for versioned graphs.
# Copyright (C) Zachary Spector.
import networkx
from networkx.exception import NetworkXError
from blinker import Signal
from collections import MutableMapping, defaultdict
from operator import attrgetter
from .xjson import (
    JSONWrapper,
    JSONListWrapper,
    JSONReWrapper,
    JSONListReWrapper
)


def getatt(attribute_name):
    """An easy way to make an alias"""
    return property(attrgetter(attribute_name))


def convert_to_networkx_graph(data, create_using=None, multigraph_input=False):
    """Convert an AllegedGraph to the corresponding NetworkX graph type."""
    if isinstance(data, AllegedGraph):
        result = networkx.convert.from_dict_of_dicts(
            data.adj,
            create_using=create_using,
            multigraph_input=data.is_multigraph()
        )
        result.graph = dict(data.graph)
        result.node = {k: dict(v) for k, v in data.node.items()}
        return result
    return networkx.convert.to_networkx_graph(
        data, create_using, multigraph_input
    )


class NeatMapping(MutableMapping):
    """Common amenities for mappings"""
    def clear(self):
        """Delete everything"""
        for k in list(self.keys()):
            del self[k]

    def __repr__(self):
        return "{}(graph={}, data={})".format(
            self.__class__.__name__, self.graph.name, repr(dict(self))
        )

    def update(self, other):
        """Version of ``update`` that doesn't clobber the database so much"""
        if hasattr(other, 'items'):
            other = other.items()
        for (k, v) in other:
            if (
                    k not in self or
                    self[k] != v
            ):
                self[k] = v


class AbstractEntityMapping(NeatMapping, Signal):
    def _get_cache(self, key, branch, turn, tick):
        raise NotImplementedError

    def _cache_contains(self, key, branch, turn, tick):
        raise NotImplementedError

    def _set_db(self, key, branch, turn, tick, value):
        """Set a value for a key in the database (not the cache)."""
        raise NotImplementedError

    def _set_cache(self, key, branch, turn, tick, value):
        raise NotImplementedError

    def _del_db(self, key, branch, turn, tick):
        """Delete a key from the database (not the cache)."""
        self._set_db(key, branch, turn, tick, None)

    def _del_cache(self, key, branch, turn, tick):
        self._set_cache(key, branch, turn, tick, None)

    def __getitem__(self, key):
        """If key is 'graph', return myself as a dict, else get the present
        value of the key and return that

        """
        def wrapval(v):
            if isinstance(v, list):
                return JSONListReWrapper(self, key, v)
            elif isinstance(v, dict):
                return JSONReWrapper(self, key, v)
            else:
                return v

        return wrapval(self._get_cache(key, *self.db.btt()))

    def __setitem__(self, key, value):
        """Set key=value at the present branch and revision"""
        if value is None:
            raise ValueError(
                "allegedb uses None to indicate that a key's been deleted"
            )
        branch, turn, tick = self.db.nbtt()
        try:
            if self._get_cache(key, branch, turn, tick) != value:
                self._set_cache(key, branch, turn, tick, value)
        except KeyError:
            self._set_cache(key, branch, turn, tick, value)
        self._set_db(key, branch, turn, tick, value)
        self.send(self, key=key, value=value)

    def __delitem__(self, key):
        branch, turn, tick = self.db.nbtt()
        self._del_cache(key, branch, turn, tick)
        self._del_db(key, branch, turn, tick)
        self.send(self, key=key, value=None)


class GraphMapping(AbstractEntityMapping):
    """Mapping for graph attributes"""
    db = getatt('graph.db')

    def __init__(self, graph):
        super().__init__()
        self.graph = graph

    def __iter__(self):
        return self.db._graph_val_cache.iter_entity_keys(
            self.graph.name, *self.db.btt(), forward=self.db.forward
        )

    def _cache_contains(self, key, branch, turn, tick):
        return self.db._graph_val_cache.contains_key(
            self.graph, key, branch, turn, tick, forward=self.db.forward
        )

    def __len__(self):
        return self.db._graph_val_cache.count_entities(
            self.graph.name, *self.db.btt(), forward=self.db.forward
        )

    def _get_cache(self, key, branch, turn, tick):
        return self.db._graph_val_cache.retrieve(
            self.graph.name, key, branch, turn, tick
        )

    def _get(self, key):
        return self._get_cache(key, *self.db.btt())

    def _set_db(self, key, branch, turn, tick, value):
        self.db.query.graph_val_set(
            self.graph.name,
            key,
            branch, turn, tick,
            value
        )

    def _set_cache(self, key, branch, turn, tick, value):
        self.db._graph_val_cache.store(
            self.graph.name, key, branch, turn, tick, value,
            planning=self.db.planning, forward=self.db.forward
        )

    def _del_db(self, key, branch, turn, tick):
        self.db.query.graph_val_del(
            self.graph.name,
            key,
            branch, turn, tick
        )


class Node(AbstractEntityMapping):
    """Mapping for node attributes"""
    db = getatt('graph.db')

    def __init__(self, graph, node):
        """Store name and graph"""
        super().__init__()
        self.graph = graph
        self.node = node

    def __iter__(self):
        return self.db._node_val_cache.iter_entity_keys(
            self.graph.name, self.node, *self.db.btt(),
            forward=self.db.forward
        )

    def _cache_contains(self, key, branch, turn, tick):
        return self.db._node_val_cache.contains_key(
            self.graph, self.node, key, branch, turn, tick,
            forward=self.db.forward
        )

    def __len__(self):
        return self.db._node_val_cache.count_entity_keys(
            self.graph.name, self.node, *self.db.btt(),
            forward=self.db.forward
        )

    def _get_cache(self, key, branch, turn, tick):
        return self.db._node_val_cache.retrieve(
            self.graph.name, self.node, key, branch, turn, tick
        )

    def _set_db(self, key, branch, turn, tick, value):
        self.db.query.node_val_set(
            self.graph.name,
            self.node,
            key,
            branch, turn, tick,
            value
        )

    def _set_cache(self, key, branch, turn, tick, value):
        self.db._node_val_cache.store(
            self.graph.name,
            self.node,
            key,
            branch, turn, tick,
            value,
            forward=self.db.forward
        )


class Edge(AbstractEntityMapping):
    """Mapping for edge attributes"""
    db = getatt('graph.db')

    def __init__(self, graph, orig, dest, idx=0):
        """Store the graph, the names of the nodes, and the index.

        For non-multigraphs the index is always 0.

        """
        super().__init__()
        self.graph = graph
        self.orig = orig
        self.dest = dest
        self.idx = idx

    def __iter__(self):
        return self.db._edge_val_cache.iter_entity_keys(
            self.graph.name,
            self.orig,
            self.dest,
            self.idx,
            *self.db.btt(),
            forward=self.db.forward
        )

    def _cache_contains(self, key, branch, turn, tick):
        return self.db._edge_val_cache.contains_key(
            self.graph.name, self.orig, self.dest, self.idx, key, branch, turn, tick,
            forward=self.db.forward
        )

    def __len__(self):
        return self.db._edge_val_cache.count_entity_keys(
            self.graph.name,
            self.orig,
            self.dest,
            self.idx,
            *self.db.btt(),
            forward=self.db.forward
        )

    def _get_cache(self, key, branch, turn, tick):
        return self.db._edge_val_cache.retrieve(
            self.graph.name,
            self.orig,
            self.dest,
            self.idx,
            key,
            branch, turn, tick
        )

    def _set_db(self, key, branch, turn, tick, value):
        self.db.query.edge_val_set(
            self.graph.name,
            self.orig,
            self.dest,
            self.idx,
            key,
            branch, turn, tick,
            value
        )

    def _set_cache(self, key, branch, turn, tick, value):
        self.db._edge_val_cache.store(
            self.graph.name,
            self.orig,
            self.dest,
            self.idx,
            key,
            branch, turn, tick,
            value,
            forward=self.db.forward
        )


class GraphNodeMapping(NeatMapping, Signal):
    """Mapping for nodes in a graph"""
    db = getatt('graph.db')

    def __init__(self, graph):
        super().__init__()
        self.graph = graph

    def __iter__(self):
        """Iterate over the names of the nodes"""
        return self.db._nodes_cache.iter_entities(
            self.graph.name, *self.db.btt()
        )

    def __contains__(self, node):
        """Return whether the node exists presently"""
        return self.db._nodes_cache.contains_entity(
            self.graph.name, node, *self.db.btt(), forward=self.db.forward
        )

    def __len__(self):
        """How many nodes exist right now?"""
        return self.db._nodes_cache.count_entities(
            self.graph.name, *self.db.btt(), forward=self.db.forward
        )

    def __getitem__(self, node):
        """If the node exists at present, return it, else throw KeyError"""
        if node not in self:
            raise KeyError
        return self.db._node_objs[(self.graph.name, node)]

    def __setitem__(self, node, dikt):
        """Only accept dict-like values for assignment. These are taken to be
        dicts of node attributes, and so, a new GraphNodeMapping.Node
        is made with them, perhaps clearing out the one already there.

        """
        branch, turn, tick = self.db.nbtt()
        planning = self.db.planning
        created = node not in self
        self.db._nodes_cache.store(
            self.graph.name,
            node,
            branch, turn, tick,
            True,
            planning=planning, forward=self.db.forward
        )
        if (self.graph.name, node) in self.db._node_objs:
            n = self.db._node_objs[(self.graph.name, node)]
            n.clear()
        else:
            n = self.db._node_objs[(self.graph.name, node)] = Node(
                self.graph, node
            )
        n.update(dikt)
        if created:
            self.db.query.exist_node(
                self.graph.name,
                node,
                branch, turn, tick,
                True
            )
            self.send(self, node_name=node, exists=True)

    def __delitem__(self, node):
        """Indicate that the given node no longer exists"""
        if node not in self:
            raise KeyError("No such node")
        branch, turn, tick = self.db.btt()
        self.db.query.exist_node(
            self.graph.name,
            node,
            branch, turn, tick,
            False
        )
        self.db._nodes_cache.store(
            self.graph.name,
            node,
            branch, turn, tick,
            False,
            planning=self.db.planning, forward=self.db.forward
        )
        self.send(self, node_name=node, exists=False)

    def __eq__(self, other):
        """Compare values cast into dicts.

        As I serve the custom Node class, rather than dicts like
        networkx normally would, the normal comparison operation would
        not let you compare my nodes with regular networkx
        nodes-that-are-dicts. So I cast my nodes into dicts for this
        purpose, and cast the other argument's nodes the same way, in
        case it is a db graph.

        """
        if not hasattr(other, 'keys'):
            return False
        if self.keys() != other.keys():
            return False
        for k in self.keys():
            if dict(self[k]) != dict(other[k]):
                return False
        return True


class GraphEdgeMapping(NeatMapping, Signal):
    """Provides an adjacency mapping and possibly a predecessor mapping
    for a graph.

    """
    _metacache = defaultdict(dict)

    @property
    def _cache(self):
        return self._metacache[id(self)]

    db = getatt('graph.db')

    def __init__(self, graph):
        super().__init__()
        self.graph = graph

    def __eq__(self, other):
        """Compare dictified versions of the edge mappings within me.

        As I serve custom Predecessor or Successor classes, which
        themselves serve the custom Edge class, I wouldn't normally be
        comparable to a networkx adjacency dictionary. Converting
        myself and the other argument to dicts allows the comparison
        to work anyway.

        """
        if not hasattr(other, 'keys'):
            return False
        if self.keys() != other.keys():
            return False
        for k in self.keys():
            if dict(self[k]) != dict(other[k]):
                return False
        return True

    def __iter__(self):
        return iter(self.graph.node)


class AbstractSuccessors(GraphEdgeMapping):
    db = getatt('graph.db')
    _metacache = defaultdict(dict)

    @property
    def _cache(self):
        return self._metacache[id(self)]

    def __init__(self, container, orig):
        """Store container and node"""
        super().__init__(container.graph)
        self.container = container
        self.orig = orig

    def __iter__(self):
        """Iterate over node IDs that have an edge with my orig"""
        return self.db._edges_cache.iter_successors(
            self.graph.name,
            self.orig,
            *self.db.btt(),
            forward=self.db.forward
        )

    def __contains__(self, dest):
        """Is there an edge leading to ``dest`` at the moment?"""
        return self.db._edges_cache.has_successor(
            self.graph.name,
            self.orig,
            dest,
            *self.db.btt(),
            forward=self.db.forward
        )

    def __len__(self):
        """How many nodes touch an edge shared with my orig?"""
        return self.db._edges_cache.count_successors(
            self.graph.name,
            self.orig,
            *self.db.btt(),
            forward=self.db.forward
        )

    def _make_edge(self, dest):
        return Edge(self.graph, self.orig, dest)

    def __getitem__(self, dest):
        """Get the edge between my orig and the given node"""
        if dest not in self:
            raise KeyError("No edge {}->{}".format(self.orig, dest))
        if dest not in self._cache:
            self._cache[dest] = self._make_edge(dest)
        return self._cache[dest]

    def __setitem__(self, dest, value):
        """Set the edge between my orig and the given dest to the given
        value, a mapping.

        """
        branch, turn, tick = self.db.btt()
        created = dest not in self
        planning=self.db.planning
        self.db.query.exist_edge(
            self.graph.name,
            self.orig,
            dest,
            0,
            branch, turn, tick,
            True
        )
        self.db._edges_cache.store(
            self.graph.name,
            self.orig,
            dest,
            0,
            branch, turn, tick,
            True,
            planning=planning,
            forward=self.db.forward
        )
        e = self[dest]
        e.clear()
        e.update(value)
        if created:
            self.send(self, orig=self.orig, dest=dest, idx=0, exists=True)

    def __delitem__(self, dest):
        """Remove the edge between my orig and the given dest"""
        branch, turn, tick = self.db.btt()
        self.db.query.exist_edge(
            self.graph.name,
            self.orig,
            dest,
            0,
            branch, turn, tick,
            False
        )
        self.db._edges_cache.store(
            self.graph.name,
            self.orig,
            dest,
            0,
            branch, turn, tick,
            None,
            planning=self.db.planning,
            forward=self.db.forward
        )
        self.send(self, orig=self.orig, dest=dest, idx=0, exists=False)

    def clear(self):
        """Delete every edge with origin at my orig"""
        for dest in list(self):
            del self[dest]


class GraphSuccessorsMapping(GraphEdgeMapping):
    """Mapping for Successors (itself a MutableMapping)"""
    class Successors(AbstractSuccessors):
        def _order_nodes(self, dest):
            if dest < self.orig:
                return (dest, self.orig)
            else:
                return (self.orig, dest)

    def __getitem__(self, orig):
        if orig not in self:
            raise KeyError("No edges from {}".format(orig))
        if orig not in self._cache:
            self._cache[orig] = self.Successors(self, orig)
        return self._cache[orig]

    def __setitem__(self, key, val):
        """Wipe out any edges presently emanating from orig and replace them
        with those described by val

        """
        if key in self:
            sucs = self[key]
            created = False
        else:
            sucs = self._cache[key] = self.Successors(self, key)
            created = True
        sucs.clear()
        sucs.update(val)
        if created:
            self.send(self, key=key, val=val)

    def __delitem__(self, key):
        """Wipe out edges emanating from orig"""
        self[key].clear()
        del self._cache[key]
        self.send(self, key=key, val=None)

    def __iter__(self):
        return iter(self.graph.node)

    def __len__(self):
        return len(self.graph.node)

    def __contains__(self, key):
        return key in self.graph.node


class DiGraphSuccessorsMapping(GraphSuccessorsMapping):
    class Successors(AbstractSuccessors):
        def _order_nodes(self, dest):
            return (self.orig, dest)


class DiGraphPredecessorsMapping(GraphEdgeMapping):
    """Mapping for Predecessors instances, which map to Edges that end at
    the dest provided to this

    """
    _predcache = defaultdict(dict)

    def __contains__(self, dest):
        return dest in self.graph.node

    def __getitem__(self, dest):
        """Return a Predecessors instance for edges ending at the given
        node

        """
        if dest not in self:
            raise KeyError("No edges available")
        if dest not in self._cache:
            self._cache[dest] = self.Predecessors(self, dest)
        return self._cache[dest]

    def _getpreds(self, dest):
        cache = self._predcache[id(self)]
        if dest not in cache:
            cache[dest] = self.Predecessors(self, dest)
        return cache[dest]

    def __setitem__(self, key, val):
        """Interpret ``val`` as a mapping of edges that end at ``dest``"""
        created = key not in self
        preds = self._getpreds(key)
        preds.clear()
        preds.update(val)
        if created:
            self.send(self, key=key, val=val)

    def __delitem__(self, key):
        """Delete all edges ending at ``dest``"""
        self._getpreds(key).clear()
        self.send(self, key=key, val=None)

    def __iter__(self):
        return iter(self.graph.node)

    def __len__(self):
        return len(self.graph.node)

    class Predecessors(GraphEdgeMapping):
        """Mapping of Edges that end at a particular node"""

        def __init__(self, container, dest):
            """Store container and node ID"""
            super().__init__(container.graph)
            self.container = container
            self.dest = dest

        def __iter__(self):
            """Iterate over the edges that exist at the present (branch, rev)

            """
            return self.db._edges_cache.iter_predecessors(
                self.graph.name,
                self.dest,
                *self.db.btt(),
                forward=self.db.forward
            )

        def __contains__(self, orig):
            """Is there an edge from ``orig`` at the moment?"""
            return self.db._edges_cache.has_predecessor(
                self.graph.name,
                self.dest,
                orig,
                *self.db.btt(),
                forward=self.db.forward
            )

        def __len__(self):
            """How many edges exist at this rev of this branch?"""
            return self.db._edges_cache.count_predecessors(
                self.graph.name,
                self.dest,
                *self.db.btt(),
                forward=self.db.forward
            )

        def _make_edge(self, orig):
            return Edge(self.graph, orig, self.dest)

        def __getitem__(self, orig):
            """Get the edge from the given node to mine"""
            return self.graph.adj[orig][self.dest]

        def __setitem__(self, orig, value):
            """Use ``value`` as a mapping of edge attributes, set an edge from the
            given node to mine.

            """
            branch, turn, tick = self.db.nbtt()
            planning=self.db.planning
            try:
                e = self[orig]
                e.clear()
                created = False
            except KeyError:
                self.db.query.exist_edge(
                    self.graph.name,
                    orig,
                    self.dest,
                    0,
                    branch, turn, tick,
                    True
                )
                e = self._make_edge(orig)
                created = True
            e.update(value)
            self.db._edges_cache.store(
                self.graph.name,
                orig,
                self.dest,
                0,
                branch, turn, tick,
                True,
                planning=planning,
                forward=self.db.forward
            )
            self.send(self, key=orig, val=value)

        def __delitem__(self, orig):
            """Unset the existence of the edge from the given node to mine"""
            branch, turn, tick = self.db.nbtt()
            planning = self.db.planning
            if 'Multi' in self.graph.__class__.__name__:
                for idx in self[orig]:
                    self.db.query.exist_edge(
                        self.graph.name,
                        orig,
                        self.dest,
                        idx,
                        branch, turn, tick,
                        False
                    )
                    self.db._edges_cache.store(
                        self.graph.name,
                        orig,
                        self.dest,
                        idx,
                        branch, turn, tick,
                        False,
                        planning=planning,
                        forward=self.db.forward
                    )
                    self.deleted.send(self, key=orig)
                    return
            self.db.query.exist_edge(
                self.graph.name,
                orig,
                self.dest,
                0,
                branch, turn, tick,
                False
            )
            self.db._edges_cache.store(
                self.graph.name,
                orig,
                self.dest,
                0,
                branch, turn, tick,
                None,
                planning=planning,
                forward=self.db.forward
            )
            self.send(self, key=orig, value=None)


class MultiEdges(GraphEdgeMapping, Signal):
    """Mapping of Edges between two nodes"""
    db = getatt('graph.db')

    def __init__(self, graph, orig, dest):
        super().__init__(graph)
        self.orig = orig
        self.dest = dest

    def __iter__(self):
        return self.db._edges_cache.iter_keys(
            self.graph.name, self.orig, self.dest,
            *self.db.btt()
        )

    def __len__(self):
        """How many edges currently connect my two nodes?"""
        n = 0
        for idx in iter(self):
            n += 1
        return n

    def __contains__(self, i):
        return self.db._edges_cache.contains_key(
            self.graph.name, self.orig, self.dest, i,
            *self.db.btt(), forward=self.db.forward
        )

    def _getedge(self, idx):
        if idx not in self._cache:
            self._cache[idx] = Edge(self.graph, self.orig, self.dest, idx)
        return self._cache[idx]

    def __getitem__(self, idx):
        """Get an Edge with a particular index, if it exists at the present
        (branch, rev)

        """
        if idx not in self:
            raise KeyError("No edge at that index")
        return self._getedge(idx)

    def __setitem__(self, idx, val):
        """Create an Edge at a given index from a mapping. Delete the existing
        Edge first, if necessary.

        """
        branch, turn, tick = self.db.nbtt()
        planning = self.db.planning
        created = idx not in self
        self.db.query.exist_edge(
            self.graph.name,
            self.orig,
            self.dest,
            idx,
            branch, turn, tick,
            True,
            planning=planning
        )
        e = self._getedge(idx)
        e.clear()
        e.update(val)
        self.db._edges_cache.store(
            self.graph.name, self.orig, self.dest, idx,
            branch, turn, tick, True,
            planning=planning, forward=self.db.forward
        )
        if created:
            self.send(self, orig=self.orig, dest=self.dest, idx=idx, exists=True)

    def __delitem__(self, idx):
        """Delete the edge at a particular index"""
        branch, turn, tick = self.db.btt()
        tick += 1
        e = self._getedge(idx)
        if not e.exists:
            raise KeyError("No edge at that index")
        e.clear()
        del self._cache[idx]
        self.db._edges_cache.store(
            self.graph.name, self.orig, self.dest, idx,
            branch, turn, tick, None, forward=self.db.forward
        )
        self.db.tick = tick
        self.send(self, orig=self.orig, dest=self.dest, idx=idx, exists=False)

    def clear(self):
        """Delete all edges between these nodes"""
        for idx in self:
            del self[idx]


class MultiGraphSuccessorsMapping(GraphSuccessorsMapping):
    """Mapping of Successors that map to MultiEdges"""
    def __getitem__(self, orig):
        """If the node exists, return its Successors"""
        if orig not in self.graph.node:
            raise KeyError("No such node")
        return self.Successors(self, orig)

    def _getsucc(self, orig):
        if orig not in self._cache:
            self._cache[orig] = self.Successors(self, orig)
        return self._cache[orig]

    def __setitem__(self, orig, val):
        """Interpret ``val`` as a mapping of successors, and turn it into a
        proper Successors object for storage

        """
        created = orig in self
        r = self._getsucc(orig)
        r.clear()
        r.update(val)
        if created:
            self.created.send(self, key=orig, val=val)

    def __delitem__(self, orig):
        """Disconnect this node from everything"""
        succs = self._getsucc(orig)
        succs.clear()
        del self._cache[orig]
        self.deleted.send(self, key=orig)

    class Successors(AbstractSuccessors):
        """Edges succeeding a given node in a multigraph"""
        def _order_nodes(self, dest):
            if dest < self.orig:
                return(dest, self.orig)
            else:
                return (self.orig, dest)

        _multedge = {}

        def _get_multedge(self, dest):
            if dest not in self._multedge:
                self._multedge[dest] = MultiEdges(
                    self.graph, *self._order_nodes(dest)
                )
            return self._multedge[dest]

        def __getitem__(self, dest):
            """Return MultiEdges to ``dest`` if it exists"""
            if dest in self.graph.node:
                return self._get_multedge(dest)
            raise KeyError("No such node")

        def __setitem__(self, dest, val):
            """Interpret ``val`` as a dictionary of edge attributes for edges
            between my ``orig`` and the given ``dest``

            """
            created = dest not in self
            self[dest].update(val)
            if created:
                self.created.send(self, key=dest, val=val)

        def __delitem__(self, dest):
            """Delete all edges between my ``orig`` and the given ``dest``"""
            self[dest].clear()
            del self._multedge[dest]
            self.deleted.send(self, key=dest)


class MultiDiGraphPredecessorsMapping(DiGraphPredecessorsMapping):
    """Version of DiGraphPredecessorsMapping for multigraphs"""
    class Predecessors(DiGraphPredecessorsMapping.Predecessors):
        """Predecessor edges from a given node"""
        def __getitem__(self, orig):
            """Get MultiEdges"""
            return MultiEdges(self.graph, orig, self.dest)

        def __setitem__(self, orig, val):
            created = orig not in self
            self[orig].update(val)
            if created:
                self.created.send(self, key=orig, val=val)

        def __delitem__(self, orig):
            self[orig].clear()
            self.deleted.send(self, key=orig)


class AllegedGraph(object):
    """Class giving the graphs those methods they share in
    common.

    """
    _succs = {}
    _statmaps = {}

    def __init__(self, db, name, data=None, **attr):
        self._name = name
        self.db = db
        if name not in self.db._graph_objs:
            self.db._graph_objs[name] = self
        if data is not None:
            convert_to_networkx_graph(data, create_using=self)
        self.graph.update(attr)

    @property
    def graph(self):
        if self._name not in self._statmaps:
            self._statmaps[self._name] = GraphMapping(self)
        return self._statmaps[self._name]

    @graph.setter
    def graph(self, v):
        self.graph.clear()
        self.graph.update(v)

    _nodemaps = {}

    @property
    def node(self):
        if self._name not in self._nodemaps:
            self._nodemaps[self._name] = GraphNodeMapping(self)
        return self._nodemaps[self._name]

    @node.setter
    def node(self, v):
        self.node.clear()
        self.node.update(v)
    _node = node

    _succmaps = {}

    @property
    def adj(self):
        if self._name not in self._succmaps:
            self._succmaps[self._name] = self.adj_cls(self)
        return self._succmaps[self._name]

    @adj.setter
    def adj(self, v):
        self.adj.clear()
        self.adj.update(v)
    edge = succ = _succ = _adj = adj

    _predmaps = {}

    @property
    def pred(self):
        if not hasattr(self, 'pred_cls'):
            raise TypeError("Undirected graph")
        if self._name not in self._predmaps:
            self._predmaps[self._name] = self.pred_cls(self)
        return self._predmaps[self._name]

    @pred.setter
    def pred(self, v):
        self.pred.clear()
        self.pred.update(v)
    _pred = pred

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, v):
        raise TypeError("graphs can't be renamed")

    def _and_previous(self):
        """Return a 4-tuple that will usually be (current branch, current
        revision - 1, current branch, current revision), unless
        current revision - 1 is before the start of the current
        branch, in which case the first element will be the parent
        branch.

        """
        branch = self.db.branch
        rev = self.db.rev
        (parent, parent_rev) = self.db.sql('parparrev', branch).fetchone()
        before_branch = parent if parent_rev == rev else branch
        return (before_branch, rev-1, branch, rev)

    def clear(self):
        """Remove all nodes and edges from the graph.

        Unlike the regular networkx implementation, this does *not*
        remove the graph's name. But all the other graph, node, and
        edge attributes go away.

        """
        self.adj.clear()
        self.node.clear()
        self.graph.clear()


class Graph(AllegedGraph, networkx.Graph):
    """A version of the networkx.Graph class that stores its state in a
    database.

    """
    adj_cls = GraphSuccessorsMapping


class DiGraph(AllegedGraph, networkx.DiGraph):
    """A version of the networkx.DiGraph class that stores its state in a
    database.

    """
    adj_cls = DiGraphSuccessorsMapping
    pred_cls = DiGraphPredecessorsMapping

    def remove_edge(self, u, v):
        """Version of remove_edge that's much like normal networkx but only
        deletes once, since the database doesn't keep separate adj and
        succ mappings

        """
        try:
            del self.succ[u][v]
        except KeyError:
            raise NetworkXError(
                "The edge {}-{} is not in the graph.".format(u, v)
            )

    def remove_edges_from(self, ebunch):
        """Version of remove_edges_from that's much like normal networkx but only
        deletes once, since the database doesn't keep separate adj and
        succ mappings

        """
        for e in ebunch:
            (u, v) = e[:2]
            if u in self.succ and v in self.succ[u]:
                del self.succ[u][v]

    def add_edge(self, u, v, attr_dict=None, **attr):
        """Version of add_edge that only writes to the database once"""
        if attr_dict is None:
            attr_dict = attr
        else:
            try:
                attr_dict.update(attr)
            except AttributeError:
                raise NetworkXError(
                    "The attr_dict argument must be a dictionary."
                )
        datadict = self.adj[u].get(v, {})
        datadict.update(attr_dict)
        if u not in self.node:
            self.node[u] = {}
        if v not in self.node:
            self.node[v] = {}
        self.succ[u][v] = datadict
        assert(
            u in self.succ and
            v in self.succ[u]
        )

    def add_edges_from(self, ebunch, attr_dict=None, **attr):
        """Version of add_edges_from that only writes to the database once"""
        if attr_dict is None:
            attr_dict = attr
        else:
            try:
                attr_dict.update(attr)
            except AttributeError:
                raise NetworkXError(
                    "The attr_dict argument must be a dict."
                )
        for e in ebunch:
            ne = len(e)
            if ne == 3:
                u, v, dd = e
                assert hasattr(dd, "update")
            elif ne == 2:
                u, v = e
                dd = {}
            else:
                raise NetworkXError(
                    "Edge tupse {} must be a 2-tuple or 3-tuple.".format(e)
                )
            if u not in self.node:
                self.node[u] = {}
            if v not in self.node:
                self.node[v] = {}
            datadict = self.adj.get(u, {}).get(v, {})
            datadict.update(attr_dict)
            datadict.update(dd)
            self.succ[u][v] = datadict
            assert(u in self.succ)
            assert(v in self.succ[u])


class MultiGraph(AllegedGraph, networkx.MultiGraph):
    """A version of the networkx.MultiGraph class that stores its state in a
    database.

    """
    adj_cls = MultiGraphSuccessorsMapping


class MultiDiGraph(AllegedGraph, networkx.MultiDiGraph):
    """A version of the networkx.MultiDiGraph class that stores its state in a
    database.

    """
    adj_cls = MultiGraphSuccessorsMapping
    pred_cls = MultiDiGraphPredecessorsMapping

    def remove_edge(self, u, v, key=None):
        """Version of remove_edge that's much like normal networkx but only
        deletes once, since the database doesn't keep separate adj and
        succ mappings

        """
        try:
            d = self.adj[u][v]
        except KeyError:
            raise NetworkXError(
                "The edge {}-{} is not in the graph.".format(u, v)
            )
        if key is None:
            d.popitem()
        else:
            try:
                del d[key]
            except KeyError:
                raise NetworkXError(
                    "The edge {}-{} with key {} is not in the graph.".format
                    (u, v, key)
                )
        if len(d) == 0:
            del self.succ[u][v]

    def remove_edges_from(self, ebunch):
        """Version of remove_edges_from that's much like normal networkx but only
        deletes once, since the database doesn't keep separate adj and
        succ mappings

        """
        for e in ebunch:
            (u, v) = e[:2]
            if u in self.succ and v in self.succ[u]:
                del self.succ[u][v]

    def add_edge(self, u, v, key=None, attr_dict=None, **attr):
        """Version of add_edge that only writes to the database once."""
        if attr_dict is None:
            attr_dict = attr
        else:
            try:
                attr_dict.update(attr)
            except AttributeError:
                raise NetworkXError(
                    "The attr_dict argument must be a dictionary."
                )
        if u not in self.node:
            self.node[u] = {}
        if v not in self.node:
            self.node[v] = {}
        if v in self.succ[u]:
            keydict = self.adj[u][v]
            if key is None:
                key = len(keydict)
                while key in keydict:
                    key += 1
            datadict = keydict.get(key, {})
            datadict.update(attr_dict)
            keydict[key] = datadict
        else:
            if key is None:
                key = 0
            datadict = {}
            datadict.update(attr_dict)
            keydict = {key: datadict}
            self.succ[u][v] = keydict
