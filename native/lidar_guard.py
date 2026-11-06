import math


def summarize_lidar_points(points, guard, stride):
    selected = []
    valid_point_count = 0
    corridor_point_count = 0
    nearest_corridor_distance_m = None
    for index, point in enumerate(points):
        x, y, z = (float(value) for value in point)
        if not all(math.isfinite(value) for value in (x, y, z)):
            continue
        if x == 0.0 and y == 0.0 and z == 0.0:
            continue
        valid_point_count += 1
        if index % stride == 0:
            selected.append([x, y, z])
        if (guard["min_forward_m"] <= x <= guard["max_forward_m"]
                and abs(y) <= guard["half_width_m"]
                and guard["min_height_m"] <= z <= guard["max_height_m"]):
            # The sensor tilts down, so the floor reaches the corridor about 1.6 m ahead.
            plane = guard.get("ground_plane")
            if plane and (plane[0] * x + plane[1] * y + plane[2] * z + plane[3]
                          < guard["min_ground_clearance_m"]):
                continue
            corridor_point_count += 1
            distance = math.hypot(x, y)
            if nearest_corridor_distance_m is None or distance < nearest_corridor_distance_m:
                nearest_corridor_distance_m = distance
    return {
        "points": selected[:1000],
        "valid_point_count": valid_point_count,
        "corridor_point_count": corridor_point_count,
        "nearest_corridor_distance_m": nearest_corridor_distance_m,
    }
