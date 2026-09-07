# A320/737 Sightings

Real-time dashboard and Teams notifications for Airbus A320-family and Boeing 737-family landings.

This app is cloned from `volocchio/525_Sightings` and keeps the same data-source architecture and JetNet credential flow, adapted for airline-family aircraft tracking.

## Aircraft tracked

| ICAO code | Model |
|-----------|-------|
| A318 | Airbus A318 |
| A319 | Airbus A319 |
| A320 | Airbus A320 |
| A321 | Airbus A321 |
| A19N | Airbus A319neo |
| A20N | Airbus A320neo |
| A21N | Airbus A321neo |
| B736 | Boeing 737-600 |
| B737 | Boeing 737-700 |
| B738 | Boeing 737-800 |
| B739 | Boeing 737-900 |
| B37M | Boeing 737 MAX 7 |
| B38M | Boeing 737 MAX 8 |
| B39M | Boeing 737 MAX 9 |
| B3XM | Boeing 737 MAX 10 |

## Credentials

Copy `.env.example` to `.env`. JetNet uses the same variables as the 525 app:

```env
JETNET_USERNAME=
JETNET_PASSWORD=
JETNET_API_KEY=
JETNET_BASE_URL=https://customer.jetnetconnect.com/api
```

FlightAware, ADS-B Exchange, OpenSky, Teams, and OpenAI variables are unchanged from the 525 app.

## Default aircraft config

```env
AIRCRAFT_TYPES=A318,A319,A320,A321,A19N,A20N,A21N,B736,B737,B738,B739,B37M,B38M,B39M,B3XM
```

## Run

```bash
pip install -r requirements.txt
python main.py
```

Dashboard default URL placeholder: `https://a320737sightings.voloaltro.tech/`.

## Notes

The app still contains some inherited ATLAS/CJ analytics surfaces from the original 525 dashboard. The ingest pipeline and source filters have been adapted so A320/737 records are accepted and JetNet enrichment remains wired in.
