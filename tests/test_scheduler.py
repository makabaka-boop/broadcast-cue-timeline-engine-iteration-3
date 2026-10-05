"""算法单测：锁定分支汇合、乱序输入、延误传播、零间隔环、正权环等行为。"""

from __future__ import annotations

import random

from app.scheduler import (STATUS_DEADLINE_EXCEEDED, STATUS_OK,
                           STATUS_POSITIVE_CYCLE, earliest_schedule)


def pt(pid, release, latest=None):
    return {"id": pid, "release": release, "latest": latest}


def rel(u, v, gap):
    return {"from": u, "to": v, "min_gap": gap}


# ---- 分支汇合 -------------------------------------------------------------

def test_branch_reconvergence_uses_latest_predecessor():
    # 两条分支在 z 汇合：
    #   a=0 -> x>=10 -> z>=x+10=20；a=0 -> y>=3 -> z>=y+3=6
    #   b=0 -> m>=7  -> z>=m+7=14； b=0 -> n>=100 -> z>=n+100=200
    # 汇合点必须取所有前驱给出的最大下界 200，不能只沿录入顺序取到 20。
    points = [pt("a", 0), pt("b", 0), pt("x", 0), pt("y", 0),
              pt("m", 0), pt("n", 0), pt("z", 5)]
    relations = [
        rel("a", "x", 10), rel("a", "y", 3),
        rel("b", "m", 7), rel("b", "n", 100),
        rel("x", "z", 10), rel("y", "z", 3),
        rel("m", "z", 7), rel("n", "z", 100),
    ]
    out = earliest_schedule(points, relations)
    assert out["status"] == STATUS_OK
    assert out["times"]["z"] == 200
    assert out["times"]["x"] == 10
    assert out["times"]["y"] == 3
    assert out["times"]["n"] == 100
    assert out["times"]["m"] == 7
    # 自身 release 下界同样参与汇合（z.release=5，被 200 覆盖）
    assert all(out["times"][p["id"]] >= p["release"]
               for p in points)


def test_reconvergence_after_delayed_late_predecessor():
    # 仅更新录入靠前的边而漏掉更晚前驱是经典 bug：这里让"晚到"的前驱变大。
    points = [pt("s", 0), pt("early", 0), pt("late", 0), pt("join", 0)]
    relations = [rel("s", "early", 5), rel("s", "late", 50),
                 rel("early", "join", 5), rel("late", "join", 50)]
    out = earliest_schedule(points, relations)
    assert out["status"] == STATUS_OK
    assert out["times"] == {"s": 0, "early": 5, "late": 50, "join": 100}


# ---- 输入乱序：同一批规则必须得到同一份时间表 ------------------------------

def test_order_independence_shuffled_points_and_relations():
    # 无环 DAG：a=2 -> b>=5；c>=max(7, b+4=9, a+1=3)=9；d 独立保持 1。
    points = [pt("a", 2), pt("b", 0, latest=10**9), pt("c", 7), pt("d", 1)]
    relations = [rel("a", "b", 3), rel("b", "c", 4), rel("a", "c", 1)]
    expected = {"a": 2, "b": 5, "c": 9, "d": 1}

    rng = random.Random(20260922)
    for trial in range(20):
        shuffled_points = points[:]
        shuffled_relations = relations[:]
        rng.shuffle(shuffled_points)
        rng.shuffle(shuffled_relations)
        out = earliest_schedule(shuffled_points, shuffled_relations)
        assert out["status"] == STATUS_OK, trial
        assert out["times"] == expected, trial

    # 全部反向再验一次。
    out = earliest_schedule(list(reversed(points)),
                            list(reversed(relations)))
    assert out["times"] == expected


# ---- 延误传播 -------------------------------------------------------------

def test_delay_propagates_downstream():
    # release 全部为 0；把 a 推迟到 40 后，b、c 必须沿链顺延。
    points = [pt("a", 0), pt("b", 0), pt("c", 0), pt("standalone", 9)]
    relations = [rel("a", "b", 5), rel("b", "c", 6)]
    out = earliest_schedule(points, relations, delay={"a": 40})
    assert out["status"] == STATUS_OK
    assert out["times"] == {"a": 40, "b": 45, "c": 51,
                            "standalone": 9}


def test_delay_through_diamond_respects_all_paths():
    points = [pt("a", 0), pt("b", 0), pt("c", 0), pt("d", 0)]
    relations = [rel("a", "b", 2), rel("a", "c", 3),
                 rel("b", "d", 10), rel("c", "d", 1)]
    out = earliest_schedule(points, relations, delay={"a": 10})
    assert out["status"] == STATUS_OK
    # a=10 -> b=12,c=13 -> d=max(12+10, 13+1)=22
    assert out["times"] == {"a": 10, "b": 12, "c": 13, "d": 22}


def test_delay_partial_overrides_keep_release_floor():
    points = [pt("a", 5), pt("b", 8)]
    out = earliest_schedule(points, [], delay={"a": 5})
    assert out["times"] == {"a": 5, "b": 8}


# ---- 零间隔环 -------------------------------------------------------------

def test_zero_gap_cycle_is_feasible():
    # a -> b -> c -> a，权全为 0：零权环合法。零权边会把环上各点的下界
    # 互相拉平到最大基础下界，即 max(release)=3。
    points = [pt("a", 1), pt("b", 2), pt("c", 3)]
    relations = [rel("a", "b", 0), rel("b", "c", 0), rel("c", "a", 0)]
    out = earliest_schedule(points, relations)
    assert out["status"] == STATUS_OK
    assert out["times"] == {"a": 3, "b": 3, "c": 3}


def test_zero_gap_self_loop_is_feasible():
    points = [pt("a", 4)]
    out = earliest_schedule(points, [rel("a", "a", 0)])
    assert out["status"] == STATUS_OK
    assert out["times"] == {"a": 4}


def test_mixed_cycle_zero_weight_with_branch():
    # 环 a->b(0), b->a(0) 是零权；外部 d->a 给 12。
    points = [pt("a", 0), pt("b", 0), pt("d", 0)]
    relations = [rel("a", "b", 0), rel("b", "a", 0), rel("d", "a", 12)]
    out = earliest_schedule(points, relations)
    assert out["status"] == STATUS_OK
    assert out["times"] == {"d": 0, "a": 12, "b": 12}


# ---- 正权环 ---------------------------------------------------------------

def test_positive_cycle_detected():
    points = [pt("a", 0), pt("b", 0), pt("c", 0)]
    relations = [rel("a", "b", 1), rel("b", "c", 2), rel("c", "a", 3)]
    out = earliest_schedule(points, relations)
    assert out["status"] == STATUS_POSITIVE_CYCLE
    assert "times" not in out


def test_positive_self_loop_detected_regardless_of_order():
    points = [pt("a", 0), pt("b", 0)]
    relations = [rel("a", "b", 0), rel("b", "b", 1)]
    for ordering in (relations, list(reversed(relations))):
        assert (earliest_schedule(points, ordering)["status"]
                == STATUS_POSITIVE_CYCLE)


def test_positive_cycle_reported_before_deadline():
    # 同时存在正权环与 latest 违约时，优先报 positive_cycle。
    points = [pt("a", 0, latest=0), pt("b", 0, latest=0)]
    relations = [rel("a", "b", 1), rel("b", "a", 1)]
    out = earliest_schedule(points, relations)
    assert out["status"] == STATUS_POSITIVE_CYCLE


# ---- latest 上界 ----------------------------------------------------------

def test_deadline_exceeded_lists_violations():
    points = [pt("a", 0, latest=10), pt("b", 0, latest=5)]
    out = earliest_schedule(points, [rel("a", "b", 8)])
    assert out["status"] == STATUS_DEADLINE_EXCEEDED
    assert out["violations"] == [
        {"id": "b", "earliest": 8, "latest": 5}]


def test_within_latest_succeeds():
    points = [pt("a", 0, latest=10), pt("b", 0, latest=10)]
    out = earliest_schedule(points, [rel("a", "b", 10)])
    assert out["status"] == STATUS_OK
    assert out["times"] == {"a": 0, "b": 10}


def test_release_above_latest_can_not_happen_validated_elsewhere():
    # latest >= release 由校验层保证；这里确认恰好相等是允许的。
    points = [pt("a", 7, latest=7)]
    out = earliest_schedule(points, [])
    assert out["status"] == STATUS_OK
    assert out["times"] == {"a": 7}
