"""模板与延误推演请求的输入校验，并产出规范化（canonical）表示。

规范化规则：提示点按 ID 升序保存；关系按 (from, to, min_gap) 升序保存。
推演算法本身对顺序不敏感，规范化只是让同一批规则落库内容一致、便于核对。
"""

from __future__ import annotations

from .errors import AppError, INVALID_TEMPLATE, INVALID_DELAY

MAX_POINTS = 300
MAX_RELATIONS = 3000
MIN_RELEASE = 0
MAX_RELEASE = 10 ** 9
MAX_MIN_GAP = 10 ** 6
MAX_ID_LEN = 200


def _is_plain_int(value) -> bool:
    """接受真正的 int，但拒绝 bool（bool 是 int 的子类）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_non_empty_str(value) -> bool:
    return isinstance(value, str) and len(value) > 0


def validate_template(payload):
    """校验登记请求体，返回规范化后的模板 dict。

    任何不合法情形都抛 ``AppError(invalid_template, ...)``，绝不返回半截结果。
    """
    if not isinstance(payload, dict):
        raise AppError(INVALID_TEMPLATE,
                       "Template must be a JSON object.", status_code=400)

    allowed_top = {"points", "relations"}
    extra = set(payload) - allowed_top
    if extra:
        raise AppError(
            INVALID_TEMPLATE,
            f"Unknown top-level field(s): {sorted(extra)}.",
            status_code=400,
            details={"unexpected_fields": sorted(extra)},
        )

    raw_points = payload.get("points")
    if not isinstance(raw_points, list):
        raise AppError(INVALID_TEMPLATE,
                       "Field 'points' is required and must be an array.",
                       status_code=400)

    if not (1 <= len(raw_points) <= MAX_POINTS):
        raise AppError(
            INVALID_TEMPLATE,
            f"'points' must contain between 1 and {MAX_POINTS} items.",
            status_code=400,
            details={"count": len(raw_points)
                     if isinstance(raw_points, list) else None},
        )

    points = []
    seen_ids = set()
    for index, point in enumerate(raw_points):
        where = f"points[{index}]"
        if not isinstance(point, dict):
            raise AppError(INVALID_TEMPLATE,
                           f"{where} must be an object.", status_code=400)

        extra_fields = set(point) - {"id", "release", "latest"}
        if extra_fields:
            raise AppError(
                INVALID_TEMPLATE,
                f"{where}: unknown field(s) {sorted(extra_fields)}.",
                status_code=400,
                details={"path": where,
                         "unexpected_fields": sorted(extra_fields)},
            )

        pid = point.get("id")
        if not _is_non_empty_str(pid):
            raise AppError(
                INVALID_TEMPLATE,
                f"{where}.id must be a non-empty string.",
                status_code=400, details={"path": f"{where}.id"})
        if len(pid) > MAX_ID_LEN:
            raise AppError(
                INVALID_TEMPLATE,
                f"{where}.id is longer than {MAX_ID_LEN} characters.",
                status_code=400, details={"path": f"{where}.id"})
        if pid in seen_ids:
            raise AppError(
                INVALID_TEMPLATE,
                f"Duplicate point id: {pid!r}.",
                status_code=400, details={"path": where, "id": pid})
        seen_ids.add(pid)

        release = point.get("release")
        if not _is_plain_int(release) or not (MIN_RELEASE <= release <= MAX_RELEASE):
            raise AppError(
                INVALID_TEMPLATE,
                f"{where}.release must be an integer in "
                f"[{MIN_RELEASE}, {MAX_RELEASE}].",
                status_code=400, details={"path": f"{where}.release"})

        latest = point.get("latest")
        if latest is not None:
            if not _is_plain_int(latest) or latest < release:
                raise AppError(
                    INVALID_TEMPLATE,
                    f"{where}.latest must be an integer no less than release.",
                    status_code=400, details={"path": f"{where}.latest"})

        points.append({"id": pid, "release": release, "latest": latest})

    raw_relations = payload.get("relations", [])
    if not isinstance(raw_relations, list):
        raise AppError(INVALID_TEMPLATE,
                       "Field 'relations' must be an array when present.",
                       status_code=400)

    if len(raw_relations) > MAX_RELATIONS:
        raise AppError(
            INVALID_TEMPLATE,
            f"'relations' may contain at most {MAX_RELATIONS} items.",
            status_code=400, details={"count": len(raw_relations)})

    relations = []
    for index, rel in enumerate(raw_relations):
        where = f"relations[{index}]"
        if not isinstance(rel, dict):
            raise AppError(INVALID_TEMPLATE,
                           f"{where} must be an object.", status_code=400)

        extra_fields = set(rel) - {"from", "to", "min_gap"}
        if extra_fields:
            raise AppError(
                INVALID_TEMPLATE,
                f"{where}: unknown field(s) {sorted(extra_fields)}.",
                status_code=400,
                details={"path": where,
                         "unexpected_fields": sorted(extra_fields)},
            )

        source = rel.get("from")
        target = rel.get("to")
        gap = rel.get("min_gap")

        if not _is_non_empty_str(source):
            raise AppError(
                INVALID_TEMPLATE,
                f"{where}.from must be a non-empty string.",
                status_code=400, details={"path": f"{where}.from"})
        if not _is_non_empty_str(target):
            raise AppError(
                INVALID_TEMPLATE,
                f"{where}.to must be a non-empty string.",
                status_code=400, details={"path": f"{where}.to"})
        if source not in seen_ids:
            raise AppError(
                INVALID_TEMPLATE,
                f"{where}.from references unknown point id {source!r}.",
                status_code=400,
                details={"path": f"{where}.from", "id": source})
        if target not in seen_ids:
            raise AppError(
                INVALID_TEMPLATE,
                f"{where}.to references unknown point id {target!r}.",
                status_code=400,
                details={"path": f"{where}.to", "id": target})

        if not _is_plain_int(gap) or not (0 <= gap <= MAX_MIN_GAP):
            raise AppError(
                INVALID_TEMPLATE,
                f"{where}.min_gap must be an integer in [0, {MAX_MIN_GAP}].",
                status_code=400, details={"path": f"{where}.min_gap"})

        relations.append({"from": source, "to": target, "min_gap": gap})

    # 规范化：点与关系都用确定顺序保存。
    points.sort(key=lambda p: p["id"])
    relations.sort(key=lambda r: (r["from"], r["to"], r["min_gap"]))

    return {"points": points, "relations": relations}


def validate_delay(payload, canonical_template):
    """校验推演请求中的 delay 覆盖，返回规范化后的 delay dict。"""
    if not isinstance(payload, dict):
        raise AppError(INVALID_DELAY, "Delay override must be a JSON object.",
                       status_code=400)

    allowed_top = {"template_id", "delay", "explain"}
    extra = set(payload) - allowed_top
    if extra:
        raise AppError(
            INVALID_DELAY,
            f"Unknown top-level field(s): {sorted(extra)}.",
            status_code=400,
            details={"unexpected_fields": sorted(extra)},
        )

    explain = payload.get("explain", False)
    if not isinstance(explain, bool):
        raise AppError(
            INVALID_DELAY,
            "Field 'explain' must be a boolean when present.",
            status_code=400, details={"path": "explain"})

    raw_delay = payload.get("delay", {})
    if not isinstance(raw_delay, dict):
        raise AppError(INVALID_DELAY,
                       "Field 'delay' must be an object mapping id to integer.",
                       status_code=400)

    release_by_id = {p["id"]: p["release"] for p in canonical_template["points"]}
    delay = {}
    for key, value in raw_delay.items():
        if not _is_non_empty_str(key) or key not in release_by_id:
            raise AppError(
                INVALID_DELAY,
                f"delay key {key!r} is not an existing point id.",
                status_code=400, details={"id": key})
        if not _is_plain_int(value) or value < 0:
            raise AppError(
                INVALID_DELAY,
                f"delay[{key!r}] must be a non-negative integer.",
                status_code=400, details={"id": key})
        if value < release_by_id[key]:
            raise AppError(
                INVALID_DELAY,
                f"delay[{key!r}] must be no less than the original release "
                f"({release_by_id[key]}).",
                status_code=400,
                details={"id": key, "release": release_by_id[key],
                         "delay": value})
        delay[key] = value

    return delay
