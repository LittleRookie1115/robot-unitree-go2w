import math

import numpy as np
from pypcd4 import PointCloud


def evaluate_corridor(distance, point_count, stop_distance):
    if point_count <= 0 or distance is None:
        return None, False
    distance = float(distance)
    if not math.isfinite(distance) or distance <= 0:
        raise RuntimeError("LIDAR_INVALID: forward distance is invalid")
    return distance, distance > stop_distance


def summarize_point_cloud(cloud, guard):
    if cloud.is_bigendian:
        raise RuntimeError("LIDAR_INVALID: big-endian PointCloud2 is unsupported")
    point_count = cloud.width * cloud.height
    if (point_count <= 0 or cloud.point_step <= 0
            or cloud.row_step != cloud.width * cloud.point_step
            or len(cloud.data) != cloud.row_step * cloud.height):
        raise RuntimeError("LIDAR_INVALID: PointCloud2 dimensions are inconsistent")
    fields = {field.name: field for field in cloud.fields}
    for name in ("x", "y", "z"):
        field = fields.get(name)
        if field is None or field.datatype not in (7, 8) or field.count != 1:
            raise RuntimeError(f"LIDAR_INVALID: PointCloud2 {name} field is invalid")

    points = PointCloud.from_msg(cloud).pc_data
    xyz = np.column_stack((points["x"], points["y"], points["z"]))
    valid = np.isfinite(xyz).all(axis=1)
    valid &= np.any(xyz != 0.0, axis=1)
    xyz = xyz[valid]
    valid_point_count = int(xyz.shape[0])
    if valid_point_count <= 0:
        raise RuntimeError("LIDAR_INVALID: PointCloud2 has no valid points")

    corridor = (
        (xyz[:, 0] >= guard.min_forward_m)
        & (xyz[:, 0] <= guard.max_forward_m)
        & (np.abs(xyz[:, 1]) <= guard.half_width_m)
        & (xyz[:, 2] >= guard.min_height_m)
        & (xyz[:, 2] <= guard.max_height_m)
    )
    corridor_points = xyz[corridor]
    corridor_point_count = int(corridor_points.shape[0])
    distance = None
    if corridor_point_count:
        distance = float(np.hypot(corridor_points[:, 0], corridor_points[:, 1]).min())
    return valid_point_count, corridor_point_count, distance
