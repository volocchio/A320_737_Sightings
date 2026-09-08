# Mission Value Handoff

This repo is becoming the evidence front-end for A320/737 Tamarack value analysis.

## Intended loop

1. **A320/737 Sightings** records real observed missions.
2. Flights are filtered by:
   - region: `NA`, `EU_UK`, `OTHER`
   - family: `A320`, `B737`
3. Observed missions are grouped into stage-length / altitude bins.
4. Tamarack Mission Simulator runs representative bins offline once calibrated A320/737 configs exist.
5. Simulator outputs feed the **Leasing_Model** A320 leasing / split-savings economics model.
6. Leasing_Model outputs feed investor deck evidence tables and scenario charts.
7. **Tamarack_525_Financials** remains the project/company run-rate, certification, staffing, and operating-cost estimator.
8. Decks cite the resulting defensible research tables instead of hand-entered claims.

## Current export endpoints

### JSON

```text
/api/mission-bins?region=NA&family=A320
/api/mission-bins?region=EU_UK&family=A320
```

### CSV

```text
/export/mission-bins.csv?region=NA&family=A320
/export/mission-bins.csv?region=EU_UK&family=A320
```

## Model roles

### Leasing_Model

Primary investor economics model for the A320-family opportunity. This should consume validated mission-simulator deltas and convert them into:

- annual fuel savings
- airline / Tamarack savings split
- aircraft adoption / penetration scenarios
- cash flow and valuation impact
- deck-ready JSON/PDF/table outputs

### Tamarack_525_Financials

Project/company financial model. Use this for broader run-rate and cost planning:

- certification/program costs
- staffing/engineering load
- operating expense
- existing 525/CJ programs
- company-level cash needs and valuation context

### A320_737_Sightings

Evidence generator. This app should not make economic claims by itself; it should produce observed market/mission inputs and traceable research tables.

### Tamarack Mission Simulator

Technical delta generator. Once calibrated A320/737 configs exist, it should consume representative mission bins and return flatwing-vs-Tamarack deltas.

## Current fields

Observed/input fields:

- `region`
- `family`
- `distance_bin`
- `altitude_bin`
- `flight_count`
- `representative_distance_nm`
- `representative_altitude_ft`
- `avg_distance_nm`
- `avg_altitude_ft`
- `sim_status`

Reserved simulator/pro forma fields, intentionally blank until calibrated:

- `flatwing_fuel_lb`
- `tamarack_fuel_lb`
- `fuel_saved_lb`
- `fuel_saved_pct`
- `wat_gain_lb`
- `annualized_savings_usd`
- `airline_share_usd`
- `tamarack_share_usd`
- `deck_evidence_note`

## Recommended downstream contract

A future `Leasing_Model` import should treat each exported mission bin as one weighted scenario:

```text
weighted_annual_savings = flight_count × estimated_frequency_factor × fuel_saved_lb × fuel_price_per_lb
airline_share = weighted_annual_savings × airline_split_pct
tamarack_share = weighted_annual_savings × tamarack_split_pct
```

Keep frequency/adoption assumptions in Leasing_Model, not in the sightings app. Sightings should only provide observed evidence and bin weights.

## Notes

- Do not publish fuel/WAT numbers from this layer until A320/737 simulator configs are calibrated.
- The sightings app can defensibly say what aircraft are actually doing: stage length, altitude, operator, routes, and region.
- The simulator layer should defensibly say what Tamarack changes for representative mission bins.
- The financial layer should convert validated savings into shared-savings economics.
