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


def geo_to_cartesian(ref_coord: Tuple[float, float, float], target_coord: Tuple[float, float, float]) -> Position:
    # Function to convert geographical coordinates to Cartesian coordinates
    # ref_coord is the reference point (latitude, longitude)
    # target_coord is the target point (latitude, longitude)

    # Calculate distances
    # NEU convention: x=North, y=East, z=Up
    dx = _haversine_distance((ref_coord[0], ref_coord[1]), (target_coord[0], ref_coord[1]))  # North
    dy = _haversine_distance((ref_coord[0], ref_coord[1]), (ref_coord[0], target_coord[1]))  # East

    x = dx if target_coord[0] >= ref_coord[0] else -dx  # North
    y = dy if target_coord[1] >= ref_coord[1] else -dy  # East
    z = target_coord[2] - ref_coord[2]                 # Up

    return x, y, z


def cartesian_to_geo(ref_coord: Tuple[float, float, float], target_coord: Tuple[float, float, float]) -> Tuple[float, float, float]:
    # Function to convert Cartesian coordinates back to geographical coordinates
    # ref_coord is the reference point (latitude, longitude)
    # target_coord is the target point (x, y, z)

    # Calculate latitude and longitude offsets
    dlat = target_coord[0] / 111320  # Approximate conversion from meters to degrees latitude
    dlon = target_coord[1] / (111320 * math.cos(math.radians(ref_coord[0])))  # Approximate conversion from meters to degrees longitude

    # Calculate geographical coordinates
    lat = ref_coord[0] + dlat
    lon = ref_coord[1] + dlon
    alt = ref_coord[2] + target_coord[2]

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
