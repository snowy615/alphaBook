"""The world layer's rules: budget, map, building, movement, combat, scoring."""
import pytest

from app import world as W
from app.world import World, WorldRejected


@pytest.fixture
def w():
    """A deterministic two-player world. Seed fixed so combat is assertable."""
    world = World(seed=7, players=2)
    world.add_player("p1", "Ada")
    world.add_player("p2", "Bo")
    return world


def rich(world, uid, credits=100_000.0, materials=5_000.0, food=5_000.0, workers=40):
    """Take money out of the equation when testing a rule that isn't about money."""
    e = world.empires[uid]
    e.pnl_credits = credits
    e.materials = materials
    e.food = food
    e.workers = workers
    return e


def own(world, uid, x, y):
    world.grid[y][x].owner = uid
    return world.grid[y][x]


def land_near(world, uid, terrain, exclude_built=True):
    """A tile the player owns with the given terrain, ready to build on."""
    for t in world.owned_tiles(uid):
        if t.terrain == terrain and (not exclude_built or not t.building):
            return t
    # Claim one so the test can proceed regardless of map generation.
    for row in world.grid:
        for t in row:
            if t.terrain == terrain and t.owner is None:
                t.owner = uid
                return t
    raise AssertionError(f"no {terrain} anywhere on the map")


# ── Map generation ──────────────────────────────────────────────────────────
class TestMap:
    def test_the_same_seed_builds_the_same_map(self):
        a = World(seed=42, players=4)
        b = World(seed=42, players=4)
        assert [t.terrain for row in a.grid for t in row] == \
               [t.terrain for row in b.grid for t in row]

    def test_a_different_seed_builds_a_different_map(self):
        a = World(seed=1, players=4)
        b = World(seed=2, players=4)
        assert [t.terrain for row in a.grid for t in row] != \
               [t.terrain for row in b.grid for t in row]

    def test_the_map_grows_with_the_player_count(self):
        assert World(seed=1, players=2).side < World(seed=1, players=6).side

    def test_the_map_never_exceeds_the_cap(self):
        assert World(seed=1, players=50).side == W.MAX_SIDE

    def test_every_terrain_is_a_known_kind(self):
        world = World(seed=3, players=4)
        assert {t.terrain for row in world.grid for t in row} <= set(W.TERRAIN)

    def test_there_is_land_to_play_on(self):
        world = World(seed=3, players=4)
        land = sum(1 for row in world.grid for t in row if t.passable)
        assert land > 0.6 * world.side ** 2, "the map should not be mostly water"


# ── Spawning ────────────────────────────────────────────────────────────────
class TestSpawn:
    def test_joining_places_a_base(self, w):
        hx, hy = w.empires["p1"].home
        tile = w.tile(hx, hy)
        assert tile.building == "base" and tile.owner == "p1"

    def test_a_base_stands_on_land(self, w):
        for uid in ("p1", "p2"):
            assert w.tile(*w.empires[uid].home).passable

    def test_bases_are_not_on_top_of_each_other(self, w):
        assert W.distance(w.empires["p1"].home, w.empires["p2"].home) >= W.SPAWN_CLEARANCE

    def test_joining_claims_the_ground_around_the_base(self, w):
        assert len(w.owned_tiles("p1")) > 1

    def test_a_player_opens_with_an_explorer(self, w):
        mine = [u for u in w.units.values() if u.owner == "p1"]
        assert len(mine) == 1 and mine[0].kind == "explorer"

    def test_joining_twice_does_not_duplicate(self, w):
        before = len(w.owned_tiles("p1"))
        w.add_player("p1", "Ada")
        assert len(w.owned_tiles("p1")) == before
        assert len(w.empires) == 2

    def test_players_get_distinct_colours(self, w):
        assert w.empires["p1"].colour != w.empires["p2"].colour


# ── The budget: trading P&L is the money ────────────────────────────────────
class TestBudget:
    def test_a_player_starts_with_the_grant(self, w):
        assert w.empires["p1"].credits == W.START_GRANT

    def test_trading_profit_becomes_credits(self, w):
        w.set_pnl("p1", 5_000.0)
        assert w.empires["p1"].credits == W.START_GRANT + 5_000.0

    def test_trading_losses_shrink_the_budget(self, w):
        w.set_pnl("p1", -1_500.0)
        assert w.empires["p1"].credits == W.START_GRANT - 1_500.0

    def test_a_deep_loss_floors_at_zero_rather_than_going_negative(self, w):
        w.set_pnl("p1", -999_999.0)
        assert w.empires["p1"].credits == 0.0

    def test_spending_reduces_the_budget(self, w):
        rich(w, "p1")
        before = w.empires["p1"].credits
        tile = land_near(w, "p1", "plain")
        w.act("p1", {"type": "build", "building": "farm", "x": tile.x, "y": tile.y})
        assert w.empires["p1"].credits == before - W.BUILDINGS["farm"]["cost"]["credits"]

    def test_a_pnl_collapse_does_not_demolish_what_is_already_built(self, w):
        rich(w, "p1")
        tile = land_near(w, "p1", "plain")
        w.act("p1", {"type": "build", "building": "farm", "x": tile.x, "y": tile.y})
        w.set_pnl("p1", -50_000.0)
        assert w.tile(tile.x, tile.y).building == "farm"
        assert w.empires["p1"].credits == 0.0

    def test_you_cannot_build_what_you_cannot_afford(self, w):
        rich(w, "p1", credits=-W.START_GRANT + 50)   # 50 credits to your name
        tile = land_near(w, "p1", "plain")
        with pytest.raises(WorldRejected, match="credits"):
            w.act("p1", {"type": "build", "building": "factory", "x": tile.x, "y": tile.y})

    def test_the_shortfall_message_says_where_credits_come_from(self, w):
        rich(w, "p1", credits=-W.START_GRANT + 50)
        tile = land_near(w, "p1", "plain")
        with pytest.raises(WorldRejected, match="trading"):
            w.act("p1", {"type": "build", "building": "factory", "x": tile.x, "y": tile.y})


# ── Building ────────────────────────────────────────────────────────────────
class TestBuild:
    def test_building_on_your_own_ground_works(self, w):
        rich(w, "p1")
        tile = land_near(w, "p1", "plain")
        res = w.act("p1", {"type": "build", "building": "farm", "x": tile.x, "y": tile.y})
        assert res["ok"] and w.tile(tile.x, tile.y).building == "farm"

    def test_you_cannot_build_on_someone_elses_ground(self, w):
        rich(w, "p1")
        t = land_near(w, "p2", "plain")
        with pytest.raises(WorldRejected, match="not yours"):
            w.act("p1", {"type": "build", "building": "farm", "x": t.x, "y": t.y})

    def test_a_farm_needs_plains(self, w):
        rich(w, "p1")
        t = land_near(w, "p1", "hills")
        with pytest.raises(WorldRejected, match="plain"):
            w.act("p1", {"type": "build", "building": "farm", "x": t.x, "y": t.y})

    def test_a_mine_needs_hills(self, w):
        rich(w, "p1")
        t = land_near(w, "p1", "hills")
        assert w.act("p1", {"type": "build", "building": "mine", "x": t.x, "y": t.y})["ok"]

    def test_one_building_per_tile(self, w):
        rich(w, "p1")
        t = land_near(w, "p1", "plain")
        w.act("p1", {"type": "build", "building": "farm", "x": t.x, "y": t.y})
        with pytest.raises(WorldRejected, match="already has"):
            w.act("p1", {"type": "build", "building": "house", "x": t.x, "y": t.y})

    def test_a_market_claims_the_ring_around_it(self, w):
        rich(w, "p1")
        # Somewhere with unowned neighbours, so the claim has room to work.
        t = None
        for row in w.grid:
            for cand in row:
                if cand.terrain == "plain" and cand.owner is None and \
                        any(w.grid[ny][nx].owner is None
                            for nx, ny in W.neighbours(cand.x, cand.y)
                            if w.in_bounds(nx, ny)):
                    t = cand
                    break
            if t:
                break
        t.owner = "p1"
        res = w.act("p1", {"type": "build", "building": "market", "x": t.x, "y": t.y})
        assert res["claimed"] > 0

    def test_an_unknown_building_is_refused_with_the_options(self, w):
        rich(w, "p1")
        t = land_near(w, "p1", "plain")
        with pytest.raises(WorldRejected, match="factory"):
            w.act("p1", {"type": "build", "building": "castle", "x": t.x, "y": t.y})

    def test_you_cannot_build_off_the_map(self, w):
        rich(w, "p1")
        with pytest.raises(WorldRejected, match="off the map"):
            w.act("p1", {"type": "build", "building": "farm", "x": 999, "y": 999})

    def test_demolishing_returns_half_the_materials(self, w):
        rich(w, "p1", materials=1000)
        t = land_near(w, "p1", "plain")
        w.act("p1", {"type": "build", "building": "farm", "x": t.x, "y": t.y})
        before = w.empires["p1"].materials
        w.act("p1", {"type": "demolish", "x": t.x, "y": t.y})
        assert w.empires["p1"].materials == before + W.BUILDINGS["farm"]["cost"]["materials"] * 0.5
        assert w.tile(t.x, t.y).building is None

    def test_you_cannot_demolish_your_own_base(self, w):
        hx, hy = w.empires["p1"].home
        with pytest.raises(WorldRejected, match="base"):
            w.act("p1", {"type": "demolish", "x": hx, "y": hy})


# ── Production ──────────────────────────────────────────────────────────────
class TestProduction:
    def test_a_farm_produces_food(self, w):
        rich(w, "p1", food=10)
        t = land_near(w, "p1", "plain")
        w.act("p1", {"type": "build", "building": "farm", "x": t.x, "y": t.y})
        before = w.empires["p1"].food
        w.tick()
        assert w.empires["p1"].food > before - w.empires["p1"].workers

    def test_a_mine_produces_materials(self, w):
        rich(w, "p1", materials=W.BUILDINGS["mine"]["cost"]["materials"])
        t = land_near(w, "p1", "hills")
        w.act("p1", {"type": "build", "building": "mine", "x": t.x, "y": t.y})
        assert w.empires["p1"].materials == 0
        w.tick()
        assert w.empires["p1"].materials == W.BUILDINGS["mine"]["yield"]["materials"]

    def test_a_factory_pays_credits(self, w):
        rich(w, "p1")
        t = land_near(w, "p1", "plain")
        w.act("p1", {"type": "build", "building": "factory", "x": t.x, "y": t.y})
        before = w.empires["p1"].credits
        w.tick()
        gained = W.BUILDINGS["factory"]["yield"]["credits"]
        assert w.empires["p1"].credits == before + gained

    def test_a_factory_burns_materials(self, w):
        rich(w, "p1", materials=200)
        t = land_near(w, "p1", "plain")
        w.act("p1", {"type": "build", "building": "factory", "x": t.x, "y": t.y})
        before = w.empires["p1"].materials
        w.tick()
        assert w.empires["p1"].materials == before - W.BUILDINGS["factory"]["upkeep"]["materials"]

    def test_a_starved_factory_pays_nothing(self, w):
        rich(w, "p1")
        t = land_near(w, "p1", "plain")
        w.act("p1", {"type": "build", "building": "factory", "x": t.x, "y": t.y})
        w.empires["p1"].materials = 0.0
        before = w.empires["p1"].credits
        w.tick()
        assert w.empires["p1"].credits == before, "an unfed factory must not pay out"

    def test_buildings_beyond_the_workforce_sit_idle(self, w):
        """Two lumber camps, one worker: only one camp can be manned."""
        rich(w, "p1", workers=1, materials=0)
        forests = [t for t in w.owned_tiles("p1") if t.terrain == "forest"][:2]
        while len(forests) < 2:
            forests.append(land_near(w, "p1", "forest"))
        for t in forests:
            w.act("p1", {"type": "build", "building": "lumber", "x": t.x, "y": t.y})
        w.tick()
        assert w.empires["p1"].materials == W.BUILDINGS["lumber"]["yield"]["materials"]

    def test_the_population_grows_on_a_food_surplus(self, w):
        rich(w, "p1", workers=1, food=500)
        w.tick()
        assert w.empires["p1"].workers == 2

    def test_the_population_stops_at_the_cap(self, w):
        rich(w, "p1", food=5000, workers=W.BASE_POP_CAP)
        w.tick()
        assert w.empires["p1"].workers == W.BASE_POP_CAP

    def test_housing_raises_the_cap(self, w):
        rich(w, "p1")
        before = w.pop_cap("p1")
        t = land_near(w, "p1", "plain")
        w.act("p1", {"type": "build", "building": "house", "x": t.x, "y": t.y})
        assert w.pop_cap("p1") == before + W.BUILDINGS["house"]["pop_cap"]

    def test_workers_leave_when_the_food_runs_out(self, w):
        rich(w, "p1", food=0, workers=5)
        w.tick()
        assert w.empires["p1"].workers == 4

    def test_starvation_never_empties_the_last_worker(self, w):
        rich(w, "p1", food=0, workers=1)
        for _ in range(5):
            w.tick()
        assert w.empires["p1"].workers == 1

    def test_a_tick_advances_the_counter(self, w):
        w.tick()
        w.tick()
        assert w.tick_no == 2


# ── Units and movement ──────────────────────────────────────────────────────
class TestUnits:
    def test_training_an_explorer_costs_credits(self, w):
        rich(w, "p1")
        before = w.empires["p1"].credits
        w.act("p1", {"type": "train", "unit": "explorer"})
        assert w.empires["p1"].credits == before - W.UNITS["explorer"]["cost"]["credits"]

    def test_a_new_unit_appears_at_the_base(self, w):
        rich(w, "p1")
        res = w.act("p1", {"type": "train", "unit": "explorer"})
        u = w.units[res["units"][0]]
        assert (u.x, u.y) == w.empires["p1"].home

    def test_soldiers_need_a_barracks(self, w):
        rich(w, "p1")
        with pytest.raises(WorldRejected, match="[Bb]arracks"):
            w.act("p1", {"type": "train", "unit": "soldier"})

    def test_a_barracks_unlocks_soldiers(self, w):
        rich(w, "p1")
        t = land_near(w, "p1", "plain")
        w.act("p1", {"type": "build", "building": "barracks", "x": t.x, "y": t.y})
        assert w.act("p1", {"type": "train", "unit": "soldier"})["count"] == 1

    def test_training_stops_when_the_workforce_runs_out(self, w):
        rich(w, "p1", workers=2)
        t = land_near(w, "p1", "plain")
        w.act("p1", {"type": "build", "building": "barracks", "x": t.x, "y": t.y})
        res = w.act("p1", {"type": "train", "unit": "soldier", "count": 10})
        assert res["count"] < 10, "a soldier needs a worker, and workers are finite"

    def test_a_batch_is_capped(self, w):
        rich(w, "p1")
        assert w.act("p1", {"type": "train", "unit": "explorer", "count": 999})["count"] <= 10

    def test_a_unit_moves(self, w):
        u = next(u for u in w.units.values() if u.owner == "p1")
        target = self._reachable(w, u)
        w.act("p1", {"type": "move", "unit_id": u.uid, "x": target[0], "y": target[1]})
        assert (u.x, u.y) == target

    def test_moving_spends_movement(self, w):
        u = next(u for u in w.units.values() if u.owner == "p1")
        target = self._reachable(w, u)
        w.act("p1", {"type": "move", "unit_id": u.uid, "x": target[0], "y": target[1]})
        assert u.moves_left < W.UNITS["explorer"]["speed"]

    def test_a_unit_cannot_walk_into_water(self, w):
        u = next(u for u in w.units.values() if u.owner == "p1")
        water = next(((t.x, t.y) for row in w.grid for t in row
                      if t.terrain == "water"), None)
        if water is None:
            pytest.skip("this map has no water")
        with pytest.raises(WorldRejected, match="water"):
            w.act("p1", {"type": "move", "unit_id": u.uid, "x": water[0], "y": water[1]})

    def test_a_unit_cannot_teleport_across_the_map(self, w):
        u = next(u for u in w.units.values() if u.owner == "p1")
        far = ((u.x + w.side // 2) % w.side, (u.y + w.side // 2) % w.side)
        with pytest.raises(WorldRejected, match="reach"):
            w.act("p1", {"type": "move", "unit_id": u.uid, "x": far[0], "y": far[1]})

    def test_movement_refreshes_on_the_next_tick(self, w):
        u = next(u for u in w.units.values() if u.owner == "p1")
        u.moves_left = 0.0
        w.tick()
        assert u.moves_left == W.UNITS["explorer"]["speed"]

    def test_an_explorer_claims_the_ground_it_walks_over(self, w):
        u = next(u for u in w.units.values() if u.owner == "p1")
        # Strip ownership so every step is a fresh claim.
        for t in w.owned_tiles("p1"):
            if (t.x, t.y) != w.empires["p1"].home:
                t.owner = None
        target = self._reachable(w, u)
        res = w.act("p1", {"type": "move", "unit_id": u.uid, "x": target[0], "y": target[1]})
        assert res["claimed"] > 0

    def test_claiming_never_takes_a_rival_tile(self, w):
        u = next(u for u in w.units.values() if u.owner == "p1")
        for nx, ny in W.neighbours(u.x, u.y):
            if w.in_bounds(nx, ny) and w.grid[ny][nx].passable:
                w.grid[ny][nx].owner = "p2"
                w.act("p1", {"type": "move", "unit_id": u.uid, "x": nx, "y": ny})
                assert w.grid[ny][nx].owner == "p2"
                return
        pytest.skip("nowhere adjacent to test")

    def test_you_cannot_move_someone_elses_unit(self, w):
        u = next(u for u in w.units.values() if u.owner == "p2")
        with pytest.raises(WorldRejected, match="not yours"):
            w.act("p1", {"type": "move", "unit_id": u.uid, "x": u.x, "y": u.y})

    def test_an_unknown_unit_id_is_refused(self, w):
        with pytest.raises(WorldRejected, match="No unit"):
            w.act("p1", {"type": "move", "unit_id": "nope", "x": 1, "y": 1})

    @staticmethod
    def _reachable(world, unit):
        """An adjacent passable tile the unit can definitely step onto."""
        for nx, ny in W.neighbours(unit.x, unit.y):
            if world.in_bounds(nx, ny) and world.grid[ny][nx].passable:
                return nx, ny
        raise AssertionError("the unit is walled in")


# ── Settlers ────────────────────────────────────────────────────────────────
class TestSettlers:
    def test_a_settler_founds_an_outpost_and_claims_ground(self, w):
        rich(w, "p1")
        res = w.act("p1", {"type": "train", "unit": "settler"})
        uid = res["units"][0]
        before = len(w.owned_tiles("p1"))
        out = w.act("p1", {"type": "found", "unit_id": uid})
        assert out["ok"] and len(w.owned_tiles("p1")) >= before

    def test_founding_consumes_the_settler(self, w):
        rich(w, "p1")
        uid = w.act("p1", {"type": "train", "unit": "settler"})["units"][0]
        w.act("p1", {"type": "found", "unit_id": uid})
        assert uid not in w.units

    def test_only_a_settler_can_found(self, w):
        u = next(u for u in w.units.values() if u.owner == "p1")
        with pytest.raises(WorldRejected, match="settler"):
            w.act("p1", {"type": "found", "unit_id": u.uid})


# ── Combat ──────────────────────────────────────────────────────────────────
class TestCombat:
    def setup_fight(self, world):
        """Put a p1 soldier next to a p2 soldier on known ground."""
        x, y = world.side // 2, world.side // 2
        for tx, ty in ((x, y), (x + 1, y)):
            world.grid[ty][tx].terrain = "plain"
        a = world._spawn_unit("p1", "soldier", x, y)
        d = world._spawn_unit("p2", "soldier", x + 1, y)
        return a, d

    def test_an_attack_damages_the_defender(self, w):
        a, d = self.setup_fight(w)
        before = d.hp
        w.act("p1", {"type": "attack", "unit_id": a.uid, "x": d.x, "y": d.y})
        assert d.hp < before

    def test_the_defender_hits_back(self, w):
        a, d = self.setup_fight(w)
        before = a.hp
        w.act("p1", {"type": "attack", "unit_id": a.uid, "x": d.x, "y": d.y})
        assert a.hp < before, "attacking must cost something"

    def test_an_attack_uses_up_the_turn(self, w):
        a, d = self.setup_fight(w)
        w.act("p1", {"type": "attack", "unit_id": a.uid, "x": d.x, "y": d.y})
        assert a.moves_left == 0
        with pytest.raises(WorldRejected, match="already acted"):
            w.act("p1", {"type": "attack", "unit_id": a.uid, "x": d.x, "y": d.y})

    def test_you_cannot_attack_at_range(self, w):
        a, d = self.setup_fight(w)
        d.x += 4
        with pytest.raises(WorldRejected, match="move next to"):
            w.act("p1", {"type": "attack", "unit_id": a.uid, "x": d.x, "y": d.y})

    def test_a_settler_cannot_attack(self, w):
        _, d = self.setup_fight(w)
        s = w._spawn_unit("p1", "settler", d.x - 1, d.y)
        with pytest.raises(WorldRejected, match="cannot attack"):
            w.act("p1", {"type": "attack", "unit_id": s.uid, "x": d.x, "y": d.y})

    def test_attacking_empty_ground_is_refused(self, w):
        a, _ = self.setup_fight(w)
        empty = (a.x, a.y - 1)
        w.grid[empty[1]][empty[0]].terrain = "plain"
        w.grid[empty[1]][empty[0]].owner = None
        with pytest.raises(WorldRejected, match="nothing"):
            w.act("p1", {"type": "attack", "unit_id": a.uid, "x": empty[0], "y": empty[1]})

    def test_a_killed_defender_is_removed(self, w):
        a, d = self.setup_fight(w)
        d.hp = 1.0
        w.act("p1", {"type": "attack", "unit_id": a.uid, "x": d.x, "y": d.y})
        assert d.uid not in w.units

    def test_the_winner_takes_the_empty_ground(self, w):
        a, d = self.setup_fight(w)
        d.hp = 1.0
        tx, ty = d.x, d.y
        w.grid[ty][tx].owner = "p2"
        res = w.act("p1", {"type": "attack", "unit_id": a.uid, "x": tx, "y": ty})
        assert res["captured"] and w.grid[ty][tx].owner == "p1"

    def test_hills_protect_the_defender(self):
        """The same blow lands softer on high ground."""
        def damage(terrain):
            world = World(seed=11, players=2)
            world.add_player("p1", "Ada")
            world.add_player("p2", "Bo")
            x, y = world.side // 2, world.side // 2
            world.grid[y][x].terrain = "plain"
            world.grid[y][x + 1].terrain = terrain
            a = world._spawn_unit("p1", "soldier", x, y)
            d = world._spawn_unit("p2", "soldier", x + 1, y)
            world.rng.seed(99)          # same jitter both times
            return world.act("p1", {"type": "attack", "unit_id": a.uid,
                                    "x": d.x, "y": d.y})["damage_dealt"]

        assert damage("hills") < damage("plain")

    def test_a_siege_damages_a_building(self, w):
        rich(w, "p2")
        t = land_near(w, "p2", "plain")
        w.act("p2", {"type": "build", "building": "farm", "x": t.x, "y": t.y})
        a = w._spawn_unit("p1", "cannon", t.x - 1, t.y)
        w.grid[t.y][t.x - 1].terrain = "plain"
        before = w.tile(t.x, t.y).hp
        w.act("p1", {"type": "attack", "unit_id": a.uid, "x": t.x, "y": t.y})
        assert w.tile(t.x, t.y).hp < before

    def test_a_razed_building_hands_over_the_tile(self, w):
        rich(w, "p2")
        t = land_near(w, "p2", "plain")
        w.act("p2", {"type": "build", "building": "farm", "x": t.x, "y": t.y})
        w.grid[t.y][t.x].hp = 1.0
        w.grid[t.y][t.x - 1].terrain = "plain"
        a = w._spawn_unit("p1", "cannon", t.x - 1, t.y)
        res = w.act("p1", {"type": "attack", "unit_id": a.uid, "x": t.x, "y": t.y})
        assert res["razed"] and w.tile(t.x, t.y).owner == "p1"

    def test_a_cannon_out_sieges_a_soldier(self, w):
        """A fort, so neither blow razes it and both are measured on the same wall."""
        rich(w, "p2")
        t = land_near(w, "p2", "plain")
        w.act("p2", {"type": "build", "building": "fort", "x": t.x, "y": t.y})
        w.grid[t.y][t.x - 1].terrain = "plain"

        def hit(kind):
            w.grid[t.y][t.x].hp = float(W.BUILDINGS["fort"]["hp"])
            u = w._spawn_unit("p1", kind, t.x - 1, t.y)
            w.rng.seed(5)
            return w.act("p1", {"type": "attack", "unit_id": u.uid,
                                "x": t.x, "y": t.y})["damage_dealt"]

        assert hit("cannon") > hit("soldier")

    def test_a_fort_absorbs_more_than_a_farm(self, w):
        rich(w, "p2")
        results = {}
        for kind in ("farm", "fort"):
            t = next(x for x in w.owned_tiles("p2")
                     if x.terrain == "plain" and not x.building)
            w.act("p2", {"type": "build", "building": kind, "x": t.x, "y": t.y})
            w.grid[t.y][t.x - 1].terrain = "plain"
            u = w._spawn_unit("p1", "cannon", t.x - 1, t.y)
            w.rng.seed(5)
            results[kind] = w.act("p1", {"type": "attack", "unit_id": u.uid,
                                         "x": t.x, "y": t.y})["damage_dealt"]
        assert results["fort"] < results["farm"]


# ── Elimination ─────────────────────────────────────────────────────────────
class TestElimination:
    def raze_base(self, world, victim="p2", raider="p1"):
        """Clear the garrison, then bring the base down to its last hit point.

        Units on a tile have to be beaten before the building underneath can be
        sieged, so the defenders are moved off rather than ignored.
        """
        hx, hy = world.empires[victim].home
        for u in list(world.units.values()):
            if u.owner == victim and (u.x, u.y) == (hx, hy):
                world.units.pop(u.uid, None)
        world.grid[hy][hx].hp = 1.0
        for nx, ny in W.neighbours(hx, hy):
            if world.in_bounds(nx, ny):
                world.grid[ny][nx].terrain = "plain"
                u = world._spawn_unit(raider, "cannon", nx, ny)
                return world.act(raider, {"type": "attack", "unit_id": u.uid,
                                          "x": hx, "y": hy})
        raise AssertionError("the base has no approach")

    def test_razing_a_base_eliminates_the_player(self, w):
        res = self.raze_base(w)
        assert res["eliminated"] and not w.empires["p2"].alive

    def test_the_conqueror_inherits_the_territory(self, w):
        self.raze_base(w)
        assert w.owned_tiles("p2") == []
        assert len(w.owned_tiles("p1")) > 1

    def test_an_eliminated_player_loses_their_units(self, w):
        hx, hy = w.empires["p2"].home
        w._spawn_unit("p2", "soldier", hx, hy - 2)     # away from the base tile
        self.raze_base(w)
        assert [u for u in w.units.values() if u.owner == "p2"] == []

    def test_a_garrison_has_to_be_beaten_before_the_base_can_be_sieged(self, w):
        """A unit standing on a building shields it — you fight it first."""
        hx, hy = w.empires["p2"].home
        w.grid[hy][hx].hp = 1.0
        w._spawn_unit("p2", "soldier", hx, hy)
        w.grid[hy][hx - 1].terrain = "plain"
        u = w._spawn_unit("p1", "cannon", hx - 1, hy)
        res = w.act("p1", {"type": "attack", "unit_id": u.uid, "x": hx, "y": hy})
        assert res["result"] == "battle"
        assert w.tile(hx, hy).building == "base" and w.empires["p2"].alive

    def test_an_eliminated_player_cannot_act(self, w):
        self.raze_base(w)
        with pytest.raises(WorldRejected, match="fallen"):
            w.act("p2", {"type": "train", "unit": "explorer"})

    def test_elimination_is_announced(self, w):
        self.raze_base(w)
        assert any(e["kind"] == "eliminated" for e in w.events)


# ── The resource exchange ───────────────────────────────────────────────────
class TestExchange:
    def test_buying_materials_costs_credits(self, w):
        rich(w, "p1", materials=0)
        before = w.empires["p1"].credits
        w.act("p1", {"type": "trade", "side": "buy", "resource": "materials", "qty": 10})
        assert w.empires["p1"].materials == 10
        assert w.empires["p1"].credits == before - 10 * W.EXCHANGE["materials"]["buy"]

    def test_selling_materials_pays_credits(self, w):
        rich(w, "p1", materials=50)
        before = w.empires["p1"].credits
        w.act("p1", {"type": "trade", "side": "sell", "resource": "materials", "qty": 10})
        assert w.empires["p1"].materials == 40
        assert w.empires["p1"].credits == before + 10 * W.EXCHANGE["materials"]["sell"]

    def test_the_spread_makes_round_tripping_a_loss(self, w):
        rich(w, "p1", materials=0)
        before = w.empires["p1"].credits
        w.act("p1", {"type": "trade", "side": "buy", "resource": "materials", "qty": 10})
        w.act("p1", {"type": "trade", "side": "sell", "resource": "materials", "qty": 10})
        assert w.empires["p1"].credits < before

    def test_you_cannot_sell_what_you_do_not_have(self, w):
        w.empires["p1"].materials = 3
        with pytest.raises(WorldRejected, match="only have"):
            w.act("p1", {"type": "trade", "side": "sell", "resource": "materials", "qty": 99})

    def test_a_nonsense_quantity_is_refused(self, w):
        with pytest.raises(WorldRejected, match="positive"):
            w.act("p1", {"type": "trade", "side": "buy", "resource": "food", "qty": -5})

    def test_only_listed_resources_trade(self, w):
        with pytest.raises(WorldRejected, match="materials"):
            w.act("p1", {"type": "trade", "side": "buy", "resource": "gold", "qty": 1})


# ── Scoring ─────────────────────────────────────────────────────────────────
class TestDevelopment:
    def test_land_counts(self, w):
        before = w.development("p1")["score"]
        for row in w.grid:
            for t in row:
                if t.owner is None and t.passable:
                    t.owner = "p1"
                    break
            break
        assert w.development("p1")["score"] > before

    def test_a_factory_scores_more_than_a_farm(self, w):
        assert W.BUILDINGS["factory"]["value"] > W.BUILDINGS["farm"]["value"]

    def test_workers_count(self, w):
        before = w.development("p1")["score"]
        w.empires["p1"].workers += 3
        assert w.development("p1")["score"] == before + 3 * W.WORKER_POINTS

    def test_the_parts_add_up_to_the_score(self, w):
        rich(w, "p1")
        t = land_near(w, "p1", "plain")
        w.act("p1", {"type": "build", "building": "factory", "x": t.x, "y": t.y})
        d = w.development("p1")
        assert d["score"] == pytest.approx(
            d["land"] + d["structures"] + d["people"] + d["army"])

    def test_standings_rank_by_development(self, w):
        rich(w, "p1")
        for _ in range(3):
            t = next(x for x in w.owned_tiles("p1")
                     if x.terrain == "plain" and not x.building)
            w.act("p1", {"type": "build", "building": "factory", "x": t.x, "y": t.y})
        rows = w.standings()
        assert rows[0]["user_id"] == "p1" and rows[0]["rank"] == 1

    def test_a_dead_empire_ranks_below_a_live_one(self, w):
        rich(w, "p1")
        TestElimination().raze_base(w)
        rows = w.standings()
        assert rows[-1]["user_id"] == "p2" and rows[-1]["alive"] is False

    def test_factories_are_counted_out_for_the_dashboard(self, w):
        rich(w, "p1")
        t = land_near(w, "p1", "plain")
        w.act("p1", {"type": "build", "building": "factory", "x": t.x, "y": t.y})
        assert w.development("p1")["factories"] == 1


# ── Views ───────────────────────────────────────────────────────────────────
class TestViews:
    def test_the_map_view_covers_every_tile(self, w):
        v = w.map_view()
        assert len(v["terrain"]) == w.side ** 2 == len(v["owners"])

    def test_the_map_view_lists_the_bases(self, w):
        kinds = [b["b"] for b in w.map_view()["buildings"]]
        assert kinds.count("base") == 2

    def test_the_map_view_names_the_players(self, w):
        assert {p["name"] for p in w.map_view()["players"]} == {"Ada", "Bo"}

    def test_owner_zero_means_unclaimed(self, w):
        v = w.map_view()
        assert 0 in v["owners"], "an opening map must have neutral ground"

    def test_a_player_view_reports_their_own_resources(self, w):
        v = w.player_view("p1")
        assert v["joined"] and v["credits"] == W.START_GRANT

    def test_a_player_view_reports_spare_workers(self, w):
        v = w.player_view("p1")
        assert v["workers_free"] == v["workers"] - w.workers_used("p1")

    def test_a_stranger_gets_a_not_joined_view(self, w):
        assert w.player_view("nobody") == {"joined": False}

    def test_the_catalogue_documents_every_building_and_unit(self):
        c = W.catalogue()
        assert set(c["buildings"]) == set(W.BUILDINGS)
        assert set(c["units"]) == set(W.UNITS)

    def test_the_catalogue_explains_the_exchange_rate(self):
        assert W.catalogue()["economy"]["credits_per_dollar"] == W.CREDITS_PER_DOLLAR


# ── Guard rails ─────────────────────────────────────────────────────────────
class TestRejections:
    def test_a_player_who_never_joined_cannot_act(self, w):
        with pytest.raises(WorldRejected, match="no base"):
            w.act("ghost", {"type": "train", "unit": "explorer"})

    def test_an_unknown_action_lists_the_real_ones(self, w):
        with pytest.raises(WorldRejected, match="build"):
            w.act("p1", {"type": "conquer"})

    def test_a_missing_coordinate_is_refused(self, w):
        with pytest.raises(WorldRejected, match="integer x and y"):
            w.act("p1", {"type": "build", "building": "farm"})

    def test_the_map_can_fill_up(self):
        world = World(seed=5, players=2)
        for i in range(len(W.COLOURS)):
            world.add_player(f"p{i}", f"P{i}")
        with pytest.raises(WorldRejected, match="full"):
            world.add_player("extra", "Extra")


# ── The link back to the market ─────────────────────────────────────────────
class TestMarketLink:
    """The one rule that ties the two halves together: P&L is the budget."""

    def run(self):
        from app import algo_engine as engine
        r = engine.Run("r1", "CODE01", "Test", "creator", seed=13)
        r.join("p1", "Ada")
        r.join("p2", "Bo")
        return r

    def test_joining_the_market_places_a_base(self):
        r = self.run()
        assert r.world.empires["p1"].alive
        assert r.world.tile(*r.world.empires["p1"].home).building == "base"

    def test_bots_do_not_get_an_empire(self):
        r = self.run()
        assert all(not p.is_bot for p in
                   (r.participants[uid] for uid in r.world.empires))

    def test_trading_profit_reaches_the_empire(self):
        r = self.run()
        r.start()
        r.participants["p1"].cash = 4_000.0
        r.advance(now=r._t0 + 1.0)
        assert r.world.empires["p1"].credits == W.START_GRANT + 4_000.0

    def test_trading_losses_reach_the_empire(self):
        r = self.run()
        r.start()
        r.participants["p1"].cash = -500.0
        r.advance(now=r._t0 + 1.0)
        assert r.world.empires["p1"].credits == W.START_GRANT - 500.0

    def test_the_world_ticks_on_the_market_clock(self):
        r = self.run()
        r.start()
        r.advance(now=r._t0 + 3 * W.WORLD_TICK_SECONDS)
        assert r.world.tick_no == 3

    def test_the_world_does_not_tick_before_the_bell(self):
        r = self.run()
        r.advance()
        assert r.world.tick_no == 0

    def test_finishing_freezes_the_world_standings(self):
        r = self.run()
        r.start()
        r.finish()
        assert len(r.world_results) == 2
        assert r.world_results[0]["rank"] == 1

    def test_the_same_seed_replays_the_same_map(self):
        from app import algo_engine as engine
        a = engine.Run("a", "A00001", "t", "c", seed=99)
        b = engine.Run("b", "B00001", "t", "c", seed=99)
        assert [t.terrain for row in a.world.grid for t in row] == \
               [t.terrain for row in b.world.grid for t in row]


# ── The shipped starter strategy ────────────────────────────────────────────
class TestStarterEmpire:
    """The world half of client/algo_client.py, played against a real world.

    The client is what every player starts from, so a change to the engine that
    breaks its opening is a change that breaks everyone's first run.
    """

    def client(self):
        import importlib.util
        import pathlib
        path = pathlib.Path(__file__).resolve().parent.parent / "client" / "algo_client.py"
        spec = importlib.util.spec_from_file_location("algo_client_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def play(self, ticks=30, pnl=6_000.0):
        """Run the starter's on_world_tick against a live world for a while."""
        client = self.client()
        world = World(seed=21, players=2)
        world.add_player("p1", "Ada")
        world.add_player("p2", "Bo")
        world.set_pnl("p1", pnl)

        memory, errors = {}, []
        for _ in range(ticks):
            snapshot = {"world_tick": world.tick_no, "map": world.map_view(),
                        "me": world.player_view("p1"), "standings": world.standings()}
            ctx = client.build_world_ctx(snapshot, memory)
            for action in client.on_world_tick(ctx) or []:
                try:
                    world.act("p1", action)
                except WorldRejected as exc:
                    errors.append(str(exc))
            world.tick()
            world.set_pnl("p1", pnl)
        return world, errors

    def test_it_never_raises(self):
        self.play()

    def test_it_claims_ground(self):
        world, _ = self.play()
        assert len(world.owned_tiles("p1")) > len(world.owned_tiles("p2"))

    def test_it_gets_an_economy_up(self):
        world, _ = self.play()
        kinds = {t.building for t in world.buildings_of("p1")}
        assert "farm" in kinds and len(kinds) >= 3

    def test_it_out_develops_an_idle_rival(self):
        world, _ = self.play()
        assert world.development("p1")["score"] > world.development("p2")["score"]

    def test_a_broke_player_still_survives_the_loop(self):
        """No credits must mean "cannot afford", never a crash or a stall."""
        world, errors = self.play(ticks=10, pnl=-10_000.0)
        assert world.empires["p1"].credits == 0.0
        assert all("crash" not in e for e in errors)

    def test_the_map_helpers_read_the_flat_arrays_correctly(self):
        client = self.client()
        world = World(seed=21, players=2)
        world.add_player("p1", "Ada")
        ctx = client.build_world_ctx(
            {"world_tick": 0, "map": world.map_view(),
             "me": world.player_view("p1"), "standings": []}, {})
        hx, hy = world.empires["p1"].home
        assert ctx["terrain"](hx, hy) == world.tile(hx, hy).terrain
        assert ctx["mine"](hx, hy) is True

    def test_off_map_lookups_do_not_explode(self):
        client = self.client()
        world = World(seed=21, players=2)
        world.add_player("p1", "Ada")
        ctx = client.build_world_ctx(
            {"world_tick": 0, "map": world.map_view(),
             "me": world.player_view("p1"), "standings": []}, {})
        assert ctx["terrain"](-5, 999) == "plain" and ctx["mine"](-5, 999) is False
