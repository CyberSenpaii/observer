#!/usr/bin/env python3
import argparse
import getpass
import json
import math
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

WGS84_MU_KM3_S2 = 398600.4418
EARTH_RADIUS_KM = 6378.137
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


def fmt_decimal(value, places=8):
    if value is None:
        return ""
    q = Decimal(str(value))
    s = f"{q:.{places}f}"
    s = s.rstrip("0").rstrip(".")
    if s == "-0":
        s = "0"
    return s


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date '{value}', expected YYYY-MM-DD"
        ) from exc


def parse_time_window(value: str):
    m = re.fullmatch(r"(\d{2})(\d{2})-(\d{2})(\d{2})", value)
    if not m:
        raise argparse.ArgumentTypeError(
            "time window must be HHMM-HHMM, e.g. 0900-1300"
        )
    sh, sm, eh, em = map(int, m.groups())
    if sh > 23 or eh > 23 or sm > 59 or em > 59:
        raise argparse.ArgumentTypeError("invalid HHMM-HHMM time window")
    return time(sh, sm), time(eh, em)


def parse_datetime_utc(value: str) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid datetime '{value}', expected ISO 8601 like 2026-05-29T00:00:00Z"
        ) from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def parse_period(value: str) -> timedelta:
    m = re.fullmatch(r"(?i)\s*(\d+)\s*([hd])\s*", value)
    if not m:
        raise argparse.ArgumentTypeError("period must look like 6h, 24h, or 3d")
    amount = int(m.group(1))
    unit = m.group(2).lower()

    if amount <= 0:
        raise argparse.ArgumentTypeError("period must be greater than 0")

    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)

    raise argparse.ArgumentTypeError("period unit must be h or d")


def parse_csv_filters(value: str):
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def validate_args(args):
    if not -90 <= args.lat <= 90:
        raise SystemExit("--lat must be between -90 and 90")
    if not -180 <= args.lon <= 180:
        raise SystemExit("--lon must be between -180 and 180")
    if args.limit < 1:
        raise SystemExit("--limit must be >= 1")
    if args.min_elevation_deg < 0 or args.min_elevation_deg > 90:
        raise SystemExit("--min-elevation-deg must be between 0 and 90")

    has_date = args.date is not None
    has_range = args.start_date is not None or args.end_date is not None
    has_interval = args.start_datetime is not None or args.period is not None

    if has_interval and (has_date or has_range):
        raise SystemExit(
            "use either --start-datetime/--period or --date/--start-date/--end-date, not both"
        )

    if args.start_datetime and not args.period:
        raise SystemExit("--start-datetime requires --period")
    if args.period and not args.start_datetime:
        raise SystemExit("--period requires --start-datetime")

    if not has_interval:
        if has_date and has_range:
            raise SystemExit("use either --date or --start-date/--end-date, not both")
        if args.start_date and not args.end_date:
            raise SystemExit("--start-date requires --end-date")
        if args.end_date and not args.start_date:
            raise SystemExit("--end-date requires --start-date")
        if args.start_date and args.end_date and args.end_date < args.start_date:
            raise SystemExit("--end-date must be on or after --start-date")
        if not has_date and not (args.start_date and args.end_date):
            raise SystemExit(
                "provide either --date, --start-date/--end-date, or --start-datetime with --period"
            )


def prompt_spacetrack_credentials(identity_arg, password_arg):
    identity = identity_arg or os.getenv("SPACETRACK_IDENTITY")
    password = password_arg or os.getenv("SPACETRACK_PASSWORD")
    if not identity:
        identity = input("Space-Track identity: ").strip()
    if not password:
        password = getpass.getpass("Space-Track password: ")

    if not identity or not password:
        raise SystemExit("Space-Track credentials are required")

    return identity, password


def load_requests():
    try:
        import requests
    except ImportError as exc:
        raise SystemExit(
            "missing dependency 'requests'. Install with: pip install requests"
        ) from exc
    return requests


def load_sgp4_tools():
    try:
        from sgp4.api import Satrec, jday
    except ImportError as exc:
        raise SystemExit("missing dependency 'sgp4'. Install with: pip install sgp4") from exc
    return jday, Satrec, None


def login_spacetrack(session, identity, password):
    url = "https://www.space-track.org/ajaxauth/login"
    resp = session.post(
        url,
        data={"identity": identity, "password": password},
        timeout=60,
        headers=BROWSER_HEADERS,
    )
    if resp.status_code != 200:
        raise SystemExit(f"Space-Track login failed: HTTP {resp.status_code}")
    if "Failed" in resp.text or "incorrect" in resp.text.lower():
        raise SystemExit("Space-Track login failed: invalid credentials")


def fetch_gp_rows(identity, password):
    requests = load_requests()
    with requests.Session() as session:
        login_spacetrack(session, identity, password)
        url = (
            "https://www.space-track.org/basicspacedata/query/"
            "class/gp/decay_date/null-val/epoch/%3Enow-30/orderby/norad_cat_id/format/json"
        )
        resp = session.get(url, timeout=180, headers=BROWSER_HEADERS)
        if resp.status_code != 200:
            raise SystemExit(f"Space-Track GP fetch failed: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise SystemExit("failed to decode GP JSON from Space-Track") from exc
    return data


def gp_to_satrec(gp_record):
    _, Satrec, _ = load_sgp4_tools()
    tle1 = gp_record.get("TLE_LINE1")
    tle2 = gp_record.get("TLE_LINE2")
    if not tle1 or not tle2:
        return None
    try:
        return Satrec.twoline2rv(tle1, tle2)
    except Exception:
        return None


def julian_date(dt_utc):
    jday, _, _ = load_sgp4_tools()
    return jday(
        dt_utc.year,
        dt_utc.month,
        dt_utc.day,
        dt_utc.hour,
        dt_utc.minute,
        dt_utc.second + dt_utc.microsecond / 1e6,
    )


def gmst_from_datetime(dt_utc):
    jd, fr = julian_date(dt_utc)
    jd_total = jd + fr
    t = (jd_total - 2451545.0) / 36525.0
    gmst_deg = (
        280.46061837
        + 360.98564736629 * (jd_total - 2451545.0)
        + 0.000387933 * t * t
        - (t * t * t) / 38710000.0
    )
    return math.radians(gmst_deg % 360.0)


def eci_to_ecef(r_eci_km, dt_utc):
    theta = gmst_from_datetime(dt_utc)
    x, y, z = r_eci_km
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    x_ecef = cos_t * x + sin_t * y
    y_ecef = -sin_t * x + cos_t * y
    z_ecef = z
    return (x_ecef, y_ecef, z_ecef)


def geodetic_to_ecef(lat_deg, lon_deg, alt_km):
    a = 6378.137
    f = 1.0 / 298.257223563
    e2 = f * (2 - f)

    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)

    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    cos_lon = math.cos(lon)
    sin_lon = math.sin(lon)

    n = a / math.sqrt(1 - e2 * sin_lat * sin_lat)
    x = (n + alt_km) * cos_lat * cos_lon
    y = (n + alt_km) * cos_lat * sin_lon
    z = (n * (1 - e2) + alt_km) * sin_lat
    return (x, y, z)


def elevation_from_ecef(sat_ecef_km, obs_lat_deg, obs_lon_deg, obs_alt_km):
    obs_ecef = geodetic_to_ecef(obs_lat_deg, obs_lon_deg, obs_alt_km)
    rx = sat_ecef_km[0] - obs_ecef[0]
    ry = sat_ecef_km[1] - obs_ecef[1]
    rz = sat_ecef_km[2] - obs_ecef[2]

    lat = math.radians(obs_lat_deg)
    lon = math.radians(obs_lon_deg)
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    sin_lon = math.sin(lon)
    cos_lon = math.cos(lon)

    east = -sin_lon * rx + cos_lon * ry
    north = -sin_lat * cos_lon * rx - sin_lat * sin_lon * ry + cos_lat * rz
    up = cos_lat * cos_lon * rx + cos_lat * sin_lon * ry + sin_lat * rz

    horiz = math.sqrt(east * east + north * north)
    elev_rad = math.atan2(up, horiz)
    return math.degrees(elev_rad)


def satellite_altitude_km(satrec):
    mean_motion_rev_per_day = satrec.no_kozai * 1440.0 / (2.0 * math.pi)
    if mean_motion_rev_per_day <= 0:
        return None
    n_rad_s = mean_motion_rev_per_day * 2.0 * math.pi / 86400.0
    semi_major_axis_km = (WGS84_MU_KM3_S2 / (n_rad_s ** 2)) ** (1.0 / 3.0)
    return semi_major_axis_km - EARTH_RADIUS_KM


def orbital_period_minutes_from_mean_motion(mean_motion_rev_per_day):
    if mean_motion_rev_per_day <= 0:
        return None
    return 1440.0 / mean_motion_rev_per_day


def parse_tle_fields(tle1, tle2):
    line1 = tle1.rstrip()
    line2 = tle2.rstrip()

    epoch_field = line1[18:32].strip()
    epoch_year = int(epoch_field[:2])
    epoch_year += 2000 if epoch_year < 57 else 1900
    day_fraction = float(epoch_field[2:])

    mm_deriv_1 = float(line1[33:43].strip() or "0")

    mm_deriv_2_raw = line1[44:52]
    bstar_raw = line1[53:61]

    def parse_tle_exp(field):
        field = field.strip()
        if not field:
            return 0.0
        sign = -1 if field[0] == "-" else 1
        mant = field[1:6] if field[0] in "+-" else field[0:5]
        exp = field[6:8] if field[0] in "+-" else field[5:7]
        mantissa = float(f"0.{mant}")
        exponent = int(exp)
        return sign * mantissa * (10 ** exponent)

    mm_deriv_2 = parse_tle_exp(mm_deriv_2_raw)
    bstar = parse_tle_exp(bstar_raw)

    inclination = float(line2[8:16].strip())
    raan = float(line2[17:25].strip())
    eccentricity = float(f"0.{line2[26:33].strip()}")
    arg_perigee = float(line2[34:42].strip())
    mean_anomaly = float(line2[43:51].strip())
    mean_motion = float(line2[52:63].strip())
    revolution_number = int(line2[63:68].strip())

    return {
        "year": epoch_year,
        "day_fraction": day_fraction,
        "mean_motion_derivative_1": mm_deriv_1,
        "mean_motion_derivative_2": mm_deriv_2,
        "bstar": bstar,
        "inclination": inclination,
        "raan": raan,
        "eccentricity": eccentricity,
        "arg_perigee": arg_perigee,
        "mean_anomaly": mean_anomaly,
        "mean_motion": mean_motion,
        "period_minutes": orbital_period_minutes_from_mean_motion(mean_motion),
        "revolution_number": revolution_number,
    }


def daterange(start_date, end_date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def refine_crossing_time(sat, lat, lon, alt_km, t1, t2, target_elev_deg=0.0, iterations=18):
    left = t1
    right = t2

    for _ in range(iterations):
        mid = left + (right - left) / 2
        jd, fr = julian_date(mid)
        err, r, _v = sat.sgp4(jd, fr)
        if err != 0:
            break
        elev = elevation_from_ecef(eci_to_ecef(r, mid), lat, lon, alt_km)

        jd_left, fr_left = julian_date(left)
        err_left, r_left, _ = sat.sgp4(jd_left, fr_left)
        if err_left != 0:
            break
        elev_left = elevation_from_ecef(eci_to_ecef(r_left, left), lat, lon, alt_km)

        if (elev_left - target_elev_deg) * (elev - target_elev_deg) <= 0:
            right = mid
        else:
            left = mid

    return left + (right - left) / 2


def find_passes_for_window_utc(
    gp_record,
    lat,
    lon,
    elevation_m,
    start_date,
    end_date,
    window_start,
    window_end,
    min_elevation_deg,
    match_mode,
    all_passes=False,
):
    jday, _, _ = load_sgp4_tools()
    sat = gp_to_satrec(gp_record)
    alt_km = elevation_m / 1000.0
    matches = []
    coarse_step = timedelta(seconds=20)

    for current_date in daterange(start_date, end_date):
        search_start_utc = datetime.combine(current_date, window_start, tzinfo=timezone.utc)
        search_end_utc = datetime.combine(current_date, window_end, tzinfo=timezone.utc)
        if search_end_utc <= search_start_utc:
            search_end_utc += timedelta(days=1)

        in_pass = False
        pass_start = None
        peak_elev = -90.0
        peak_time = None
        prev_above = False
        prev_time = None
        current = search_start_utc

        while current <= search_end_utc:
            jd, fr = jday(
                current.year, current.month, current.day,
                current.hour, current.minute,
                current.second + current.microsecond / 1e6,
            )
            err, r, _v = sat.sgp4(jd, fr)
            if err == 0:
                elev = elevation_from_ecef(eci_to_ecef(r, current), lat, lon, alt_km)
                above = elev >= 0.0

                if match_mode == "overlap":
                    if above and not in_pass:
                        in_pass = True
                        if prev_time is not None and prev_above is False:
                            pass_start = refine_crossing_time(
                                sat, lat, lon, alt_km, prev_time, current, 0.0
                            )
                        else:
                            pass_start = current
                        peak_elev = elev
                        peak_time = current

                    elif above and in_pass:
                        if elev > peak_elev:
                            peak_elev = elev
                            peak_time = current

                    elif not above and in_pass:
                        if prev_time is not None:
                            los_time = refine_crossing_time(
                                sat, lat, lon, alt_km, prev_time, current, 0.0
                            )
                        else:
                            los_time = current

                        if peak_elev >= min_elevation_deg:
                            matches.append({
                                "window_start_utc": search_start_utc.isoformat(),
                                "window_end_utc": search_end_utc.isoformat(),
                                "window_date_utc": current_date.isoformat(),
                                "aos_utc": pass_start.isoformat(),
                                "peak_utc": peak_time.isoformat() if peak_time else None,
                                "los_utc": los_time.isoformat(),
                                "max_elevation_deg": round(peak_elev, 2),
                            })
                            if not all_passes:
                                in_pass = False
                                pass_start = None
                                peak_elev = -90.0
                                peak_time = None
                                prev_above = above
                                prev_time = current
                                break

                        in_pass = False
                        pass_start = None
                        peak_elev = -90.0
                        peak_time = None

                else:
                    if above and not prev_above:
                        if prev_time is not None:
                            test_start = refine_crossing_time(
                                sat, lat, lon, alt_km, prev_time, current, 0.0
                            )
                        else:
                            test_start = current

                        test_peak = elev
                        test_peak_time = current
                        inner_prev = current
                        inner = current + coarse_step
                        los_time = None

                        while inner <= search_end_utc:
                            jd2, fr2 = jday(
                                inner.year, inner.month, inner.day,
                                inner.hour, inner.minute,
                                inner.second + inner.microsecond / 1e6,
                            )
                            err2, r2, _ = sat.sgp4(jd2, fr2)
                            if err2 != 0:
                                break

                            elev2 = elevation_from_ecef(
                                eci_to_ecef(r2, inner), lat, lon, alt_km
                            )

                            if elev2 < 0.0:
                                los_time = refine_crossing_time(
                                    sat, lat, lon, alt_km, inner_prev, inner, 0.0
                                )
                                break

                            if elev2 > test_peak:
                                test_peak = elev2
                                test_peak_time = inner

                            inner_prev = inner
                            inner += coarse_step

                        if test_peak >= min_elevation_deg:
                            matches.append({
                                "window_start_utc": search_start_utc.isoformat(),
                                "window_end_utc": search_end_utc.isoformat(),
                                "window_date_utc": current_date.isoformat(),
                                "aos_utc": test_start.isoformat(),
                                "peak_utc": test_peak_time.isoformat(),
                                "los_utc": los_time.isoformat() if los_time else None,
                                "max_elevation_deg": round(test_peak, 2),
                            })
                            if not all_passes:
                                prev_above = False
                                prev_time = inner_prev if los_time else inner
                                break

                        if los_time:
                            current = inner
                            prev_time = inner
                            prev_above = False
                            continue

                prev_above = above
                prev_time = current

            current += coarse_step

        if match_mode == "overlap" and in_pass and peak_elev >= min_elevation_deg:
            matches.append({
                "window_start_utc": search_start_utc.isoformat(),
                "window_end_utc": search_end_utc.isoformat(),
                "window_date_utc": current_date.isoformat(),
                "aos_utc": pass_start.isoformat(),
                "peak_utc": peak_time.isoformat() if peak_time else None,
                "los_utc": None,
                "max_elevation_deg": round(peak_elev, 2),
            })

    return matches


def find_passes_for_interval_utc(
    gp_record,
    lat,
    lon,
    elevation_m,
    start_dt_utc,
    end_dt_utc,
    min_elevation_deg,
    all_passes=False,
):
    jday, _, _ = load_sgp4_tools()
    sat = gp_to_satrec(gp_record)
    alt_km = elevation_m / 1000.0
    matches = []
    coarse_step = timedelta(seconds=20)

    in_pass = False
    pass_start = None
    peak_elev = -90.0
    peak_time = None
    prev_above = False
    prev_time = None
    current = start_dt_utc

    while current <= end_dt_utc:
        jd, fr = jday(
            current.year, current.month, current.day,
            current.hour, current.minute,
            current.second + current.microsecond / 1e6,
        )
        err, r, _v = sat.sgp4(jd, fr)
        if err == 0:
            elev = elevation_from_ecef(eci_to_ecef(r, current), lat, lon, alt_km)
            above = elev >= 0.0

            if above and not in_pass:
                in_pass = True
                if prev_time is not None and prev_above is False:
                    pass_start = refine_crossing_time(
                        sat, lat, lon, alt_km, prev_time, current, 0.0
                    )
                else:
                    pass_start = current
                peak_elev = elev
                peak_time = current

            elif above and in_pass:
                if elev > peak_elev:
                    peak_elev = elev
                    peak_time = current

            elif not above and in_pass:
                if prev_time is not None:
                    los_time = refine_crossing_time(
                        sat, lat, lon, alt_km, prev_time, current, 0.0
                    )
                else:
                    los_time = current

                if peak_elev >= min_elevation_deg:
                    matches.append({
                        "window_start_utc": start_dt_utc.isoformat(),
                        "window_end_utc": end_dt_utc.isoformat(),
                        "window_date_utc": start_dt_utc.date().isoformat(),
                        "aos_utc": pass_start.isoformat(),
                        "peak_utc": peak_time.isoformat() if peak_time else None,
                        "los_utc": los_time.isoformat(),
                        "max_elevation_deg": round(peak_elev, 2),
                    })
                    if not all_passes:
                        in_pass = False
                        pass_start = None
                        peak_elev = -90.0
                        peak_time = None
                        prev_above = above
                        prev_time = current
                        break

                in_pass = False
                pass_start = None
                peak_elev = -90.0
                peak_time = None

            prev_above = above
            prev_time = current

        current += coarse_step

    if in_pass and peak_elev >= min_elevation_deg:
        matches.append({
            "window_start_utc": start_dt_utc.isoformat(),
            "window_end_utc": end_dt_utc.isoformat(),
            "window_date_utc": start_dt_utc.date().isoformat(),
            "aos_utc": pass_start.isoformat(),
            "peak_utc": peak_time.isoformat() if peak_time else None,
            "los_utc": None,
            "max_elevation_deg": round(peak_elev, 2),
        })

    return matches


def classify_regime(alt_km):
    if alt_km is None:
        return "UNKNOWN"
    if alt_km < 2000:
        return "LEO"
    if alt_km < 35786:
        return "MEO"
    if abs(alt_km - 35786) < 2000:
        return "GEO"
    return "HEO"


def normalize_object_type(obj_type):
    if not obj_type:
        return "UNKNOWN"
    return str(obj_type).strip().upper()


def matches_name_filters(name, include_filters, exclude_filters):
    test = (name or "").upper()
    if include_filters:
        if not any(f.upper() in test for f in include_filters):
            return False
    if exclude_filters:
        if any(f.upper() in test for f in exclude_filters):
            return False
    return True


def apply_local_offset(iso_utc_str, offset_hours):
    if not iso_utc_str:
        return None
    dt = datetime.fromisoformat(iso_utc_str)
    local_dt = dt + timedelta(hours=offset_hours)
    return local_dt.strftime("%Y-%m-%d %H:%M:%S")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Observer: satellite pass finder from Space-Track GP/TLE data."
    )
    parser.add_argument("--lat", type=float, required=True, help="Observer latitude in decimal degrees")
    parser.add_argument("--lon", type=float, required=True, help="Observer longitude in decimal degrees")
    parser.add_argument("--elevation-m", type=float, default=0.0, help="Observer elevation in meters")
    parser.add_argument("--regime", default="ALL", choices=["ALL", "LEO", "MEO", "GEO", "HEO"], help="Orbit regime filter")
    parser.add_argument("--date", type=parse_date, help="Single UTC date YYYY-MM-DD")
    parser.add_argument("--start-date", type=parse_date, help="UTC start date YYYY-MM-DD")
    parser.add_argument("--end-date", type=parse_date, help="UTC end date YYYY-MM-DD")
    parser.add_argument("--time-window", type=parse_time_window, default="0000-2359", help="UTC HHMM-HHMM")
    parser.add_argument("--start-datetime", type=parse_datetime_utc, help="UTC ISO 8601 datetime, e.g. 2026-05-29T00:00:00Z")
    parser.add_argument("--period", type=parse_period, help="Continuous search period from start-datetime, e.g. 6h, 24h, 3d")
    parser.add_argument("--min-elevation-deg", type=float, default=0.0, help="Minimum peak elevation required")
    parser.add_argument("--limit", type=int, default=10, help="Limit number of satellites printed")
    parser.add_argument("--output", choices=["text", "json", "both"], default="text", help="Output mode")
    parser.add_argument("--local-offset-hours", type=float, default=0.0, help="Display pass times with a fixed local UTC offset")
    parser.add_argument("--exclude-debris", action="store_true", help="Exclude debris and rocket bodies")
    parser.add_argument("--include-name", type=parse_csv_filters, default=[], help="Comma-separated name substrings to include")
    parser.add_argument("--exclude-name", type=parse_csv_filters, default=[], help="Comma-separated name substrings to exclude")
    parser.add_argument("--satellite", help="Specific satellite name substring to match")
    parser.add_argument("--all-passes", action="store_true", help="Return all qualifying passes per satellite")
    parser.add_argument("--match-mode", choices=["overlap", "aos"], default="overlap", help="Pass matching mode for date-window searches")
    parser.add_argument("--identity", help="Space-Track identity (or use SPACETRACK_IDENTITY)")
    parser.add_argument("--password", help="Space-Track password (or use SPACETRACK_PASSWORD)")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    validate_args(args)

    interval_mode = args.start_datetime is not None and args.period is not None
    if interval_mode:
        interval_start_utc = args.start_datetime.astimezone(timezone.utc)
        interval_end_utc = interval_start_utc + args.period
        window_start = None
        window_end = None
        start_date = None
        end_date = None
    else:
        start_date = args.date if args.date else args.start_date
        end_date = args.date if args.date else args.end_date
        window_start, window_end = args.time_window

    identity, password = prompt_spacetrack_credentials(args.identity, args.password)
    gp_rows = fetch_gp_rows(identity, password)

    print(f"Debug: GP rows fetched from Space-Track: {len(gp_rows)}")
    print(f"Debug: regime filter = {args.regime}")
    print(f"Debug: exclude_debris = {args.exclude_debris}")
    print(f"Debug: include_name filters = {args.include_name}")
    print(f"Debug: exclude_name filters = {args.exclude_name}")
    print(f"Debug: satellite filter = {args.satellite}")
    print(f"Debug: all_passes = {args.all_passes}")
    if interval_mode:
        print(f"Debug: interval_start_utc = {interval_start_utc.isoformat()}")
        print(f"Debug: interval_end_utc = {interval_end_utc.isoformat()}")

    results = []

    for gp in gp_rows:
        sat = gp_to_satrec(gp)
        if sat is None:
            continue

        sat_name = (gp.get("OBJECT_NAME") or gp.get("SATNAME") or "").strip()
        object_type = normalize_object_type(gp.get("OBJECT_TYPE"))
        alt_km = satellite_altitude_km(sat)
        regime = classify_regime(alt_km)

        if args.regime != "ALL" and regime != args.regime:
            continue

        if args.exclude_debris and object_type in {"DEBRIS", "ROCKET BODY", "R/B"}:
            continue

        if args.satellite and args.satellite.upper() not in sat_name.upper():
            continue

        if not matches_name_filters(sat_name, args.include_name, args.exclude_name):
            continue

        if interval_mode:
            matches = find_passes_for_interval_utc(
                gp,
                args.lat,
                args.lon,
                args.elevation_m,
                interval_start_utc,
                interval_end_utc,
                args.min_elevation_deg,
                args.all_passes,
            )
        else:
            matches = find_passes_for_window_utc(
                gp,
                args.lat,
                args.lon,
                args.elevation_m,
                start_date,
                end_date,
                window_start,
                window_end,
                args.min_elevation_deg,
                args.match_mode,
                args.all_passes,
            )

        if not matches:
            continue

        tle1 = gp.get("TLE_LINE1", "")
        tle2 = gp.get("TLE_LINE2", "")
        fields = parse_tle_fields(tle1, tle2)

        for match in matches:
            match["window_start_local"] = apply_local_offset(match.get("window_start_utc"), args.local_offset_hours)
            match["window_end_local"] = apply_local_offset(match.get("window_end_utc"), args.local_offset_hours)
            match["aos_local"] = apply_local_offset(match.get("aos_utc"), args.local_offset_hours)
            match["peak_local"] = apply_local_offset(match.get("peak_utc"), args.local_offset_hours)
            match["los_local"] = apply_local_offset(match.get("los_utc"), args.local_offset_hours)

        results.append({
            "satellite": sat_name,
            "regime": regime,
            "type": object_type,
            "derived_altitude_km": None if alt_km is None else round(alt_km, 2),
            "matches": matches,
            "tle": {"line1": tle1, "line2": tle2},
            "fields": {
                "Year": fields["year"],
                "Day Fraction": float(fmt_decimal(fields["day_fraction"], 8) or 0),
                "Mean motion derivative / 2": float(fmt_decimal(fields["mean_motion_derivative_1"], 8) or 0),
                "Mean motion 2nd derivative / 6": float(fmt_decimal(fields["mean_motion_derivative_2"], 8) or 0),
                "Bstar Drag Coefficient": float(fmt_decimal(fields["bstar"], 8) or 0),
                "Inclination": float(fmt_decimal(fields["inclination"], 8) or 0),
                "RAAN": float(fmt_decimal(fields["raan"], 8) or 0),
                "Eccentricity": float(fmt_decimal(fields["eccentricity"], 8) or 0),
                "Argument of Perigee": float(fmt_decimal(fields["arg_perigee"], 8) or 0),
                "Mean Anomaly": float(fmt_decimal(fields["mean_anomaly"], 8) or 0),
                "Mean motion": float(fmt_decimal(fields["mean_motion"], 8) or 0),
                "Orbital period (min)": float(fmt_decimal(fields["period_minutes"], 8) or 0),
                "Revolution number": fields["revolution_number"],
            },
        })

    results.sort(
        key=lambda item: (
            item["matches"][0]["aos_local"] or item["matches"][0]["aos_utc"] or "",
            item["satellite"],
        )
    )

    print(f"Debug: satellites with at least one qualifying pass: {len(results)}")

    trimmed = results[: args.limit]

    if args.output in {"text", "both"}:
        for idx, item in enumerate(trimmed, start=1):
            print(f"[{idx}] Satellite: {item['satellite']}")
            print(
                f"Regime: {item['regime']} | Type: {item['type']} | "
                f"Derived altitude km: {fmt_decimal(item['derived_altitude_km'], 2)}"
            )
            print(f"Matches: {len(item['matches'])}")
            for m in item["matches"]:
                print(
                    f" - window_start_utc={m['window_start_utc']} "
                    f"window_end_utc={m['window_end_utc']} "
                    f"window_date_utc={m['window_date_utc']}"
                )
                print(
                    f"   aos_utc={m['aos_utc']} peak_utc={m['peak_utc']} "
                    f"los_utc={m['los_utc']}"
                )
                print(
                    f"   window_start_local={m['window_start_local']} "
                    f"window_end_local={m['window_end_local']}"
                )
                print(
                    f"   aos_local={m['aos_local']} peak_local={m['peak_local']} "
                    f"los_local={m['los_local']} "
                    f"max_elevation_deg={fmt_decimal(m['max_elevation_deg'], 2)}"
                )

            print("TLE:")
            print(item["tle"]["line1"])
            print(item["tle"]["line2"])
            print("Fields:")
            for k, v in item["fields"].items():
                if isinstance(v, float):
                    print(f" {k}: {fmt_decimal(v, 8)}")
                else:
                    print(f" {k}: {v}")
            print()

    if args.output in {"json", "both"}:
        print(json.dumps(trimmed, indent=2))


if __name__ == "__main__":
    main()
