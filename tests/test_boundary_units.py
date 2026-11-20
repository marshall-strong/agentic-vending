"""analysis/boundary_units.py: units sold in a poll interval whose two ends show different prices."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis"))

from boundary_units import NOON_RULE, SALES_RULE, price_sales, priced_average, slot_average_price, split_among, split_boundary  # noqa: E402

T0 = datetime.fromisoformat("2026-12-04T09:00:00-05:00")
T = [T0 + timedelta(hours=h) for h in range(3)]


def test_the_counters_settle_a_mixed_interval():
    # 3 units, 650 cents, price 150 -> 250: 150*old + 250*(3-old) = 650 -> old = 1
    assert split_boundary(3, 650, 150, 250) == (1, 2, 0)


def test_all_units_at_the_old_price_when_the_cents_say_so():
    assert split_boundary(2, 300, 150, 250) == (2, 0, 0)


def test_all_units_at_the_new_price_are_not_credited_to_the_opening_price():
    assert split_boundary(2, 500, 150, 250) == (0, 2, 0)


def test_units_the_counters_cannot_split_are_unknown_not_the_opening_price():
    # 2 units, 333 cents fits no whole split of 150 and 250 (a misread or a third price)
    assert split_boundary(2, 333, 150, 250) == (0, 0, 2)
    assert split_boundary(2, None, 150, 250) == (0, 0, 2)


def test_an_interval_with_one_price_is_all_that_price():
    assert split_boundary(4, 600, 150, 150) == (4, 0, 0)
    assert split_boundary(4, 600, None, 150) == (0, 4, 0)       # no reading before it: the one reading there is


def average(readings, sales):
    return priced_average(price_sales(readings, sales))


def test_a_mixed_sale_is_priced_by_its_split_in_the_average():
    # 10 units at 150, then 3 units and 650 cents across the change to 250: one at 150 and two at 250
    readings = [(T[0], 0, 0, 150), (T[1], 10, 1500, 150), (T[2], 13, 2150, 250)]
    sales = [(T[0], T[1], 10, 1500), (T[1], T[2], 3, 650)]
    assert [p[2:5] for p in price_sales(readings, sales)] == [(10, 0, 0), (1, 2, 0)]
    avg, unknown = average(readings, sales)
    assert avg == (1500 + 150 + 2 * 250) / 13 and unknown == 0            # not 150, the opening price of every unit


def test_an_unsplittable_sale_is_left_out_of_the_average_and_counted():
    # 2 units and 333 cents across 150 -> 250 fit no split: unknown, so the average is the 10 sure units' 150
    readings = [(T[0], 0, 0, 150), (T[1], 10, 1500, 150), (T[2], 12, 1833, 250)]
    sales = [(T[0], T[1], 10, 1500), (T[1], T[2], 2, 333)]
    assert average(readings, sales) == (150, 2)


def test_when_every_unit_is_unknown_there_is_no_average():
    readings = [(T[0], 0, 0, 150), (T[1], 2, 333, 250)]
    assert average(readings, [(T[0], T[1], 2, 333)]) == (None, 2)


def test_the_reported_average_price_falls_back_to_the_noon_price_when_every_unit_is_unknown():
    # what sales_analysis reports for a slot: avg_price (cents here), avg_price_rule and price_unknown_units together
    readings = [(T[0], 0, 0, 150), (T[1], 2, 333, 250)]
    parts = price_sales(readings, [(T[0], T[1], 2, 333)])
    assert slot_average_price(parts, lambda: 200.0) == (200.0, NOON_RULE, 2)         # the mean noon price, not None or 250
    # with a unit of known price the average is the units' own, and the noon price is not asked for
    known = price_sales([(T[0], 0, 0, 150), (T[1], 10, 1500, 150), *readings[1:]], [(T[0], T[1], 10, 1500)])
    assert slot_average_price(known, lambda: 1 / 0) == (150, SALES_RULE, 0)


def test_unit_and_cents_rises_from_different_polls_are_not_split():
    # units 10, 11, 13 but cents 1000, 5, 1400 (the middle cents reading is a misread): the units rose from T[1] and
    # the cents from T[0], so the 400 cents span all three units, not the last two. Prices 100, 100, 200.
    readings = [(T[0], 10, 1000, 100), (T[1], 11, 5, 100), (T[2], 13, 1400, 200)]
    sales = [(T[0], T[1], 1, 0), (T[1], T[2], 2, 400)]
    assert [p[2:5] for p in price_sales(readings, sales)] == [(1, 0, 0), (0, 0, 2)]
    assert average(readings, sales) == (100, 2)                           # not 166.7: the two last units are not at 200


def test_a_misread_both_counters_share_does_not_stop_the_split():
    # both counters misread at T[1] and agree on the window from T[0]; the sale at T[2] still splits (price 150 -> 250)
    readings = [(T[0], 10, 1000, 150), (T[1], 4, 500, 150), (T[2], 13, 1650, 250)]
    assert price_sales(readings, [(T[1], T[2], 3, 650)])[0][2:5] == (1, 2, 0)


def test_a_shared_misread_does_not_hide_a_price_change_before_it():
    # prices 150, 250, 250; both counters misread at T[1] and agree on the window from T[0]. The three units and 650 cents
    # fit one at 150 and two at 250 uniquely: the opening price is the price at the window's start, not at the misread poll
    readings = [(T[0], 10, 1000, 150), (T[1], 4, 500, 250), (T[2], 13, 1650, 250)]
    assert price_sales(readings, [(T[1], T[2], 3, 650)])[0] == (150, 250, 1, 2, 0, ())
    assert average(readings, [(T[1], T[2], 3, 650)]) == ((150 + 2 * 250) / 3, 0)       # not 250 for all three


def test_a_price_seen_only_inside_the_window_still_prices_its_units():
    # S7 of #174: prices 150, 250, 150, both counters misread at T[1] and agree on the window from T[0]. The window opens and
    # closes at 150, but 250 was listed in between: 3 units and 650 cents fit one split only (one at 150, two at 250)
    readings = [(T[0], 10, 1000, 150), (T[1], 4, 500, 250), (T[2], 13, 1650, 150)]
    assert average(readings, [(T[1], T[2], 3, 650)]) == (650 / 3, 0)         # old code: 150 for all three units
    # the same window with other takings: 333 cents fit no split of 3 units at 150 and 250; 450 fit only all three at 150
    assert average(readings, [(T[1], T[2], 3, 333)]) == (None, 3)            # unknown, never credited to 150
    assert average(readings, [(T[1], T[2], 3, 450)]) == (150, 0)
    # the split names the units at the price that was neither the opening nor the closing one
    assert price_sales(readings, [(T[1], T[2], 3, 650)])[0] == (150, 150, 1, 0, 0, ((250, 2),))


def test_several_splits_among_the_prices_in_a_window_are_unknown_not_a_guess():
    # prices 100, 200, 300: 3 units and 600 cents fit three at 200, or one each at 100, 200, 300 - nothing says which
    readings = [(T[0], 10, 1000, 100), (T[1], 4, 500, 200), (T[2], 13, 1600, 300)]
    assert price_sales(readings, [(T[1], T[2], 3, 600)])[0] == (100, 300, 0, 0, 3, ())
    assert average(readings, [(T[1], T[2], 3, 600)]) == (None, 3)
    assert split_among(3, 600, [100, 200, 300]) is None                  # three at 200, or one at each
    assert split_among(3, 500, [100, 200, 300]) is None                  # two at 100 and one at 300, or one at 100 and two at 200


def test_split_among_finds_the_one_whole_split():
    assert split_among(3, 650, [150, 250]) == [(150, 1), (250, 2)]
    assert split_among(3, 650, [250, 150, 350]) is None                  # one at 150 and two at 250, or two at 150 and one at 350
    assert split_among(3, 333, [150, 250]) is None and split_among(3, None, [150, 250]) is None
    assert split_among(0, 0, [150, 250]) == [(150, 0), (250, 0)]


def test_a_window_with_one_price_all_through_is_all_that_price_whatever_the_cents():
    readings = [(T[0], 10, 1000, 150), (T[1], 4, 500, 150), (T[2], 13, 1500, 150)]
    assert price_sales(readings, [(T[1], T[2], 3, 333)])[0] == (150, 150, 3, 0, 0, ())      # as before: one price, nothing to split


def test_a_window_with_a_price_in_the_middle_is_unknown_when_the_counters_windows_differ():
    # prices 150, 250, 150; the units' window starts at T[0] after a misread, the cents' at T[1]: the cents cover other units
    readings = [(T[0], 10, 1000, 150), (T[1], 4, 1100, 250), (T[2], 13, 1750, 150)]
    assert price_sales(readings, [(T[1], T[2], 3, 650)])[0] == (150, 150, 0, 0, 3, ())



def test_a_sale_before_the_slots_first_reading_takes_that_reading():
    readings = [(T[1], 5, 750, 150)]
    assert price_sales(readings, [(T[0], T[1], 5, 750)])[0] == (150, 150, 5, 0, 0, ())


# --- review round 1 on the fix for S7 ---

def test_a_large_counter_jump_that_cannot_fit_is_rejected_at_once_not_searched():
    import time

    # prices 100, 200, 300 and both counters misread at T[1]: every sale is a multiple of 100 cents, so 1,600,001 cents
    # over 8,000 units can't fit. The old search worked through the counts (quadratic in the units: seconds here)
    jump, takings = 8_000, 1_600_001
    readings = [(T[0], 10, 1000, 100), (T[1], 4, 500, 200), (T[2], 10 + jump, 1000 + takings, 300)]
    started = time.perf_counter()
    parts = price_sales(readings, [(T[1], T[2], jump, takings)])
    assert time.perf_counter() - started < 2.0              # microseconds when rejected by the prices' gcd; seconds searched
    assert parts == [(100, 300, 0, 0, jump, ())]
    # a jump too big for any search is just as quick
    big = 10 ** 12
    readings = [(T[0], 10, 1000, 100), (T[1], 4, 500, 200), (T[2], 10 + big, 1000 + 200 * big + 1, 300)]
    started = time.perf_counter()
    assert price_sales(readings, [(T[1], T[2], big, 200 * big + 1)])[0][4] == big
    assert time.perf_counter() - started < 2.0


def test_a_missing_end_price_does_not_skip_the_split_among_the_prices_seen():
    # no price at the opening poll, then 250, then 150 at the closing poll; both counters misread at T[1]. 3 units and 650
    # cents fit one at 150 and two at 250 only: not all three at the closing price
    readings = [(T[0], 10, 1000, None), (T[1], 4, 500, 250), (T[2], 13, 1650, 150)]
    assert price_sales(readings, [(T[1], T[2], 3, 650)])[0] == (None, 150, 0, 1, 0, ((250, 2),))
    assert average(readings, [(T[1], T[2], 3, 650)]) == (650 / 3, 0)
    # no price at the closing poll: the same split, the other way round
    readings = [(T[0], 10, 1000, 150), (T[1], 4, 500, 250), (T[2], 13, 1650, None)]
    assert price_sales(readings, [(T[1], T[2], 3, 650)])[0] == (150, None, 1, 0, 0, ((250, 2),))


def test_split_among_agrees_with_counting_every_way_for_small_cases():
    from collections import Counter
    from itertools import combinations, combinations_with_replacement

    every = (100, 150, 250, 300, 450)
    for k in range(1, 6):
        for prices in combinations(every, k):
            for units in range(0, 6):
                ways: dict[int, list] = {}
                for sold in combinations_with_replacement(prices, units):
                    count = Counter(sold)
                    ways.setdefault(sum(sold), []).append([(p, count.get(p, 0)) for p in prices])
                for cents in range(-50, units * 450 + 100, 50):
                    want = ways[cents][0] if len(ways.get(cents, [])) == 1 else None
                    assert split_among(units, cents, list(reversed(prices))) == want, (prices, units, cents)


def test_a_search_over_four_or_more_prices_stops_at_its_bound_and_leaves_the_units_unknown(monkeypatch):
    import boundary_units

    prices = [100, 200, 300, 400]
    assert split_among(20, 8000, prices) == [(100, 0), (200, 0), (300, 0), (400, 20)]       # all at the top: the one split
    assert split_among(2, 600, [100, 200, 300, 400, 500]) is None                             # 100+500, 200+400, 300+300
    assert split_among(2, 900, [100, 200, 300, 400, 500]) == [(100, 0), (200, 0), (300, 0), (400, 1), (500, 1)]
    monkeypatch.setattr(boundary_units, "MAX_NODES", 0)
    assert split_among(20, 8000, prices) is None                                              # past the bound: unknown
    monkeypatch.undo()
    # a huge count over many prices ends at the bound, not after the counts
    import time
    started = time.perf_counter()
    assert split_among(10 ** 12, 10 ** 12 * 250, [100, 200, 300, 400, 500]) is None
    assert time.perf_counter() - started < 2.0


def test_a_proven_split_among_four_prices_is_found_whatever_the_number_of_units():
    # round 2: only the top price can take 2,000,000 cents over 5,000 units, and the search used to spend its whole budget on
    # counts the prices' bounds rule out, then give up (the units unknown, the slot's average skewed)
    assert split_among(5_000, 2_000_000, [100, 200, 300, 400]) == [(100, 0), (200, 0), (300, 0), (400, 5_000)]
    assert split_among(5_000, 2_500_000, [100, 200, 300, 400, 500]) == [(100, 0), (200, 0), (300, 0), (400, 0), (500, 5_000)]
    # one unit one step down from all at the top price: still the only split, a billion units over
    assert split_among(10 ** 9, 4 * 10 ** 11 - 100, [100, 200, 300, 400]) == [
        (100, 0), (200, 0), (300, 1), (400, 10 ** 9 - 1)]


def test_a_search_that_hits_its_cap_says_so_and_one_that_ends_on_its_own_does_not(monkeypatch):
    import boundary_units

    wide = [100, 200, 300, 400]                      # 20 units and 5,000 cents: many splits
    for cap, said in ((0, True), (5_000, False)):    # a cap of 0 lets no step through
        monkeypatch.setattr(boundary_units, "MAX_NODES", cap)
        capped: list = []
        assert split_among(20, 5_000, wide, capped) is None
        assert bool(capped) is said                  # unknown by the cap when it can't search, several splits when it can
    monkeypatch.setattr(boundary_units, "MAX_NODES", 1)
    capped = []
    assert split_among(20, 8_000, wide, capped) == [(100, 0), (200, 0), (300, 0), (400, 20)]   # one count to try: one step
    assert capped == []
    monkeypatch.setattr(boundary_units, "MAX_NODES", 0)
    # turned away before any search, or solved without one: never the cap's doing, even with a cap of 0
    for units, cents, prices in ((20, 8_001, wide), (-1, -100, wide), (20, None, wide), (20, 9_000, wide), (20, 5_000, [300, 100, 200])):
        assert split_among(units, cents, prices, capped) is None
    assert capped == []


def test_price_sales_reports_which_sales_had_their_split_search_capped(monkeypatch):
    import boundary_units

    def gives_up(units, cents, prices, capped=None):       # a window with four prices is out of reach of real polls: stand in
        if cents is not None:
            capped.append(units)
        return None
    monkeypatch.setattr(boundary_units, "split_among", gives_up)
    readings = [(T[0], 10, 1000, 150), (T[1], 4, 500, 250), (T[2], 13, 1650, 150)]
    capped: list = []
    parts = price_sales(readings, [(T[0], T[1], 1, 150), (T[1], T[2], 3, 650)], capped)
    assert capped == [1] and parts[1][4] == 3              # the second sale, by its place in the list; its units unknown
    assert price_sales(readings, [(T[1], T[2], 3, 650)])[0][4] == 3      # no list given: nothing to report to, nothing breaks


def test_a_float_or_blank_price_is_read_as_whole_cents_or_not_at_all():
    from boundary_units import price_value
    nan = float("nan")
    assert [price_value(p) for p in (150, 150.0, None, nan, float("inf"), 150.5, "x")] == [150, 150, None, None, None, None, None]
    assert split_among(2, 400, [150.0, 250.0]) == [(150, 1), (250, 1)]              # floats, as pandas reads a column with a blank
    assert split_among(2, 400, [150.0, nan, 250.0, None]) == [(150, 1), (250, 1)]   # the blank is left out, not a price
    assert split_among(2, 300, [150.0, nan]) == [(150, 2)]
    assert split_among(2, 300, [nan, None]) is None


def test_split_among_refuses_a_negative_number_of_units():
    assert split_among(-1, -150, [150]) is None
    assert split_among(-2, -500, [150, 250]) is None
    assert split_among(0, 0, [150]) == [(150, 0)]                          # no units, no takings: still the one split
    assert split_among(0, 5, [150]) is None
