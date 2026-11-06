"""Python 3.8-compatible freshness checks shared by the native observer/tests."""
import math
import time


def freshness(record, now_wall, now_mono, max_source_age, max_future, max_receipt_age,
              require_stamp=True):
    if not record:
        return False, "no_sample"
    if now_mono - record["received_monotonic"] > max_receipt_age:
        return False, "receipt_stale"
    if not require_stamp:
        return True, "receipt_only"
    stamp = record.get("source_stamp", 0)
    if not math.isfinite(stamp) or stamp <= 0:
        return False, "source_stamp_missing"
    age = now_wall - stamp
    if age > max_source_age:
        return False, "source_stale"
    if age < -max_future:
        return False, "source_future"
    if not record.get("frame_id"):
        return False, "frame_missing"
    if "quaternion" in record:
        q = record["quaternion"]
        if not all(math.isfinite(value) for value in q) or abs(
                sum(value * value for value in q) - 1.0) > 0.1:
            return False, "invalid_orientation"
    return True, "fresh"


def unknown_reading(name, **values):
    reading = dict(name=name, temp_c=-1.0, voltage=-1.0, current_a=-1.0,
                   battery_percent=-1.0)
    reading.update(values)
    return reading


def publish_component(readings, component, healthy, stale, **fields):
    if fields:
        readings.append(unknown_reading(component, **fields))
    readings.append(unknown_reading(component + "/online", current_a=float(bool(healthy))))
    readings.append(unknown_reading(component + "/stale", current_a=float(bool(stale))))


# Go2-W LowState motors 0..11 are the leg joints. Motors 12..15 follow the
# same FL, FR, RL, RR order as those legs and as the official wheel joints.
# These match both Soma and the released Client. A part literally named "body"
# must not sit between them; that Client would collapse it back to the root.
WHEEL_COMPONENTS = (
    "body/FL_foot",
    "body/FR_foot",
    "body/RL_foot",
    "body/RR_foot",
)


def build_health(records, config, now_wall=None, now_mono=None):
    now_wall = time.time() if now_wall is None else now_wall
    now_mono = time.monotonic() if now_mono is None else now_mono
    readings = []
    low = records.get("lowstate", {})
    low_fresh, low_reason = freshness(low, now_wall, now_mono, config["max_source_age_s"],
                             config["max_source_future_s"], config["max_receipt_age_s"],
                             require_stamp=False)
    voltage = low.get("voltage", -1.0) if low_fresh else -1.0
    low_stale = low_reason in ("source_stale", "receipt_stale")
    publish_component(readings, "body/base", low_fresh, low_stale)
    publish_component(
        readings, "body/battery", low_fresh, low_stale,
        voltage=voltage,
        battery_percent=low.get("battery_percent", -1.0) if low_fresh else -1.0,
    )
    for key, component in [
        ("lidar", "body/onboard_lidar"),
        ("imu", "body/onboard_imu"),
        ("camera", "body/front_camera"),
    ]:
        healthy, reason = freshness(
            records.get(key, {}), now_wall, now_mono, config["max_source_age_s"],
            config["max_source_future_s"], config["max_receipt_age_s"],
        )
        publish_component(
            readings, component, healthy,
            reason in ("source_stale", "receipt_stale"),
        )
    temperatures = low.get("motor_temperatures_c") or []
    # Fewer than 16 motors means this frame cannot identify the wheels. Leave
    # them unobserved instead of reporting a communication fault.
    if len(temperatures) >= 16:
        stale = (not low_fresh) and low_reason in ("source_stale", "receipt_stale")
        for component, temp_c in zip(WHEEL_COMPONENTS, temperatures[12:16]):
            fields = {"temp_c": float(temp_c)} if low_fresh else {}
            publish_component(readings, component, low_fresh, stale, **fields)
            if low_fresh:
                readings.append(unknown_reading(
                    component + "/communication_ok", current_a=1.0,
                ))
    return {"voltage": voltage, "charging": False, "remaining_s": -1,
            "readings": readings}
