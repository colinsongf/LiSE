# This file is part of LiSE, a framework for life simulation games.
# Copyright (c) Zachary Spector,  zacharyspector@gmail.com
"""That which displays a one-way connection between two places.

An arrow connects two spots, the origin and the destination, and it
points from the origin to the destination, regardless of where on the
screen they are at the moment.

"""
from math import cos, sin, hypot, atan, pi
from kivy.uix.widget import Widget
from kivy.properties import (
    ReferenceListProperty,
    AliasProperty,
    ObjectProperty,
    NumericProperty,
    ListProperty,
    BooleanProperty,
    StringProperty
)
from kivy.lang import Builder
from kivy.clock import Clock

try:
    from kivy.garden.collider import Collide2DPoly
except (KeyError, ImportError):
    from ..util import Collide2DPoly
from ..util import get_thin_rect_vertices, fortyfive


def get_points(ox, oy, ro, dx, dy, rd, taillen):
    """Return a pair of lists of points for use making an arrow.

    The first list is the beginning and end point of the trunk of the arrow.

    The second list is the arrowhead.

    """
    # handle special cases;
    # flip the arrow, if needed, to make it point up and right;
    # store coefficients to flip it back again
    if ox < dx:
        leftx = ox
        rightx = dx
        xco = 1
    elif ox > dx:
        leftx = ox * -1
        rightx = dx * -1
        xco = -1
    else:
        off1 = cos(fortyfive) * taillen
        off2 = sin(fortyfive) * taillen
        x0 = ox
        y0 = oy + ro if oy < dy else oy - ro
        endx = dx
        endy = dy - rd if oy < dy else dy + rd
        x1 = endx - off1
        x2 = endx + off1
        y1 = y2 = endy - off2 if oy < dy else endy + off2
        return (
            [x0, y0, endx, endy],
            [x1, y1, endx, endy, x2, y2]
        )
    if oy < dy:
        boty = oy
        topy = dy
        yco = 1
    elif oy > dy:
        boty = oy * -1
        topy = dy * -1
        yco = -1
    else:
        off1 = cos(fortyfive) * taillen
        off2 = sin(fortyfive) * taillen
        x0 = ox + ro if ox < dx else ox - ro
        y0 = oy
        endx = dx - rd if ox < dx else dx + rd
        endy = dy
        y1 = endy - off1
        y2 = endy + off1
        x1 = x2 = endx - off2 if ox < dx else endx + off2
        return (
            [x0, y0, endx, endy],
            [x1, y1, endx, endy, x2, y2]
        )

    rise = topy - boty
    run = rightx - leftx

    # truncate the end so it just touches the destination circle.
    start_theta = atan(rise/run)
    end_theta = atan(run/rise)
    length = hypot(run, rise) - rd
    rightx = leftx + cos(start_theta) * length
    topy = boty + sin(start_theta) * length
    # truncate the start so it's at the very edge of the origin circle.
    length -= ro
    leftx = rightx - sin(end_theta) * length
    boty = topy - cos(end_theta) * length

    # make the little wedge at the end so you can tell which way the
    # arrow's pointing, and flip it all back around to the way it was
    top_theta = start_theta - fortyfive
    bot_theta = pi - fortyfive - end_theta
    xoff1 = cos(top_theta) * taillen
    yoff1 = sin(top_theta) * taillen
    xoff2 = cos(bot_theta) * taillen
    yoff2 = sin(bot_theta) * taillen
    x1 = (rightx - xoff1) * xco
    x2 = (rightx - xoff2) * xco
    y1 = (topy - yoff1) * yco
    y2 = (topy - yoff2) * yco
    startx = leftx * xco
    starty = boty * yco
    endx = rightx * xco
    endy = topy * yco
    return (
        [startx, starty, endx, endy],
        [x1, y1, endx, endy, x2, y2]
    )


class ArrowWidget(Widget):
    """A widget that points from one :class:`~LiSE.gui.board.Spot` to
    another.

    :class:`Arrow`s are the graphical representations of
    :class:`~LiSE.model.Portal`s. They point from the :class:`Spot`
    representing the :class:`Portal`'s origin, to the one representing
    its destination.

    """
    board = ObjectProperty()
    name = StringProperty()
    margin = NumericProperty(10)
    """When deciding whether a touch collides with me, how far away can
    the touch get before I should consider it a miss?"""
    w = NumericProperty(2)
    """The width of the inner, brighter portion of the :class:`Arrow`. The
    whole :class:`Arrow` will end up thicker."""
    pawns_here = ListProperty([])
    trunk_points = ListProperty([])
    head_points = ListProperty([])
    points = ReferenceListProperty(trunk_points, head_points)
    trunk_quad_vertices_bg = ListProperty([0] * 8)
    trunk_quad_vertices_fg = ListProperty([0] * 8)
    left_head_quad_vertices_bg = ListProperty([0] * 8)
    right_head_quad_vertices_bg = ListProperty([0] * 8)
    left_head_quad_vertices_fg = ListProperty([0] * 8)
    right_head_quad_vertices_fg = ListProperty([0] * 8)
    slope = NumericProperty(0.0, allownone=True)
    y_intercept = NumericProperty(0)
    origin = ObjectProperty()
    destination = ObjectProperty()
    repointed = BooleanProperty(True)
    bg_scale_unselected = NumericProperty(4)
    bg_scale_selected = NumericProperty(5)
    selected = BooleanProperty(False)
    hit = BooleanProperty(False)
    bg_color_unselected = ListProperty()
    bg_color_selected = ListProperty()
    fg_color_unselected = ListProperty()
    fg_color_selected = ListProperty()
    arrowhead_size = NumericProperty(10)
    collide_radius = NumericProperty(3)
    collider = ObjectProperty()
    portal = ObjectProperty()

    def on_portal(self, *args):
        """Set my ``name`` and instantiate my ``mirrormap`` as soon as I have
        the properties I need to do so.

        """
        if not (
                self.board and
                self.origin and
                self.destination and
                self.origin.name in self.board.character.portal and
                self.destination.name in self.board.character.portal
        ):
            Clock.schedule_once(self.on_portal, 0)
            return
        self.name = '{}->{}'.format(self.portal['origin'], self.portal['destination'])

    def collide_point(self, x, y):
        """Delegate to my ``collider``, or return ``False`` if I don't have
        one.

        """
        if not self.collider:
            return False
        return (x, y) in self.collider

    def __init__(self, **kwargs):
        """Create trigger for my _repoint method. Delegate to parent for
        everything else.

        """
        self._trigger_repoint = Clock.create_trigger(
            self._repoint,
            timeout=-1
        )
        super().__init__(**kwargs)

    def on_origin(self, *args):
        """Make sure to redraw whenever the origin moves."""
        if self.origin is None:
            Clock.schedule_once(self.on_origin, 0)
            return
        self.origin.bind(
            pos=self._trigger_repoint,
            size=self._trigger_repoint
        )

    def on_destination(self, *args):
        """Make sure to redraw whenever the destination moves."""
        if self.destination is None:
            Clock.schedule_once(self.on_destination, 0)
            return
        self.destination.bind(
            pos=self._trigger_repoint,
            size=self._trigger_repoint
        )

    def on_board(self, *args):
        """Draw myself for the first time as soon as I have the properties I
        need to do so.

        """
        if None in (
                self.board,
                self.origin,
                self.destination
        ):
            Clock.schedule_once(self.on_board, 0)
            return
        self._trigger_repoint()

    def add_widget(self, wid, index=0, canvas=None):
        """Put the :class:`Pawn` at a point along my length proportionate to
        how close it is to finishing its travel through me.

        Only :class:`Pawn` should ever be added as a child of :class:`Arrow`.

        """
        super().add_widget(wid, index, canvas)
        if not hasattr(wid, 'group'):
            return
        wid._no_use_canvas = True
        mycanvas = (
            self.canvas.before if canvas == 'before' else
            self.canvas.after if canvas == 'after' else
            self.canvas
        )
        mycanvas.remove(wid.canvas)
        pawncanvas = (
            self.board.pawnlayout.canvas.before if canvas == 'before' else
            self.board.pawnlayout.canvas.after if canvas == 'after' else
            self.board.pawnlayout.canvas
        )
        for child in self.children:
            if hasattr(child, 'group') and child.group in pawncanvas.children:
                pawncanvas.remove(child.group)
            pawncanvas.add(child.group)
        self.pospawn(wid)

    def remove_widget(self, wid):
        """Remove the special :class:`InstructionGroup` I was using to draw
        this :class:`Pawn`.

        """
        super().remove_widget(wid)
        wid._no_use_canvas = False

    def on_points(self, *args):
        """Reposition my children when I have new points."""
        for pawn in self.children:
            self.pospawn(pawn)

    def pos_along(self, pct):
        """Return coordinates for where a Pawn should be if it has travelled
        along ``pct`` of my length (between 0 and 1).

        Might get complex when I switch over to using beziers for
        arrows, but for now this is quite simple, using distance along
        a line segment.

        """
        if pct < 0 or pct > 1:
            raise ValueError("Invalid portion")
        (ox, oy) = self.origin.center
        (dx, dy) = self.destination.center
        xdist = (dx - ox) * pct
        ydist = (dy - oy) * pct
        return (ox + xdist, oy + ydist)

    def pospawn(self, pawn):
        """Position a :class:`Pawn` that is my child so as to reflect how far
        its :class:`Thing` has gone along my :class:`Portal`.

        """
        if self._turn < pawn.thing['arrival_time']:
            # It's weird that the pawn is getting placed in me, but
            # I'll do my best..
            pawn.pos = self.pos_along(0)
            return
        elif (
                pawn.thing['next_arrival_time'] and
                self._turn >= pawn.thing['next_arrival_time']
        ):
            pawn.pos = self.pos_along(1)
            return
        try:
            pawn.pos = self.pos_along(
                (
                    self._turn -
                    pawn.thing['arrival_time']
                ) / (
                    pawn.thing['next_arrival_time'] -
                    pawn.thing['arrival_time']
                )
            )
        except (TypeError, ZeroDivisionError):
            pawn.pos = self.pos_along(0)

    def _get_points(self):
        """Return the coordinates of the points that describe my shape."""
        orig = self.origin
        dest = self.destination
        (ox, oy) = orig.center
        ow = orig.width if hasattr(orig, 'width') else 0
        taillen = float(self.arrowhead_size)
        ory = ow / 2
        (dx, dy) = dest.center
        (dw, dh) = dest.size if hasattr(dest, 'size') else (0, 0)
        dry = dh / 2
        return get_points(
            ox, oy, ory, dx, dy, dry, taillen
        )

    def _get_slope(self):
        """Return a float of the increase in y divided by the increase in x,
        both from left to right."""
        orig = self.origin
        dest = self.destination
        ox = orig.x
        oy = orig.y
        dx = dest.x
        dy = dest.y
        if oy == dy:
            return 0
        elif ox == dx:
            return None
        else:
            rise = dy - oy
            run = dx - ox
            return rise / run

    def _get_b(self):
        """Return my Y-intercept.

        I probably don't really hit the left edge of the window, but
        this is where I would, if I were long enough.

        """
        orig = self.origin
        dest = self.destination
        (ox, oy) = orig.pos
        (dx, dy) = dest.pos
        denominator = dx - ox
        x_numerator = (dy - oy) * ox
        y_numerator = denominator * oy
        return ((y_numerator - x_numerator), denominator)

    def _repoint(self, *args):
        """Recalculate points, y-intercept, and slope"""
        if None in (self.origin, self.destination):
            Clock.schedule_once(self._repoint, 0)
            return
        (self.trunk_points, self.head_points) = self._get_points()
        (ox, oy, dx, dy) = self.trunk_points
        r = self.w / 2
        bgr = r * self.bg_scale_selected if self.selected \
            else self.bg_scale_unselected
        self.trunk_quad_vertices_bg = get_thin_rect_vertices(
            ox, oy, dx, dy, bgr
        )
        self.collider = Collide2DPoly(self.trunk_quad_vertices_bg)
        self.trunk_quad_vertices_fg = get_thin_rect_vertices(ox, oy, dx, dy, r)
        (x1, y1, endx, endy, x2, y2) = self.head_points
        self.left_head_quad_vertices_bg = get_thin_rect_vertices(
            x1, y1, endx, endy, bgr
        )
        self.right_head_quad_vertices_bg = get_thin_rect_vertices(
            x2, y2, endx, endy, bgr
        )
        self.left_head_quad_vertices_fg = get_thin_rect_vertices(
            x1, y1, endx, endy, r
        )
        self.right_head_quad_vertices_fg = get_thin_rect_vertices(
            x2, y2, endx, endy, r
        )
        self.slope = self._get_slope()
        self.y_intercept = self._get_b()
        self.repointed = True


class Arrow(ArrowWidget):
    """An :class:`ArrowWidget` that represents a LiSE :class:`Portal` object.

    This subclass is much more often used than :class:`ArrowWidget`,
    which is only seen on its own when the user is in the process of
    creating a new :class:`Portal`.

    """
    portal = ObjectProperty()
    """The portal that I represent."""
    reciprocal = AliasProperty(
        lambda self: self._get_reciprocal()
        if None not in (self.board, self.portal) else None,
        lambda self, v: None,
        bind=('portal',)
    )
    grabbed = BooleanProperty(False)
    _turn = NumericProperty()

    def _get_reciprocal(self):
        """Return the :class:`Arrow` that connects my origin and destination
        in the opposite direction, if it exists.

        """
        orign = self.portal['origin']
        destn = self.portal['destination']
        if (
                destn in self.board.arrow and
                orign in self.board.arrow[destn]
        ):
            return self.board.arrow[destn][orign]
        else:
            return None


Builder.load_string(
"""
#: import Dummy ELiDE.dummy.Dummy
<ArrowWidget>:
    bg_color_unselected: [0.5, 0.5, 0.5, 0.5]
    bg_color_selected: [0.0, 1.0, 1.0, 1.0]
    fg_color_unselected: [1.0, 1.0, 1.0, 1.0]
    fg_color_selected: [1.0, 1.0, 1.0, 1.0]
    canvas:
        Color:
            rgba: root.bg_color_selected if root.selected else root.bg_color_unselected
        Quad:
            points: root.trunk_quad_vertices_bg
        Quad:
            points: root.left_head_quad_vertices_bg
        Quad:
            points: root.right_head_quad_vertices_bg
        Color:
            rgba: root.fg_color_selected if root.selected else root.fg_color_unselected
        Quad:
            points: root.trunk_quad_vertices_fg
        Quad:
            points: root.left_head_quad_vertices_fg
        Quad:
            points: root.right_head_quad_vertices_fg
<Arrow>:
    origin: self.board.spot[self.portal['origin']] if self.portal['origin'] in self.board.spot else Dummy()
    destination: self.board.spot[self.portal['destination']] if self.portal['destination'] in self.board.spot else Dummy()
    _turn: app.turn
"""
)
