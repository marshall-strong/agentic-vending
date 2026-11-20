"""analysis/visit_windows.py: visits from door-counter rises, the unseen window, and our sales inside it."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis"))

import visit_windows as vw  # noqa: E402

T0 = datetime.fromisoformat("2026-10-05T07:00:00-04:00")     # a Monday, in the hourly era
FRIDAY = datetime.fromisoformat("2026-12-04T07:00:00-05:00")  # after the start (3 Dec), a Friday
WEDNESDAY = datetime.fromisoformat("2026-12-09T07:00:00-05:00")


def at(h, t0=T0):
    return (t0 + timedelta(hours=h)).isoformat()


def polls(hours, t0=T0):
    return [{"at_eastern": at(h, t0)} for h in hours]


def door(rises, t0=T0):
    """rises: {hour: rise} on an hourly poll grid from 0 to 12, as export_sales.py writes them (lifetime and rise)."""
    rows, life = [], 1000
    for h in range(0, 13):
        life += rises.get(h, 0)
        rows.append({"at_eastern": at(h, t0), "door_open_lifetime": str(life), "rise": str(rises.get(h, 0))})
    return rows


def receipt(first_h, done_h, t0=FRIDAY):
    """One fulfilled receipt of ours: (first line resolved, fulfilled_at), as door_events.read_receipts gives them."""
    return (t0 + timedelta(hours=first_h), t0 + timedelta(hours=done_h))


def sale(h, units=1, slot="15"):
    return {"slot": slot, "at_eastern": at(h), "units": str(units)}


def test_one_rise_is_one_visit_with_a_one_poll_window():
    r = vw.analyse(door({6: 1}), polls(range(13)), [])
    (v,) = r["visits"]
    assert v["window_min"] == 60 and v["openings"] == 1 and v["weekday"] == "Mon"
    assert v["from"].startswith("2026-10-05T12:00") and v["to"].startswith("2026-10-05T13:00")


def test_rises_within_three_hours_merge_and_widen_the_window():
    r = vw.analyse(door({4: 2, 6: 3}), polls(range(13)), [])
    (v,) = r["visits"]
    assert v["openings"] == 5 and v["window_min"] == 180          # 10:00 (poll before) to 13:00


def test_rises_more_than_three_hours_apart_are_two_visits():
    r = vw.analyse(door({2: 1, 8: 1}), polls(range(13)), [])
    assert len(r["visits"]) == 2


def test_our_sales_at_a_rise_poll_count_as_inside_the_window():
    r = vw.analyse(door({6: 1}), polls(range(13)), [sale(6, 2), sale(5, 4), sale(9, 1, "16")])
    (v,) = r["visits"]
    assert v["our_units_inside"] == 2                               # only the poll that also saw the door open
    assert v["minutes_to_next_sale"] == 180                         # 13:00 to the 16:00 sale
    assert r["summary"]["our_units_inside"] == 2 and r["summary"]["our_units"] == 7


def test_visits_before_hourly_polling_are_left_out():
    early = vw.HOURLY_FROM - timedelta(days=7)
    r = vw.analyse(door({6: 1}), polls(range(13)), [], since=early + timedelta(days=30))
    assert r["visits"] == []


def test_poll_gaps_report_the_long_ones():
    g = vw.poll_gaps([T0 + timedelta(hours=h) for h in (0, 1, 2, 5, 6)], T0)
    assert g["gaps"] == 4 and g["over_65_min"] == 1 and g["over_125_min"] == 1
    assert g["longest"][0]["minutes"] == 180


def test_grades_come_from_the_lab_data_by_window_start():
    lab = {"visits": [{"start": at(5)[:16], "evidence": "likely refill"}], "calendar": {"days": []}}
    r = vw.analyse(door({6: 1}), polls(range(13)), [], lab)
    assert r["visits"][0]["grade"] == "likely refill"
    assert r["summary"]["restock_visits"] == 1 and r["summary"]["restock_weekdays"] == {"Mon": 1}


def test_without_lab_data_visits_are_ungraded():
    r = vw.analyse(door({6: 1}), polls(range(13)), [])
    assert r["visits"][0]["grade"] is None and r["summary"]["graded"] is False


def test_a_sale_between_two_merged_rises_is_inside_the_window():
    r = vw.analyse(door({4: 1, 6: 1}), polls(range(13)), [sale(5, 3), sale(6, 1), sale(8, 2)])
    (v,) = r["visits"]
    assert v["our_units_inside"] == 4                               # 11:00 to 13:00: the 12:00 and 13:00 polls


def test_hours_follow_the_hour_a_poll_closes_whether_it_lands_at_59_or_00():
    t59 = (T0 + timedelta(hours=6, minutes=59)).isoformat()         # 13:59 poll: sold 13:00-14:00
    t00 = (T0 + timedelta(hours=7)).isoformat()                     # 14:00 poll: also 13:00-14:00
    sales = [{"slot": "15", "at_eastern": t59, "units": "2"}, {"slot": "15", "at_eastern": t00, "units": "1"}]
    r = vw.analyse(door({}), polls(range(13)), sales)
    by = {p["hour"]: p["units_per_day"] for p in r["hour_profile"]}
    assert by[13] == 3 and by[14] == 0


def test_a_misread_door_reading_is_not_a_visit():
    # (1000, 5, 1000): one low reading between good polls. The old exporter wrote a rise of 995 at the poll after
    # the misread, so the `rise` column of such an export is wrong; the visit finder reads the lifetime count instead.
    lifetimes = {5: 5}
    rows = [{"at_eastern": at(h), "door_open_lifetime": str(lifetimes.get(h, 1000)), "rise": "995" if h == 6 else "0"}
            for h in range(0, 13)]
    r = vw.analyse(rows, polls(range(13)), [])
    assert r["visits"] == []


def test_a_door_opening_after_a_misread_counts_from_the_old_high():
    lifetimes = {5: 5, 6: 1002}
    rows = [{"at_eastern": at(h), "door_open_lifetime": str(lifetimes.get(h, 1000 if h < 6 else 1002)), "rise": "0"}
            for h in range(0, 13)]
    (v,) = vw.analyse(rows, polls(range(13)), [])["visits"]
    assert v["openings"] == 2 and v["to"].startswith("2026-10-05T13:00")


# --- after the start (3 Dec): a rise is our visit only when a fulfilled receipt of ours falls with it ---

def test_a_midweek_door_only_rise_after_the_start_is_not_our_visit():
    r = vw.analyse(door({6: 1}, WEDNESDAY), polls(range(13), WEDNESDAY), [])
    assert r["visits"] == [] and r["summary"]["visits"] == 0


def test_a_door_only_rise_near_our_friday_visit_is_not_merged_into_it():
    # rise at 10:00 with no receipt, our visit seen at the 13:00 poll (receipt 12:15, fulfilled 12:30)
    rows = door({3: 1, 6: 2}, FRIDAY)
    (v,) = vw.analyse(rows, polls(range(13), FRIDAY), [], receipts=[receipt(5.25, 5.5)])["visits"]
    assert v["openings"] == 2 and v["window_min"] == 60          # old code: 3 openings, 240 min (10:00 poll before to 13:00)
    assert v["from"].startswith("2026-12-04T12:00") and v["to"].startswith("2026-12-04T13:00")


def test_our_visit_with_a_receipt_is_found_and_rises_before_the_start_still_merge():
    (v,) = vw.analyse(door({6: 1}, FRIDAY), polls(range(13), FRIDAY), [], receipts=[receipt(5.25, 5.5)])["visits"]
    assert v["openings"] == 1 and v["weekday"] == "Fri"
    # before 3 Dec nothing is ours or not ours: no receipts needed, merged as before
    (old,) = vw.analyse(door({4: 2, 6: 3}), polls(range(13)), [])["visits"]
    assert old["openings"] == 5 and old["window_min"] == 180


def door_counts(counts, t0=FRIDAY):
    """Hourly door readings from t0, as the file's lifetime column: counts is the list of readings."""
    return [{"at_eastern": at(h, t0), "door_open_lifetime": str(c), "rise": "0"} for h, c in enumerate(counts)]


def test_a_receipt_with_two_candidate_rises_is_one_visit_with_an_ambiguous_window():
    # rises seen at 12:00 and 13:00, a receipt resolved 12:15 and fulfilled 12:30: either window could hold the opening
    # (the 13:00 window containing the receipt is not preferred). One visit of ours; neither rise is in a window or a sale
    sales = [{"slot": "15", "at_eastern": at(h, FRIDAY), "units": str(u)} for h, u in ((5, 2), (6, 3))]
    r = vw.analyse(door({5: 1, 6: 1}, FRIDAY), polls(range(13), FRIDAY), sales, receipts=[receipt(5.25, 5.5)])
    (v,) = r["visits"]
    assert v["window"] == "ambiguous" and v["from"] is None and v["to"] is None and v["window_min"] is None
    assert v["openings"] is None and v["our_units_inside"] is None and v["weekday"] == "Fri"
    assert r["summary"]["visits"] == 1 and r["summary"]["our_units_inside"] == 0 and r["summary"]["window_median_min"] is None
    # two receipts of one visit are still one visit
    two = [receipt(5.25, 5.5), receipt(5.3, 5.6)]
    assert len(vw.analyse(door({5: 1, 6: 1}, FRIDAY), polls(range(13), FRIDAY), [], receipts=two)["visits"]) == 1


def test_a_receipt_reaches_back_an_hour_from_its_fulfilment_not_from_its_first_line():
    # first line 10:06, fulfilled 11:30: a visit runs at most an hour, so a door seen at 10:00 opened before it began.
    # The receipt still proves our visit, with no door window; the 10:00 rise is not in it
    (v,) = vw.analyse(door({3: 1}, FRIDAY), polls(range(13), FRIDAY), [], receipts=[receipt(3.1, 4.5)])["visits"]
    assert v["window"] == "unknown" and v["from"] is None and v["window_min"] is None and v["openings"] is None
    assert v["our_units_inside"] is None


def test_a_rise_after_the_first_line_was_resolved_is_ruled_out():
    # first line 12:50, fulfilled 13:10: the door opened by 12:50, so the only rise, seen at 14:00 (13:00 to 14:00), is not ours
    (v,) = vw.analyse(door({7: 1}, FRIDAY), polls(range(13), FRIDAY), [], receipts=[receipt(5.83, 6.17)])["visits"]
    assert v["window"] == "unknown" and v["from"] is None      # old code claimed that 14:00 rise: a 60 minute window


def test_a_receipt_with_one_candidate_rise_claims_it_and_not_the_later_rise():
    # same receipt, rises seen at 13:00 and 14:00: only the 13:00 window can hold an opening by 12:50
    (v,) = vw.analyse(door({6: 1, 7: 1}, FRIDAY), polls(range(13), FRIDAY), [], receipts=[receipt(5.83, 6.17)])["visits"]
    assert v["window"] == "known" and v["openings"] == 1
    assert v["from"].startswith("2026-12-04T12:00") and v["to"].startswith("2026-12-04T13:00")


def test_two_openings_seen_at_one_poll_are_one_visit_and_the_count_stays_machine_wide():
    (v,) = vw.analyse(door({6: 2}, FRIDAY), polls(range(13), FRIDAY), [], receipts=[receipt(5.25, 5.5)])["visits"]
    assert v["openings"] == 2 and v["window_min"] == 60


def test_a_misread_does_not_hide_our_visit_after_the_start():
    # 100, 5, 101 from 11:00: the rise is measured from 11:00, and our receipt was fulfilled 11:35
    rows = door_counts([100] * 4 + [100, 5, 101] + [101] * 6)          # polls 07:00 to 19:00; 11:00 reads 100, 12:00 reads 5
    (v,) = vw.analyse(rows, polls(range(13), FRIDAY), [], receipts=[receipt(4.33, 4.58)])["visits"]
    assert v["openings"] == 1 and v["window_min"] == 120
    assert v["from"].startswith("2026-12-04T11:00") and v["to"].startswith("2026-12-04T13:00")


# --- the CLI's "rerun the analysis" warning fires only on data that is really stale (S10 of #174) ---

def write_data(tmp_path, door_rows, receipts, lab):
    """The CSVs export_sales.py writes, and the analysis's lab_data.json, in tmp_path; the paths main() takes."""
    import csv
    import json

    def write(name, header, rows):
        with open(tmp_path / name, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
    write("door_events.csv", ["at_eastern", "door_open_lifetime", "door_open_since_reset", "rise"],
          [(r["at_eastern"], r["door_open_lifetime"], r["door_open_lifetime"], r["rise"]) for r in door_rows])
    write("polls.csv", ["at_eastern"], [(r["at_eastern"],) for r in polls(range(13), FRIDAY)])
    write("sales_events.csv", ["slot", "row", "col", "at_eastern", "units", "cents"], [])
    write("receipts.csv", ["request_id", "first_at_eastern", "fulfilled_at_eastern"],
          [("r%d" % i, first.isoformat(), done.isoformat()) for i, (first, done) in enumerate(receipts)])
    (tmp_path / "lab_data.json").write_text(json.dumps(lab), encoding="utf-8")
    return ["--data", str(tmp_path), "--lab-data", str(tmp_path / "lab_data.json"), "--out", str(tmp_path / "out.json")]


def lab_listing(*unplaced):
    """lab_data.json as sales_analysis.py writes it: no visit with a door window, and `unplaced` receipts without one."""
    return {"visits": [], "calendar": {"days": []},
            "refill_evidence": {"visits_without_window": [
                {"fulfilled_at": done.isoformat(), "window": kind, "candidate_rises": []}
                for done, kind in unplaced]}}


def test_a_fresh_lab_listing_an_ambiguous_visit_does_not_warn(tmp_path, capsys):
    # rises seen at 12:00 and 13:00 and a receipt fulfilled 12:30: an ambiguous visit, which the analysis lists but cannot grade
    done = receipt(5.25, 5.5)[1]
    argv = write_data(tmp_path, door({5: 1, 6: 1}, FRIDAY), [receipt(5.25, 5.5)], lab_listing((done, "ambiguous")))
    vw.main(argv)
    assert "WARNING" not in capsys.readouterr().out                      # old code: warned, as for every visit with no grade


def test_a_fresh_lab_listing_an_unknown_visit_does_not_warn(tmp_path, capsys):
    done = receipt(3.1, 4.5)[1]
    argv = write_data(tmp_path, door({3: 1}, FRIDAY), [receipt(3.1, 4.5)], lab_listing((done, "unknown")))
    vw.main(argv)
    assert "WARNING" not in capsys.readouterr().out


def test_a_lab_that_does_not_list_the_visit_is_stale_and_warns(tmp_path, capsys):
    # a lab_data.json from before this receipt (or from the old analysis): nothing lists the visit
    argv = write_data(tmp_path, door({5: 1, 6: 1}, FRIDAY), [receipt(5.25, 5.5)], lab_listing())
    vw.main(argv)
    assert "WARNING" in capsys.readouterr().out
    # and one that lists it with another window kind was not made from these receipts
    argv = write_data(tmp_path, door({5: 1, 6: 1}, FRIDAY), [receipt(5.25, 5.5)], lab_listing((receipt(5.25, 5.5)[1], "unknown")))
    vw.main(argv)
    assert "WARNING" in capsys.readouterr().out


def test_a_lab_listing_one_receipt_does_not_silence_the_warning_for_another_in_the_same_minute(tmp_path, capsys):
    # two unknown visits, fulfilled at 11:30:00 and 11:30:15; the lab data lists only the first
    first = (FRIDAY + timedelta(hours=3.1), FRIDAY + timedelta(hours=4.5))
    second = (FRIDAY + timedelta(hours=3.1), FRIDAY + timedelta(hours=4.5, seconds=15))
    argv = write_data(tmp_path, door({3: 1}, FRIDAY), [first, second], lab_listing((first[1], "unknown")))
    vw.main(argv)
    assert "WARNING" in capsys.readouterr().out                         # old code matched on the minute: no warning
    lab = lab_listing((first[1], "unknown"))
    visits = vw.analyse(door({3: 1}, FRIDAY), polls(range(13), FRIDAY), [], lab, receipts=[first, second])["visits"]
    assert [v["grade"] for v in visits] == [vw.NO_WINDOW_GRADE, None]
    # listed with both, nothing is stale
    argv = write_data(tmp_path, door({3: 1}, FRIDAY), [first, second], lab_listing((first[1], "unknown"), (second[1], "unknown")))
    vw.main(argv)
    assert "WARNING" not in capsys.readouterr().out


def test_a_visit_without_a_door_window_is_graded_as_having_none(tmp_path):
    done = receipt(5.25, 5.5)[1]
    lab = lab_listing((done, "ambiguous"))
    (v,) = vw.analyse(door({5: 1, 6: 1}, FRIDAY), polls(range(13), FRIDAY), [], lab, receipts=[receipt(5.25, 5.5)])["visits"]
    assert v["window"] == "ambiguous" and v["grade"] == vw.NO_WINDOW_GRADE
    assert vw.analyse(door({5: 1, 6: 1}, FRIDAY), polls(range(13), FRIDAY), [], None, receipts=[receipt(5.25, 5.5)])["visits"][0]["grade"] is None
