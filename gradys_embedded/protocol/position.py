"""
This module contains some helpers useful when working with positions. Positions are used to localize
the nodes inside the python simulation.
"""
import math
from typing import Tuple

Position = Tuple[float, float, float]
"""
Represents a node's position inside the simulation. It is a tuple of three floating point numbers
representing the euclidean coordinates of the node.
"""


def _haversine_distance(coord1, coord2):
    # Function to calculate haversine distance between two coordinates
    R = 6371000  # Earth radius in meters

    lat1, lon1 = math.radians(coord1[0]), math.radians(coord1[1])
    lat2, lon2 = math.radians(coord2[0]), math.radians(coord2[1])

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    distance = R * c
    return distance


def geo_to_cartesian(ref_coord: Tuple[float, float, float], target_coord: Tuple[float, float, float], x_axis_degrees: float = 0.0) -> Position:
    # ref_coord is the reference point (latitude, longitude, altitude)
    # target_coord is the target point (latitude, longitude, altitude)
    # x_axis_degrees rotates the protocol frame clockwise from true north

    dn = _haversine_distance((ref_coord[0], ref_coord[1]), (target_coord[0], ref_coord[1]))
    de = _haversine_distance((ref_coord[0], ref_coord[1]), (ref_coord[0], target_coord[1]))

    north = dn if target_coord[0] >= ref_coord[0] else -dn
    east = de if target_coord[1] >= ref_coord[1] else -de
    z = target_coord[2] - ref_coord[2]

    theta = math.radians(x_axis_degrees)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    x = north * cos_t + east * sin_t
    y = -north * sin_t + east * cos_t

    return x, y, z


def cartesian_to_geo(ref_coord: Tuple[float, float, float], target_coord: Tuple[float, float, float], x_axis_degrees: float = 0.0) -> Tuple[float, float, float]:
    # ref_coord is the reference point (latitude, longitude, altitude)
    # target_coord is the target point (x, y, z) in the rotated protocol frame
    # x_axis_degrees rotates the protocol frame clockwise from true north

    x, y, z = target_coord
    theta = math.radians(x_axis_degrees)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    north = x * cos_t - y * sin_t
    east = x * sin_t + y * cos_t

    dlat = north / 111320
    dlon = east / (111320 * math.cos(math.radians(ref_coord[0])))

    lat = ref_coord[0] + dlat
    lon = ref_coord[1] + dlon
    alt = ref_coord[2] + z

    return lat, lon, alt


def squared_distance(start: Position, end: Position) -> float:
    """
    Calculates the squared distance between two positions.

    Args:
        start: First position
        end: Second position

    Returns:
        The distance squared
    """
    return (end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2 + (end[2] - start[2]) ** 2
