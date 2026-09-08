# Mission Value Handoff

This repo is becoming the evidence front-end for A320/737 Tamarack value analysis.

## Intended loop

1. **A320/737 Sightings** records real observed missions.
2. Flights are filtered by:
   - region: `NA`, `EU_UK`, `OTHER`
   - family: `A320`, `B737`
3. Observed missions are grouped into stage-length / altitude bins.
4. Tamarack Mission Simulator runs representative bins offline once calibrated A320/737 configs exist.
5. Simulator outputs feed financial/pro forma models.
6. Decks cite the resulting defensible research tables instead of hand-entered claims.

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

## Notes

- Do not publish fuel/WAT numbers from this layer until A320/737 simulator configs are calibrated.
- The sightings app can defensibly say what aircraft are actually doing: stage length, altitude, operator, routes, and region.
- The simulator layer should defensibly say what Tamarack changes for representative mission bins.
- The financial layer should convert validated savings into shared-savings economics.
