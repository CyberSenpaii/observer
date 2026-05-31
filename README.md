# observer
# Satellite discovery
```python observer.py --lat 25.5138953 --lon 184.7930245 --elevation-m 10 --regime LEO --time-window 0000-2359 --start-date 2026-05-29 --end-date 2026-05-29 --min-elevation-deg 20 --limit 5 --output both --local-offset-hours 12 --exclude-debris --exclude-name COSMOS```

# Identify pass windows for a given sat by name
```python observer.py --lat 25.5138953 --lon 184.7930245 --elevation-m 10 --start-datetime 2026-05-29T00:00:00Z --period 12h --satellite "NOAA 7" --min-elevation-deg 10 --output both --local-offset-hours 12 --all-passes --exclude-debris```
# Can use h (hours) or d (days) 
```python observer.py --lat 25.5138953 --lon 184.7930245 --elevation-m 10 --start-datetime 2026-05-29T00:00:00Z --period 1d --satellite "NOAA 7" --min-elevation-deg 10 --output both --local-offset-hours 12 --all-passes --exclude-debris```
