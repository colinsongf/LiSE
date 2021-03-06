# This file is part of allegedb, an object relational mapper for graphs.
# Copyright (c) Zachary Spector
"""Wrapper to run SQL queries in a lightly abstracted way, such that
code that's more to do with the queries than with the data per se
doesn't pollute the other files so much.

"""
from collections import MutableMapping
from sqlite3 import IntegrityError as sqliteIntegError
try:
    # python 2
    import xjson
except ImportError:
    # python 3
    from allegedb import xjson
import os
xjpath = os.path.dirname(xjson.__file__)
alchemyIntegError = None
try:
    from sqlalchemy.exc import IntegrityError as alchemyIntegError
except ImportError:
    pass


IntegrityError = (
    alchemyIntegError, sqliteIntegError
) if alchemyIntegError is not None else sqliteIntegError


class GlobalKeyValueStore(MutableMapping):
    """A dict-like object that keeps its contents in a table.

    Mostly this is for holding the current branch and revision.

    """
    def __init__(self, qe):
        self.qe = qe

    def __iter__(self):
        if hasattr(self.qe, '_global_cache'):
            yield from self.qe._global_cache
            return
        for (k, v) in self.qe.global_items():
            yield k

    def __len__(self):
        if hasattr(self.qe, '_global_cache'):
            return len(self.qe._global_cache)
        return self.qe.ctglobal()

    def __getitem__(self, k):
        if hasattr(self.qe, '_global_cache'):
            return self.qe._global_cache[k]
        return self.qe.global_get(k)

    def __setitem__(self, k, v):
        self.qe.global_set(k, v)
        if hasattr(self.qe, '_global_cache'):
            self.qe._global_cache[k] = v

    def __delitem__(self, k):
        self.qe.global_del(k)
        if hasattr(self.qe, '_global_cache'):
            del self.qe._global_cache[k]


class QueryEngine(object):
    """Wrapper around either a DBAPI2.0 connection or an
    Alchemist. Provides methods to run queries using either.

    """
    json_path = xjpath

    def __init__(
            self, dbstring, connect_args, alchemy,
            json_dump=None, json_load=None
    ):
        """If ``alchemy`` is True and ``dbstring`` is a legit database URI,
        instantiate an Alchemist and start a transaction with
        it. Otherwise use sqlite3.

        You may pass an already created sqlalchemy :class:`Engine`
        object in place of ``dbstring`` if you wish. I'll still create
        my own transaction though.

        """
        dbstring = dbstring or 'sqlite:///:memory:'

        def alchem_init(dbstring, connect_args):
            from sqlalchemy import create_engine
            from sqlalchemy.engine.base import Engine
            from allegedb.alchemy import Alchemist
            if isinstance(dbstring, Engine):
                self.engine = dbstring
            else:
                self.engine = create_engine(
                    dbstring,
                    connect_args=connect_args
                )
            self.alchemist = Alchemist(self.engine)
            self.transaction = self.alchemist.conn.begin()

        def lite_init(dbstring, connect_args):
            from sqlite3 import connect, Connection
            from json import loads
            self.strings = loads(
                open(self.json_path + '/sqlite.json', 'r').read()
            )
            if isinstance(dbstring, Connection):
                self.connection = dbstring
            else:
                if dbstring.startswith('sqlite:'):
                    slashidx = dbstring.rindex('/')
                    dbstring = dbstring[slashidx+1:]
                self.connection = connect(dbstring)

        if alchemy:
            try:
                alchem_init(dbstring, connect_args)
            except ImportError:
                lite_init(dbstring, connect_args)
        else:
            lite_init(dbstring, connect_args)

        self.globl = GlobalKeyValueStore(self)
        self._branches = {}
        self._nodevals2set = []
        self._edgevals2set = []
        self._graphvals2set = []
        self._nodes2set = []
        self._edges2set = []
        self.json_dump = json_dump or xjson.json_dump
        self.json_load = json_load or xjson.json_load

    def sql(self, stringname, *args, **kwargs):
        """Wrapper for the various prewritten or compiled SQL calls.

        First argument is the name of the query, either a key in
        ``sqlite.json`` or a method name in
        ``allegedb.alchemy.Alchemist``. The rest of the arguments are
        parameters to the query.

        """
        if hasattr(self, 'alchemist'):
            return getattr(self.alchemist, stringname)(*args, **kwargs)
        else:
            s = self.strings[stringname]
            return self.connection.cursor().execute(
                s.format(**kwargs) if kwargs else s, args
            )

    def sqlmany(self, stringname, *args):
        """Wrapper for executing many SQL calls on my connection.

        First arg is the name of a query, either a key in the
        precompiled JSON or a method name in
        ``allegedb.alchemy.Alchemist``. Remaining arguments should be
        tuples of argument sequences to be passed to the query.

        """
        if hasattr(self, 'alchemist'):
            return getattr(self.alchemist.many, stringname)(*args)
        s = self.strings[stringname]
        return self.connection.cursor().executemany(s, args)

    def have_graph(self, graph):
        """Return whether I have a graph by this name."""
        graph = self.json_dump(graph)
        return bool(self.sql('graphs_named', graph).fetchone()[0])

    def new_graph(self, graph, typ):
        """Declare a new graph by this name of this type."""
        graph = self.json_dump(graph)
        return self.sql('new_graph', graph, typ)

    def del_graph(self, graph):
        """Delete all records to do with the graph"""
        g = self.json_dump(graph)
        self.sql('del_edge_val_graph', g)
        self.sql('del_node_val_graph', g)
        self.sql('del_edge_val_graph', g)
        self.sql('del_edges_graph', g)
        self.sql('del_nodes_graph', g)
        self.sql('del_graph', g)

    def graph_type(self, graph):
        """What type of graph is this?"""
        graph = self.json_dump(graph)
        return self.sql('graph_type', graph).fetchone()[0]

    def have_branch(self, branch):
        """Return whether the branch thus named exists in the database."""
        return bool(self.sql('ctbranch', branch).fetchone()[0])

    def all_branches(self):
        """Return all the branch data in tuples of (branch, parent,
        parent_turn).

        """
        return self.sql('branches_dump').fetchall()

    def global_get(self, key):
        """Return the value for the given key in the ``globals`` table."""
        key = self.json_dump(key)
        r = self.sql('global_get', key).fetchone()
        if r is None:
            raise KeyError("Not set")
        return self.json_load(r[0])

    def global_items(self):
        """Iterate over (key, value) pairs in the ``globals`` table."""
        for (k, v) in self.sql('global_dump'):
            yield (self.json_load(k), self.json_load(v))

    def global_set(self, key, value):
        """Set ``key`` to ``value`` globally (not at any particular branch or
        revision)

        """
        (key, value) = map(self.json_dump, (key, value))
        try:
            return self.sql('global_insert', key, value)
        except IntegrityError:
            return self.sql('global_update', value, key)

    def global_del(self, key):
        """Delete the global record for the key."""
        key = self.json_dump(key)
        return self.sql('global_del', key)

    def new_branch(self, branch, parent, parent_turn, parent_tick):
        """Declare that the ``branch`` is descended from ``parent`` at
        ``parent_turn``, ``parent_tick``

        """
        return self.sql('branches_insert', branch, parent, parent_turn, parent_tick, parent_turn, parent_tick)

    def update_branch(self, branch, parent, parent_turn, parent_tick, end_turn, end_tick):
        return self.sql('update_branches', parent, parent_turn, parent_tick, end_turn, end_tick, branch)

    def set_branch(self, branch, parent, parent_turn, parent_tick, end_turn, end_tick):
        try:
            self.sql('branches_insert', branch, parent, parent_turn, parent_tick, end_turn, end_tick)
        except IntegrityError:
            self.update_branch(branch, parent, parent_turn, parent_tick, end_turn, end_tick)

    def new_turn(self, branch, turn, end_tick=0, plan_end_tick=0):
        return self.sql('turns_insert', branch, turn, end_tick, plan_end_tick)

    def update_turn(self, branch, turn, end_tick, plan_end_tick):
        return self.sql('update_turns', end_tick, plan_end_tick, branch, turn)

    def set_turn(self, branch, turn, end_tick, plan_end_tick):
        try:
            return self.sql('turns_insert', branch, turn, end_tick, plan_end_tick)
        except IntegrityError:
            return self.sql('update_turns', end_tick, plan_end_tick, branch, turn)

    def turns_dump(self):
        return self.sql('turns_dump')

    def graph_val_dump(self):
        """Yield the entire contents of the graph_val table."""
        self._flush_graph_val()
        for (graph, key, branch, turn, tick, value) in self.sql('graph_val_dump'):
            yield (
                self.json_load(graph),
                self.json_load(key),
                branch,
                turn,
                tick,
                self.json_load(value)
            )

    def _flush_graph_val(self):
        """Send all new and changed graph values to the database."""
        if not self._graphvals2set:
            return
        delafter = {}
        for graph, key, branch, turn, tick, value in self._graphvals2set:
            if (graph, key, branch) in delafter:
                delafter[graph, key, branch] = min((
                    (turn, tick),
                    delafter[graph, key, branch]
                ))
            else:
                delafter[graph, key, branch] = (turn, tick)
        self.sqlmany(
            'del_graph_val_after',
            *((graph, key, branch, turn, turn, tick)
              for ((graph, key, branch), (turn, tick)) in delafter.items())
        )
        self.sqlmany('graph_val_insert', *self._graphvals2set)
        self._graphvals2set = []

    def graph_val_set(self, graph, key, branch, turn, tick, value):
        graph, key, value = map(self.json_dump, (graph, key, value))
        self._graphvals2set.append((graph, key, branch, turn, tick, value))

    def graph_val_del(self, graph, key, branch, turn, tick):
        """Indicate that the key is unset."""
        self.graph_val_set(graph, key, branch, turn, tick, None)

    def graphs_types(self):
        for (graph, typ) in self.sql('graphs_types'):
            yield (self.json_load(graph), typ)

    def _flush_nodes(self):
        if not self._nodes2set:
            return
        # delete history that is to be overwritten due to paradox
        cleanups = {}
        for graph, node, branch, turn, tick, extant in self._nodes2set:
            if (graph, node, branch) in cleanups:
                cleanups[graph, node, branch] = min((
                    (turn, tick), cleanups[graph, node, branch]
                ))
            else:
                cleanups[graph, node, branch] = (turn, tick)
        self.sqlmany('del_nodes_after', *(k + (turn, turn, tick) for k, (turn, tick) in cleanups.items()))
        self.sqlmany('nodes_insert', *self._nodes2set)
        self._nodes2set = []

    def exist_node(self, graph, node, branch, turn, tick, extant):
        """Declare that the node exists or doesn't.

        Inserts a new record or updates an old one, as needed.

        """
        self._nodes2set.append((self.json_dump(graph), self.json_dump(node), branch, turn, tick, extant))

    def nodes_dump(self):
        """Dump the entire contents of the nodes table."""
        self._flush_nodes()
        for (graph, node, branch, turn,tick, extant) in self.sql('nodes_dump'):
            yield (
                self.json_load(graph),
                self.json_load(node),
                branch,
                turn,
                tick,
                bool(extant)
            )

    def node_val_dump(self):
        """Yield the entire contents of the node_val table."""
        self._flush_node_val()
        for (
                graph, node, key, branch, turn, tick, value
        ) in self.sql('node_val_dump'):
            yield (
                self.json_load(graph),
                self.json_load(node),
                self.json_load(key),
                branch,
                turn,
                tick,
                self.json_load(value)
            )

    def _flush_node_val(self):
        if not self._nodevals2set:
            return
        delafter = {}
        for graph, node, key, branch, turn, tick, value in self._nodevals2set:
            if (graph, node, key, branch) in delafter:
                delafter[graph, node, key, branch] = min((
                    (turn, tick),
                    delafter[graph, node, key, branch]
                ))
            else:
                delafter[graph, node, key, branch] = (turn, tick)
        if delafter:
            self.sqlmany(
                'del_node_val_after',
                *((graph, node, key, branch, turn, turn, tick)
                  for ((graph, node, key, branch), (turn, tick)) in
                  delafter.items())
            )
        self.sqlmany('node_val_insert', *self._nodevals2set)
        self._nodevals2set = []

    def node_val_set(self, graph, node, key, branch, turn, tick, value):
        """Set a key-value pair on a node at a specific branch and revision"""
        graph, node, key, value = map(self.json_dump, (graph, node, key, value))
        self._nodevals2set.append((graph, node, key, branch, turn, tick, value))

    def node_val_del(self, graph, node, key, branch, turn, tick):
        """Delete a key from a node at a specific branch and revision"""
        self.node_val_set(graph, node, key, branch, turn, tick, None)

    def edges_dump(self):
        """Dump the entire contents of the edges table."""
        self._flush_edges()
        for (
                graph, orig, dest, idx, branch, turn, tick, extant
        ) in self.sql('edges_dump'):
            yield (
                self.json_load(graph),
                self.json_load(orig),
                self.json_load(dest),
                idx,
                branch,
                turn,
                tick,
                bool(extant)
            )

    def _flush_edges(self):
        if not self._edges2set:
            return
        delafter = {}
        for graph, orig, dest, idx, branch, turn, tick, extant in self._edges2set:
            key = graph, orig, dest, idx, branch
            if key in delafter:
                delafter[key] = min((
                    (turn, tick),
                    delafter[key]
                ))
            else:
                delafter[key] = (turn, tick)
        if delafter:
            self.sqlmany(
                'del_edges_after',
                *((graph, orig, dest, idx, branch, turn, turn, tick)
                  for ((graph, orig, dest, idx, branch), (turn, tick)) in
                  delafter.items())
            )
        self.sqlmany('edges_insert', *self._edges2set)
        self._edges2set = []

    def exist_edge(self, graph, orig, dest, idx, branch, turn, tick, extant):
        """Declare whether or not this edge exists."""
        graph, orig, dest = map(self.json_dump, (graph, orig, dest))
        self._edges2set.append((graph, orig, dest, idx, branch, turn, tick, extant))

    def edge_val_dump(self):
        """Yield the entire contents of the edge_val table."""
        self._flush_edge_val()
        for (
                graph, orig, dest, idx, key, branch, turn, tick, value
        ) in self.sql('edge_val_dump'):
            yield (
                self.json_load(graph),
                self.json_load(orig),
                self.json_load(dest),
                idx,
                self.json_load(key),
                branch,
                turn,
                tick,
                self.json_load(value)
            )

    def _flush_edge_val(self):
        if not self._edgevals2set:
            return
        delafter = {}
        for graph, orig, dest, idx, key, branch, turn, tick, value in self._edgevals2set:
            dkey = graph, orig, dest, idx, key, branch
            if dkey in delafter:
                delafter[dkey] = min((
                    (turn, tick), delafter[dkey]
                ))
            else:
                delafter[dkey] = (turn, tick)
        self.sqlmany(
            'del_edge_val_after',
            *((graph, orig, dest, idx, key, branch, turn, turn, tick)
              for ((graph, orig, dest, idx, key, branch), (turn, tick))
              in delafter.items())
        )
        self.sqlmany('edge_val_insert', *self._edgevals2set)
        self._edgevals2set = []

    def edge_val_set(self, graph, orig, dest, idx, key, branch, turn, tick, value):
        """Set this key of this edge to this value."""
        graph, orig, dest, key, value = map(self.json_dump, (graph, orig, dest, key, value))
        self._edgevals2set.append(
            (graph, orig, dest, idx, key, branch, turn, tick, value)
        )

    def edge_val_del(self, graph, orig, dest, idx, key, branch, turn, tick):
        """Declare that the key no longer applies to this edge, as of this
        branch and revision.

        """
        self.edge_val_set(graph, orig, dest, idx, key, branch, turn, tick, None)

    def initdb(self):
        """Create tables and indices as needed."""
        if hasattr(self, 'alchemist'):
            self.alchemist.meta.create_all(self.engine)
            if 'branch' not in self.globl:
                self.globl['branch'] = 'trunk'
            if 'rev' not in self.globl:
                self.globl['rev'] = 0
            return
        from sqlite3 import OperationalError
        cursor = self.connection.cursor()
        try:
            cursor.execute('SELECT * FROM global;')
        except OperationalError:
            cursor.execute(self.strings['create_global'])
        if 'branch' not in self.globl:
            self.globl['branch'] = 'trunk'
        if 'turn' not in self.globl:
            self.globl['turn'] = 0
        if 'tick' not in self.globl:
            self.globl['tick'] = 0
        try:
            cursor.execute('SELECT * FROM branches;')
        except OperationalError:
            cursor.execute(self.strings['create_branches'])
        try:
            cursor.execute('SELECT * FROM turns;')
        except OperationalError:
            cursor.execute(self.strings['create_turns'])
        try:
            cursor.execute('SELECT * FROM graphs;')
        except OperationalError:
            cursor.execute(self.strings['create_graphs'])
        try:
            cursor.execute('SELECT * FROM graph_val;')
        except OperationalError:
            cursor.execute(self.strings['create_graph_val'])
        try:
            cursor.execute('SELECT * FROM nodes;')
        except OperationalError:
            cursor.execute(self.strings['create_nodes'])

        try:
            cursor.execute('SELECT * FROM node_val;')
        except OperationalError:
            cursor.execute(self.strings['create_node_val'])
        try:
            cursor.execute('SELECT * FROM edges;')
        except OperationalError:
            cursor.execute(self.strings['create_edges'])
        try:
            cursor.execute('SELECT * FROM edge_val;')
        except OperationalError:
            cursor.execute(self.strings['create_edge_val'])

    def flush(self):
        """Put all pending changes into the SQL transaction."""
        self._flush_nodes()
        self._flush_edges()
        self._flush_graph_val()
        self._flush_node_val()
        self._flush_edge_val()

    def commit(self):
        """Commit the transaction"""
        self.flush()
        if hasattr(self, 'transaction'):
            self.transaction.commit()
        else:
            self.connection.commit()

    def close(self):
        """Commit the transaction, then close the connection"""
        self.commit()
        if hasattr(self, 'connection'):
            self.connection.close()
