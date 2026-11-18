from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, Depends, Request

from ..catalog import StationCatalog
from ..routing import RoutingGraph
from ..schemas import RouteResultDTO, RouteSearchRequest, RouteSearchResponse, RouteSegmentDTO, RouteType
from ..sessions import SessionState, get_session

router = APIRouter(tags=["routes"])
log = logging.getLogger("myway.routes")


@router.post(
    "/routes/search",
    response_model=RouteSearchResponse,
    summary="경로 탐색 (확정 조건 + 확정 역 기준)",
)
def search_route(
    req: RouteSearchRequest,
    request: Request,
    session: SessionState = Depends(get_session),
) -> RouteSearchResponse:
    request_id = uuid.uuid4().hex
    store = request.app.state.session_store
    catalog: StationCatalog = request.app.state.catalog
    routing: RoutingGraph = request.app.state.routing

    origin_id = req.origin_station_id or session.confirmed_stations.get("origin")
    destination_id = req.destination_station_id or session.confirmed_stations.get("destination")
    if not origin_id:
        return RouteSearchResponse(status="error", error_code="ORIGIN_NOT_CONFIRMED")
    if not destination_id:
        return RouteSearchResponse(status="error", error_code="DESTINATION_NOT_CONFIRMED")

    origin = catalog.get(origin_id)
    destination = catalog.get(destination_id)
    if origin is None or destination is None:
        return RouteSearchResponse(status="error", error_code="STATION_NOT_FOUND")
    if not origin.is_active or not destination.is_active:
        return RouteSearchResponse(status="error", error_code="STATION_INACTIVE")
    if origin_id == destination_id:
        return RouteSearchResponse(status="error", error_code="SAME_STATION")

    store.confirm_conditions(session.session_id)
    confirmed = session.confirmed_condition or {}
    constraints = confirmed.get("constraints") or {}
    preference = confirmed.get("preference")

    max_duration_minutes = constraints.get("max_duration_minutes")
    max_duration_seconds = max_duration_minutes * 60 if isinstance(max_duration_minutes, int) else None
    max_transfers = constraints.get("max_transfers")
    if not isinstance(max_transfers, int):
        max_transfers = None

    optimization = "shortest_time"
    if isinstance(preference, str):
        p = preference.lower()
        if "transfer" in p or "환승" in preference:
            optimization = "minimum_transfer"
        elif "fare" in p or "요금" in preference:
            return RouteSearchResponse(status="error", error_code="UNSUPPORTED_PREFERENCE")

    t0 = time.perf_counter()
    try:
        result = routing.search(
            origin_id,
            destination_id,
            max_duration_seconds=max_duration_seconds,
            max_transfers=max_transfers,
            optimization=optimization,
        )
    except Exception:
        log.exception("route_search request_id=%s status=incomplete", request_id)
        return RouteSearchResponse(status="incomplete", error_code="SEARCH_FAILED")
    elapsed_ms = (time.perf_counter() - t0) * 1000

    if isinstance(result, dict) and result.get("status") == "unsupported":
        return RouteSearchResponse(status="error", error_code="CONSTRAINTS_NOT_SUPPORTED")
    if result is None:
        log.info(
            "route_search request_id=%s status=no_route elapsed_ms=%.0f",
            request_id,
            elapsed_ms,
        )
        return RouteSearchResponse(status="no_route", metrics={"elapsed_ms": round(elapsed_ms, 1)})

    segments: list[RouteSegmentDTO] = []
    total_meters = 0
    transfer_count = 0
    for step in result["route"]:
        from_station = catalog.get(step["from"])
        to_station = catalog.get(step["to"])
        if from_station is None or to_station is None:
            return RouteSearchResponse(status="incomplete", error_code="SEARCH_FAILED")
        is_transfer = bool(step.get("transfer"))
        transfer_count += int(step.get("transfer") or 0)
        total_meters += int(step.get("distance") or 0)
        segments.append(
            RouteSegmentDTO(
                from_station=from_station,
                to_station=to_station,
                line_name=from_station.line,
                seconds=int(step["time"]),
                meters=int(step.get("distance") or 0),
                is_transfer=is_transfer,
            )
        )

    route = RouteResultDTO(
        type=RouteType.MIN_TRANSFERS if optimization == "minimum_transfer" else RouteType.SHORTEST_TIME,
        segments=segments,
        total_seconds=int(result["total_time"]),
        total_meters=total_meters,
        total_fare_krw=None,
        transfer_count=transfer_count,
        fare_version=None,
    )
    log.info(
        "route_search request_id=%s status=ok elapsed_ms=%.0f segments=%d transfers=%d within_1s=%s",
        request_id,
        elapsed_ms,
        len(segments),
        transfer_count,
        elapsed_ms < 1000,
    )
    return RouteSearchResponse(
        status="ok",
        route=route,
        metrics={"elapsed_ms": round(elapsed_ms, 1)},
    )
