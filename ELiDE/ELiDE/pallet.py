# This file is part of LiSE, a framework for life simulation games.
# Copyright (C) Zachary Spector, ZacharySpector@gmail.com
"""Widget to display the contents of a :class:`kivy.atlas.Atlas` in
one :class:`kivy.uix.togglebutton.ToggleButton` apiece, arranged in a
:class:`kivy.uix.stacklayout.StackLayout`. The user selects graphics
from the :class:`Pallet`, and the :class:`Pallet` updates its
``selection`` list to show what the user selected."""
from kivy.clock import Clock
from kivy.properties import (
    DictProperty,
    NumericProperty,
    ObjectProperty,
    OptionProperty,
    ListProperty,
    ReferenceListProperty,
    StringProperty
)
from kivy.resources import resource_find
from kivy.atlas import Atlas
from kivy.lang import Builder
from kivy.logger import Logger
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.togglebutton import ToggleButton
from kivy.uix.stacklayout import StackLayout


class SwatchButton(ToggleButton):
    """Toggle button containing a texture and its name, which, when
    toggled, will report the fact to the :class:`Pallet` it's in.

    """
    name = StringProperty()
    tex = ObjectProperty()

    def on_state(self, *args):
        if self.state == 'down':
            assert(self not in self.parent.selection)
            if self.parent.selection_mode == 'single':
                for wid in self.parent.selection:
                    if wid is not self:
                        wid.state = 'normal'
                self.parent.selection = [self]
            else:
                self.parent.selection.append(self)
        else:
            if self in self.parent.selection:
                self.parent.selection.remove(self)


kv = """
<SwatchButton>:
    Image:
        id: theimg
        center: root.center
        texture: root.tex
        size: self.texture_size if root.tex else (1, 1)
        size_hint: (None, None)
        pos_hint: {'x': None, 'y': None}
    Label:
        text: root.name
        size: self.texture_size
        pos_hint: {'x': None, 'y': None}
        x: root.x + 5
        y: theimg.y - self.height
"""
Builder.load_string(kv)


class Pallet(StackLayout):
    atlas = ObjectProperty()
    filename = StringProperty()
    swatches = DictProperty({})
    swatch_width = NumericProperty(100)
    swatch_height = NumericProperty(75)
    swatch_size = ReferenceListProperty(swatch_width, swatch_height)
    selection = ListProperty([])
    selection_mode = OptionProperty('single', options=['single', 'multiple'])

    def on_selection(self, *args):
        Logger.debug(
            'Pallet: {} got selection {}'.format(
                self.filename, self.selection
            )
        )

    def on_filename(self, *args):
        if not self.filename:
            return
        resource = resource_find(self.filename)
        if not resource:
            raise ValueError("Couldn't find atlas: {}".format(self.filename))
        self.atlas = Atlas(resource)

    def on_atlas(self, *args):
        if self.atlas is None:
            return
        self.upd_textures()
        self.atlas.bind(textures=self.upd_textures)

    def upd_textures(self, *args):
        if self.canvas is None:
            Clock.schedule_once(self.upd_textures, 0)
            return
        for name in list(self.swatches.keys()):
            if name not in self.atlas.textures:
                self.remove_widget(self.swatches[name])
                del self.swatches[name]
        for (name, tex) in self.atlas.textures.items():
            if name in self.swatches and self.swatches[name] != tex:
                self.remove_widget(self.swatches[name])
            if name not in self.swatches or self.swatches[name] != tex:
                self.swatches[name] = SwatchButton(
                    name=name,
                    tex=tex,
                    size_hint=(None, None),
                    size=self.swatch_size
                )
                self.add_widget(self.swatches[name])


kv = """
<Pallet>:
    orientation: 'lr-tb'
    padding_y: 100
    size_hint: (None, None)
    height: self.minimum_height
"""
Builder.load_string(kv)


class PalletBox(BoxLayout):
    pallets = ListProperty()
