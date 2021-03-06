# This file is part of LiSE, a framework for life simulation games.
# Copyright (c) Zachary Spector,  zacharyspector@gmail.com
"""A command-line-only test to make sure that travel works."""


import networkx as nx
from LiSE import Engine
from os import remove

def test_advance(lise):
    r = lise.advance()
    if r is None:
        print("Location at tick {}: {}".format(lise.tick, npc.avatar["physical"]["location"]))

def clear_off():
    for fn in ('LiSEworld.db', 'LiSEcode.db'):
        try:
            remove(fn)
        except OSError:
            pass

def mkengine(w='LiSEworld.db'):
    return Engine(
        worlddb=w,
        codedb='LiSEcode.db'
    )

clear_off()
lise = mkengine()

lise.tick = -1

phys = lise.new_character("physical")
phys.add_place("home")
phys.add_place("work")
phys.add_portal("home", "work")
phys.add_portal("work", "home")
npc = lise.new_character("nonplayer")
phys.add_thing("npc", "home")
npc.add_avatar("physical", "npc")

@npc.rule
def home2work(engine, npc):
    """Rule to schedule a new trip to work by 9 o'clock."""
    # arrange to get to work by 9 o'clock
    print("Will go to work in 9 hours")
    npc.avatar["physical"].travel_to_by(
        "work",
        engine.tick+9)

@home2work.prereq
def daystart(engine, npc):
    """Run at midnight only."""
    return engine.tick % 24 == 0

@home2work.prereq
def home_all_day(engine, npc):
    """Run if I'm scheduled to be at Home for this tick and the
    following twenty-four.

    """
    present = engine.tick
    for t in range(present, present+24):
        # The branch and tick pointers will be reset by the
        # event handler once this function returns, don't
        # worry.
        engine.tick = t
        # I really only have one avatar, but doing it this way
        # I don't need to care what its name is, and it
        # handles the case where I have more than one avatar.
        for avatar in list(npc.avatar["physical"].values()):
            if avatar["location"] != "home":
                return False
    print("Home all day.")
    return True

@npc.rule
def work2home(engine, npc):
    """Rule to go home when work's over, at 5 o'clock."""
    # Leave, go home, arrive whenever
    print("Leaving work now.")
    npc.avatar["physical"].travel_to("home")

@work2home.prereq
def closing_time(engine, npc):
    """Run at 5pm only."""
    return engine.tick % 24 == 17

@work2home.prereq
def at_work(engine, npc):
    """Run only when I'm at Work."""
    return npc.avatar["physical"]["location"] == "work"

while lise.tick < 76:
    test_advance(lise)

lise.close()

lise = Engine(
    worlddb='LiSEworld.db',
    codedb='LiSEcode.db'
)
npc = lise.character["nonplayer"]

while lise.tick < 119:
    test_advance(lise)

phys = lise.character["physical"]

phys.add_place("downtown")
# when symmetrical, automagically create Downtown->Home portal with same attributes
phys.add_portal("home", "downtown", symmetrical=True, km=2)
assert(phys.portal["downtown"]["home"]["km"] == 2)
phys.portal["downtown"]["home"]["fun"] = 100
assert(phys.portal["home"]["downtown"]["fun"] == 100)
npc.avatar["physical"].travel_to("downtown", weight="km")

while lise.tick < 142:
    test_advance(lise)

lise.close()
clear_off()
# More complex test

