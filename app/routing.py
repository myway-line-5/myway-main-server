from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from .catalog import StationCatalog
from .db import Database


def _load_algorithm_functions(algorithm_src_path: str) -> dict[str, Any]:
    src = str(Path(algorithm_src_path).resolve())
    if src not in sys.path:
        sys.path.insert(0, src)
    from constrained_dijkstra import constrained_dijkstra
    from dijkstra import dijkstra
    from minimum_cost import calculate_fare, minimum_cost
    from minimum_transfer import minimum_transfer

    return {
        "dijkstra": dijkstra,
        "constrained_dijkstra": constrained_dijkstra,
        "minimum_transfer": minimum_transfer,
        "minimum_cost": minimum_cost,
        "calculate_fare": calculate_fare,
    }


class RoutingGraph:
    def __init__(self, stations: dict[str, dict[str, Any]], graph: dict[str, list[dict[str, Any]]]):
        self.stations = stations
        self.graph: dict[str, list[dict[str, Any]]] = defaultdict(list, graph)

    @classmethod
    def load(cls, db: Database, catalog: StationCatalog, algorithm_src_path: str) -> "RoutingGraph":
        stations = {
            s.station_id: {"name": s.name, "line": s.line, "branch": None}
            for s in catalog.all()
        }
        graph: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in db.query_all(
            "SELECT edge_id, from_station_id, to_station_id, seconds, meters, is_transfer "
            "FROM RouteEdge"
        ):
            graph[r["from_station_id"]].append(
                {
                    "to": r["to_station_id"],
                    "time": r["seconds"],
                    "distance": r["meters"],
                    "type": "TRANSFER" if r["is_transfer"] else "RIDE",
                    "transfer": int(r["is_transfer"]),
                    "edge_id": r["edge_id"],
                }
            )
        graph_obj = cls(stations, graph)
        graph_obj.functions = _load_algorithm_functions(algorithm_src_path)
        return graph_obj

    def search(
        self,
        origin_id: str,
        destination_id: str,
        max_duration_seconds: int | None = None,
        max_transfers: int | None = None,
        max_fare_krw: int | None = None,
        optimization: str = "shortest_time",
    ) -> dict[str, Any] | None:
        fns = self.functions
        has_constraints = (
            max_duration_seconds is not None
            or max_transfers is not None
            or max_fare_krw is not None
        )
        if optimization == "minimum_fare":
            result = fns["minimum_cost"](self, origin_id, destination_id)
            if result is not None and max_fare_krw is not None:
                if result.get("total_fare", 0) > max_fare_krw:
                    return None
            return result
        if optimization == "minimum_transfer":
            if has_constraints:
                return {"status": "unsupported"}
            return fns["minimum_transfer"](self, origin_id, destination_id)
        if has_constraints:
            result = fns["constrained_dijkstra"](
                self,
                origin_id,
                destination_id,
                max_time=max_duration_seconds,
                max_transfers=max_transfers,
            )
            if result is not None and max_fare_krw is not None:
                total_meters = sum(int(step.get("distance") or 0) for step in result["route"])
                if fns["calculate_fare"](total_meters) > max_fare_krw:
                    return None
            return result
        result = fns["dijkstra"](self, origin_id, destination_id)
        if result is not None and max_fare_krw is not None:
            total_meters = sum(int(step.get("distance") or 0) for step in result["route"])
            if fns["calculate_fare"](total_meters) > max_fare_krw:
                return None
        return result
