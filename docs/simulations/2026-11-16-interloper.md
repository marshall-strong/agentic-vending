# What the Interloper might cost us: four simulated seasons

**Simulated data.** The simple refill rule (no model, $0 spent) through the full competition, Thu 3 Dec to Wed 3 Feb, with the Interloper (#116) taking a share of demand for the drinks it also sells. Nobody can see its sales, so the share is a guess; these four seasons show the range.

## The model

`--interloper S` in `python -m vending.simulate` (`vending/simulate/world.py`, `interloper_loss`):

- **The same drink** (Pepsi, Mountain Dew or Spindrift, which it sells as 12 oz cans at $1.50) loses `S` of its demand when our price matches its $1.50. Dearer loses more, in proportion to the price ratio, capped at twice; cheaper loses less. The comparison is per unit, not per ounce: a shopper sees two price tags.
- **Energy drinks** (Red Bull, Celsius, Gorgie, Monster) lose 0.4 of that against its $4.00 Reign.
- **Everything else** (Coke, Diet Coke, Snapple, AriZona, Hint) is untouched. Its protein shakes aren't products in the simulator.
- No loss is above 90%. `S = 0` is the simulator without the Interloper.

## Results

Seed 20261203, Thursday 19:00 runs, the simple rule:

| Share taken (`S`) | Profit after model spend | Change | Units sold | Red Bull | Celsius | Mountain Dew 20 oz |
|---|---:|---:|---:|---:|---:|---:|
| 0 (no Interloper) | $462.33 | | 323 | 156 | 64 | 29 |
| 0.15 | $434.10 | −$28 | 304 | | | |
| 0.25 | $412.11 | −$50 | 290 | 142 | 54 | 21 |
| 0.40 | $380.33 | −$82 | 274 | 129 | 53 | 18 |

Coke and Diet Coke sold the same in every season (41 and 32 to 33).

The `S = 0` season is $2.29 below the archived baseline ($464.62) because this branch includes the 15 Nov sourcing drinks (`ea19996`). At `S = 0` the new code multiplies demand by exactly 1.

## What it says

- **Most of the cost is energy, not cans.** Red Bull and Celsius are most of our volume, so even a weaker pull from Reign costs more than the head-to-head cans. At `S = 0.25`, 24 of the 33 lost units are energy.
- **The rule doesn't react.** It keeps the same drinks and prices whatever sells. An agent that moves away from the overlap, or prices energy against $4.00, should lose less; that's the test for the agent's `interloper` topic (#153), and it needs a model season (about $3).
- **The range is wide because the share is unknown.** Our first live weeks will show it: in-stock sales of Red Bull, Celsius and Mountain Dew against the fall rates, while Coke shows the baseline (A21 in `docs/assumptions.md`, #153).

## Rerun

```
python -m vending.db migrate                       # on each scratch database first
python -m vending.simulate --database-url postgresql://postgres:...@127.0.0.1:PORT/s25 \
    --decider scripted --interloper 0.25 --out <dir>
```

The seasons' data isn't archived in `docs/simulations/data/`, to keep the image small; rerunning reproduces it.
