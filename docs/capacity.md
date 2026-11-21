# Column capacity

All columns in our machine are the same. Capacity depends on the drink's package size. Kevin
confirmed this on 19 Nov. So the 20 Nov slot redraw changed no slot's capacity: a slot on any
shelf, at any point across the machine, holds the same number of a given package. On 21 Sep, David said a column takes “as many as physically fit,” set by
container size. He said the chart was “probably close to right.” [21 Sep meeting, row 12](planning/2026-09-21-design-brief-meeting.md)

The team found the figures on the Package Loading chart, part 931,650,021, inside the door. It calls its
figures approximate. Kevin also remembers consulting a manual or looking online.
[Door chart, photographed 9 Nov 2026](images/package-loading-chart-2026-11-09.jpg)

| Catalog package | Products | Units per column | Source | Checked in our machine? |
|---|---:|---:|---|---|
| 7.5 oz can | 1 | 9 | Marshall's 12 Nov override for `PEPSI-REAL_SUGAR-7_5CAN`. The chart has no figure. | Unknown |
| 8.4 oz can | 5 | 11 | Door chart, 240/250 ml can | No count recorded |
| 11 oz carton | 1 | 9 | Marshall's 12 Nov override for `PREMIER-CHOCOLAT-11CAR`. The chart has no figure. | Unknown |
| 12 oz can | 18 | 8 | Door chart | No count recorded |
| 16 oz can | 1 | 8 | Estimate A20. Marshall's 12 Nov override for `MONSTER-WHITE-16CAN` also gives 8. | A20 was never counted. Whether the override came from a machine count is unknown. |
| 16 oz bottle | 7 | 7 | Door chart. Glass holds 7 and PET holds 8. The catalog does not specify the material, so the planner uses 7. | No count recorded |
| 16.9 oz bottle | 4 | 8 | Marshall's 12 Nov overrides for the four PURELEAF SKUs. The chart has no figure. | Unknown |
| 20 oz bottle | 8 | 7 | Door chart, PET | No count recorded |

Catalog counts and overrides are from the team snapshot reported on 19 Nov 2026. The door chart
also gives 8 for a 14 oz PET bottle. That size was absent from the catalog snapshot. The code
matches a catalog size to the chart and gives a SKU override precedence. When both are missing, the
capacity is not counted. [Capacity code](../vending/capacity.py)

For A20, the 16 oz can has the same 66 mm body width as a 12 oz can. The chart's figures put the
column at about 585 mm. Eight 66 mm cans and eleven 53 mm cans fit that estimate. The team made
the estimate on 11 Nov. A 16 oz Ghost can vended from the machine in October. A count in the
machine can replace the estimate. [Assumption A20](assumptions.md)

The planner warns when a planned load leaves space. The weekly dashboard shows the warning on the
plan line and under Needs attention. The first-fill plan shows it beside the product. Budget, pack
size or a closet shortfall can explain a smaller fill. Add the reason to the plan line.
[Preview tool](mcp-server.md) · [Weekly dashboard](dashboard.md)

## Count from the day-one fill's receipts

The course student worker buys and stocks the machine. The team never fills a slot or contacts the worker; counts
come from the receipts.

1. After the Mon 30 Nov fill, read its receipt: `python scripts/bridge.py request REQUEST_ID`.
2. For each package size, take the units the receipt shows loaded into a slot we asked to fill to capacity.
3. Use that count for the matching package size. Note the slot and count for any slot that differs.
4. If no receipt shows it, ask the instructors rather than guess.
5. Run `python -m vending.config show` and copy the current `capacity_overrides` object.
6. Add or update each applicable SKU in that object. Keep the other entries.
7. Save the complete object with your name. The command replaces the whole object.

```sh
python -m vending.config set capacity_overrides '{"SKU": 8}' --by NAME
```

In Windows PowerShell 5.1, escape the double quotes as follows.

```powershell
python -m vending.config set capacity_overrides '{\"SKU\": 8}' --by NAME
```

Replace `SKU`, `8` and `NAME` with the recorded values. The command lists any entries it removed.
The setting accepts 1 to 15 units and matches SKUs exactly. A SKU cannot contain a space. Preview
warns about an override for a SKU missing from the catalog or a count greater than 1.5 times the
chart figure.

## Related planning references

- The [opening capital plan](opening-capital-plan.md#1-the-day-one-fill) uses capacity to estimate the first fill's cost.
- The [restock policy](restock-policy.md) uses capacity to estimate days of cover.
- [Assumption A8](assumptions.md) records the week replayer's 50-unit sanity limit. Its test cases lack package sizes, so the physical figures do not apply to that check.
- The [tool list](planning/tool-list.md) defines `fill_to`. Omit it in the desired state to use the product's capacity.
